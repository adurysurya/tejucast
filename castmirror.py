"""Cast Streaming (the low-latency "mirroring" protocol Chrome uses for Cast screen).

Wire format follows Google's Open Screen reference implementation
(chromium.googlesource.com/openscreen, cast/streaming):
  * OFFER/ANSWER JSON on urn:x-cast:com.google.cast.webrtc to app 0F5096E8
  * RTP + Cast header over one UDP port, AES-128-CTR encrypted per frame
  * RTCP Sender Reports out; Cast feedback (ACK/NACK) + PLI in
"""
import ctypes
import ctypes.util
import os
import random
import socket
import struct
import threading
import time

MIRROR_APP = "0F5096E8"
NS_WEBRTC = "urn:x-cast:com.google.cast.webrtc"

PT_VIDEO = 96    # kVideoHackForAndroidTV
PT_AUDIO = 127   # kAudioHackForAndroidTV
MAX_PACKET = 1500 - 20 - 8          # kMaxRtpPacketSizeForIpv4UdpOnEthernet
HEADER = 12 + 7                      # RTP header + Cast header (w/ reference frame id)
MAX_PAYLOAD = MAX_PACKET - HEADER - 4
MAX_INFLIGHT = 100                   # stay under the receiver's kMaxUnackedFrames (120)
NTP_UNIX_OFFSET = 2208988800


class AesCtr:
    """AES-128-CTR via the system libcrypto (no Python packages needed)."""

    _lib = None

    def __init__(self, key):
        if AesCtr._lib is None:
            name = ctypes.util.find_library("crypto")
            if not name:
                raise RuntimeError("libcrypto (OpenSSL) not found")
            lib = ctypes.CDLL(name)
            vp = ctypes.c_void_p
            lib.EVP_CIPHER_CTX_new.restype = vp
            lib.EVP_aes_128_ctr.restype = vp
            lib.EVP_EncryptInit_ex.argtypes = [vp, vp, vp, ctypes.c_char_p, ctypes.c_char_p]
            lib.EVP_EncryptUpdate.argtypes = [vp, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int),
                                              ctypes.c_char_p, ctypes.c_int]
            lib.EVP_CIPHER_CTX_free.argtypes = [vp]
            AesCtr._lib = lib
        self.lib = AesCtr._lib
        self.key = bytes(key)
        self.ctx = self.lib.EVP_CIPHER_CTX_new()
        self.cipher = self.lib.EVP_aes_128_ctr()

    def crypt(self, iv, data):
        if self.lib.EVP_EncryptInit_ex(self.ctx, self.cipher, None, self.key, bytes(iv)) != 1:
            raise RuntimeError("EVP_EncryptInit_ex failed")
        out = ctypes.create_string_buffer(len(data) + 16)
        outl = ctypes.c_int(0)
        if self.lib.EVP_EncryptUpdate(self.ctx, out, ctypes.byref(outl), data, len(data)) != 1:
            raise RuntimeError("EVP_EncryptUpdate failed")
        return out.raw[:outl.value]


def ntp_timestamp(unix_seconds):
    sec = int(unix_seconds)
    frac = int((unix_seconds - sec) * (1 << 32)) & 0xFFFFFFFF
    return ((sec + NTP_UNIX_OFFSET) & 0xFFFFFFFF) << 32 | frac


class RtpStream:
    """One Cast Streaming sender stream (audio or video)."""

    def __init__(self, kind, codec, ssrc, payload_type, rate, target_delay_ms):
        self.kind, self.codec = kind, codec
        self.ssrc, self.pt, self.rate = ssrc, payload_type, rate
        self.key, self.iv_mask = os.urandom(16), os.urandom(16)
        self.target_delay = target_delay_ms
        self.aes = AesCtr(self.key)
        self.sock = self.dest = self.clock = None
        self.lock = threading.Lock()
        self.next_frame = 0
        self.checkpoint = -1
        self.seq = random.randint(0, 0xFFFF)
        self.history = {}           # frame_id -> [packets]
        self.resent = {}            # (frame_id, packet_id) -> time
        self.packets = self.octets = 0
        self.sr_sent = False
        self.waiting_for_key = False
        self.on_keyframe_needed = None
        self.stats = {"frames": 0, "dropped": 0, "nacked": 0, "pli": 0}

    # ---- OFFER description
    def offer(self, index, **extra):
        d = {
            "index": index,
            "type": "audio_source" if self.kind == "audio" else "video_source",
            "codecName": self.codec,
            "rtpProfile": "cast",
            "rtpPayloadType": self.pt,
            "ssrc": self.ssrc,
            "targetDelay": self.target_delay,
            "aesKey": self.key.hex(),
            "aesIvMask": self.iv_mask.hex(),
            "timeBase": f"1/{self.rate}",
            "receiverRtcpEventLog": False,
        }
        d.update(extra)
        return d

    def attach(self, sock, dest, clock):
        """clock() -> (running_time_ns, unix_time) taken at the same instant."""
        self.sock, self.dest, self.clock = sock, dest, clock

    # ---- sending
    def send_sender_report(self):
        running_ns, wall = self.clock()
        rtp = running_ns * self.rate // 1_000_000_000
        pkt = struct.pack(">BBHIQIII", 0x80, 200, 6, self.ssrc, ntp_timestamp(wall),
                          rtp & 0xFFFFFFFF, self.packets & 0xFFFFFFFF, self.octets & 0xFFFFFFFF)
        try:
            self.sock.sendto(pkt, self.dest)
        except OSError:
            pass
        self.sr_sent = True

    def send_frame(self, data, pts_ns, keyframe):
        if not self.sr_sent:
            self.send_sender_report()  # receiver drops packets until it has an SR
        rtp_ts = (pts_ns * self.rate // 1_000_000_000) & 0xFFFFFFFF
        with self.lock:
            if self.next_frame - 1 - self.checkpoint >= MAX_INFLIGHT:
                # TV isn't acknowledging (stalled or lossy): drop, then restart from a keyframe.
                self.stats["dropped"] += 1
                if self.kind == "video" and not self.waiting_for_key:
                    self.waiting_for_key = True
                    if self.on_keyframe_needed:
                        self.on_keyframe_needed()
                return
            if self.waiting_for_key:
                if not keyframe:
                    self.stats["dropped"] += 1
                    return
                self.waiting_for_key = False
            fid = self.next_frame
            self.next_frame += 1
            ref = fid if keyframe else fid - 1
            nonce = bytearray(16)
            struct.pack_into(">I", nonce, 8, fid & 0xFFFFFFFF)
            iv = bytes(a ^ b for a, b in zip(nonce, self.iv_mask))
            enc = self.aes.crypt(iv, data)
            chunks = [enc[i:i + MAX_PAYLOAD] for i in range(0, len(enc), MAX_PAYLOAD)] or [b""]
            n = len(chunks)
            pkts = []
            for pid, chunk in enumerate(chunks):
                rtp = struct.pack(">BBHII", 0x80, (0x80 if pid == n - 1 else 0) | self.pt,
                                  self.seq, rtp_ts, self.ssrc)
                self.seq = (self.seq + 1) & 0xFFFF
                cast = struct.pack(">BBHHB", (0x80 if keyframe else 0) | 0x40, fid & 0xFF,
                                   pid, n - 1, ref & 0xFF)
                pkts.append(rtp + cast + chunk)
            self.history[fid] = pkts
            old = fid - 2 * MAX_INFLIGHT
            if old in self.history:
                del self.history[old]
            self.packets += n
            self.octets += len(enc)
            self.stats["frames"] += 1
        for p in pkts:
            try:
                self.sock.sendto(p, self.dest)
            except OSError:
                pass

    # ---- feedback from the TV
    def _expand(self, low8, after):
        """Expand an 8-bit truncated frame id to the full id that comes after `after`."""
        return after + 1 + ((low8 - (after + 1)) & 0xFF)

    def on_feedback(self, ckpt8, losses):
        now = time.monotonic()
        resend = []
        with self.lock:
            latest = self.next_frame - 1
            ckpt = latest - ((latest - ckpt8) & 0xFF)
            if ckpt > self.checkpoint:
                for f in range(self.checkpoint + 1, ckpt + 1):
                    self.history.pop(f, None)
                self.checkpoint = ckpt
            for fid8, pid, bits in losses:
                fid = self._expand(fid8, self.checkpoint)
                pkts = self.history.get(fid)
                if not pkts:
                    continue
                if pid == 0xFFFF:
                    wanted = range(len(pkts))
                else:
                    wanted = [pid] + [pid + 1 + i for i in range(8) if bits >> i & 1]
                for p in wanted:
                    if p < len(pkts) and now - self.resent.get((fid, p), 0) > 0.02:
                        self.resent[(fid, p)] = now
                        resend.append(pkts[p])
            if len(self.resent) > 5000:
                self.resent = {k: v for k, v in self.resent.items() if now - v < 1}
        self.stats["nacked"] += len(resend)
        for p in resend:
            try:
                self.sock.sendto(p, self.dest)
            except OSError:
                pass

    def on_pli(self):
        self.stats["pli"] += 1
        if self.on_keyframe_needed:
            self.on_keyframe_needed()


class MirrorTransport:
    """UDP socket shared by all streams + RTCP receive thread."""

    def __init__(self, host, port, streams, clock):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 << 20)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        try:  # mark as interactive video for Wi-Fi QoS (DSCP AF41)
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, 0x88)
        except OSError:
            pass
        self.sock.bind(("0.0.0.0", 0))
        self.sock.settimeout(0.5)
        self.dest = (host, port)
        self.by_ssrc = {s.ssrc: s for s in streams}
        self.streams = streams
        for s in streams:
            s.attach(self.sock, self.dest, clock)
        self.running = True
        self.thread = threading.Thread(target=self._rx, daemon=True)
        self.thread.start()

    def send_reports(self):
        for s in self.streams:
            s.send_sender_report()

    def _rx(self):
        while self.running:
            try:
                data, _ = self.sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                self._parse_rtcp(data)
            except (struct.error, IndexError):
                pass

    def _parse_rtcp(self, data):
        pos = 0
        while pos + 4 <= len(data):
            b0, pt, words = struct.unpack_from(">BBH", data, pos)
            if b0 >> 6 != 2:
                return
            end = pos + (words + 1) * 4
            body = data[pos + 4:end]
            fmt = b0 & 0x1F
            if pt == 206 and len(body) >= 8:
                media_ssrc = struct.unpack_from(">I", body, 4)[0]
                s = self.by_ssrc.get(media_ssrc)
                if s:
                    if fmt == 1:
                        s.on_pli()
                    elif fmt == 15 and body[8:12] == b"CAST" and len(body) >= 16:
                        ckpt, nloss = body[12], body[13]
                        losses = []
                        for i in range(nloss):
                            off = 16 + 4 * i
                            if off + 4 > len(body):
                                break
                            fid8, pid, bits = struct.unpack_from(">BHB", body, off)
                            losses.append((fid8, pid, bits))
                        s.on_feedback(ckpt, losses)
            pos = end

    def close(self):
        self.running = False
        try:
            self.sock.close()
        except OSError:
            pass


def negotiate(cc, streams_offer, log, receiver_ns, conn_ns, app_status_fn, timeout=20):
    """Launch the mirroring receiver, send OFFER, return (transport_id, session_id, answer)."""
    cc.send("receiver-0", receiver_ns, {"type": "LAUNCH", "appId": MIRROR_APP}, add_id=True)
    status = cc.wait_for(lambda m: app_status_fn(m, MIRROR_APP) is not None, timeout=timeout)
    app = app_status_fn(status, MIRROR_APP)
    namespaces = [n.get("name") for n in app.get("namespaces", [])]
    if namespaces and NS_WEBRTC not in namespaces:
        raise RuntimeError("TV's mirroring app does not support Cast Streaming")
    transport, session_id = app["transportId"], app["sessionId"]
    cc.send(transport, conn_ns, {"type": "CONNECT"})
    seq = random.randint(1, 1 << 20)
    cc.send(transport, NS_WEBRTC, {
        "type": "OFFER",
        "seqNum": seq,
        "offer": {"castMode": "mirroring", "supportedStreams": streams_offer},
    })
    msg = cc.wait_for(lambda m: m["ns"] == NS_WEBRTC and m["data"].get("type") == "ANSWER",
                      timeout=timeout)
    d = msg["data"]
    if d.get("result") != "ok":
        raise RuntimeError(f"TV rejected the stream offer: {d.get('error') or d}")
    return transport, session_id, d["answer"]
