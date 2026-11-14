#!/usr/bin/env python3
"""tejucast - stream your Linux (Wayland) desktop to a Google TV / Chromecast.

How it works
  1. Asks the desktop's ScreenCast portal (KDE/GNOME) for a screen or window.
  2. GStreamer captures it from PipeWire (+ system audio) and encodes it.
  3. Default (mirroring): sends it with Cast Streaming - the same low-latency
     RTP protocol Chrome's "Cast screen" uses (see castmirror.py).
     --hls: serves a live HLS stream and has the TV's media player play it.

No third-party Python packages needed (uses PyGObject + GStreamer from the OS).
"""
import argparse
import json
import re
import os
import shutil
import signal
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gio, GLib, Gst  # noqa: E402

CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "tejucast")
TOKEN_FILE = os.path.join(CONFIG_DIR, "restore_token")


STATUS_DIR = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "tejucast")
STATUS_FILE = os.path.join(STATUS_DIR, "status.json")
UNIT = "tejucast"


class Status:
    """Live state for front-ends (the Plasma tray widget reads this file)."""

    def __init__(self):
        self.data = {}
        self.enabled = False
        self.lock = threading.Lock()

    def update(self, **kw):
        if not self.enabled:
            return
        with self.lock:
            self.data.update(kw, pid=os.getpid(), updated=time.time())
            try:
                os.makedirs(STATUS_DIR, exist_ok=True)
                tmp = STATUS_FILE + f".{os.getpid()}"
                with open(tmp, "w") as f:
                    json.dump(self.data, f)
                os.replace(tmp, STATUS_FILE)
            except OSError:
                pass


STATUS = Status()


def log(*a):
    msg = " ".join(str(x) for x in a)
    print("[tejucast]", msg, flush=True)
    STATUS.update(message=msg)


# --------------------------------------------------------------------------- discovery

CACHE_FILE = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "tejucast",
                          "devices.json")


SERVICES = {"_googlecast._tcp": "cast", "_airplay._tcp": "airplay"}


def _avahi_unescape(text):
    """avahi-browse -p escapes names: spaces/UTF-8 bytes appear as \\DDD (decimal)."""
    out, i = bytearray(), 0
    while i < len(text):
        if text[i] == "\\" and i + 4 <= len(text) and text[i + 1:i + 4].isdigit():
            out.append(int(text[i + 1:i + 4]) & 0xFF)
            i += 4
        elif text[i] == "\\" and i + 1 < len(text):
            out += text[i + 1].encode()
            i += 2
        else:
            out += text[i].encode()
            i += 1
    return out.decode("utf-8", "replace")


def _parse_avahi_line(line):
    f = line.split(";")
    if len(f) < 10 or f[0] != "=" or f[2] != "IPv4" or f[4] not in SERVICES:
        return None
    txt = {}
    for tok in f[9].split('" "'):
        tok = tok.strip('"')
        if "=" in tok:
            k, v = tok.split("=", 1)
            txt[k] = v
    if SERVICES[f[4]] == "cast":
        return {"name": txt.get("fn", _avahi_unescape(f[3])), "model": txt.get("md", ""), "host": f[7],
                "port": int(f[8]), "id": txt.get("id", f[3]), "protocols": ["cast"]}
    return {"name": _avahi_unescape(f[3]), "model": txt.get("model", ""), "host": f[7],
            "airplay_port": int(f[8]), "id": "airplay:" + txt.get("deviceid", f[3]), "protocols": ["airplay"]}


def _merge(into, d):
    """Combine the Cast and AirPlay records of one device (same IP). Cast naming wins."""
    if not into:
        return dict(d)
    cast_first = "cast" in d["protocols"]
    base, extra = (dict(d), into) if cast_first else (dict(into), d)
    for k, v in extra.items():
        base.setdefault(k, v)
    base["protocols"] = sorted(set(into["protocols"]) | set(d["protocols"]))
    return base


def discover(timeout=3.0, use_cache=True):
    """Find Google Cast and AirPlay receivers via avahi (listens `timeout` s; TVs can be slow
    to answer). Devices seen before are remembered so front-ends can list them instantly."""
    import select
    by_host = {}
    procs = []
    for svc in SERVICES:
        try:
            procs.append(subprocess.Popen(["avahi-browse", "-rp", svc], stdout=subprocess.PIPE,
                                          stderr=subprocess.DEVNULL))
        except FileNotFoundError:
            break
    bufs = {p.stdout.fileno(): "" for p in procs}
    end = time.time() + timeout
    try:
        open_fds = set(bufs)
        while open_fds and time.time() < end:
            r, _, _ = select.select(list(open_fds), [], [], max(0.0, end - time.time()))
            if not r:
                break
            for fd in r:
                chunk = os.read(fd, 65536).decode(errors="replace")
                if not chunk:
                    open_fds.discard(fd)
                    continue
                bufs[fd] += chunk
                *lines, bufs[fd] = bufs[fd].split("\n")
                for line in lines:
                    d = _parse_avahi_line(line)
                    if d:
                        by_host[d["host"]] = _merge(by_host.get(d["host"]), d)
    finally:
        for p in procs:
            p.kill()
            p.wait()
    found = {d["id"]: d for d in by_host.values()}
    cache = {}
    try:
        with open(CACHE_FILE) as f:
            cache = {d["id"]: d for d in json.load(f) if "protocols" in d}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    # TVs often answer for only one of their services per scan: keep protocols seen before
    # on the same IP, so e.g. AirPlay doesn't vanish from a TV that also speaks Cast.
    for key, d in list(found.items()):
        for old in cache.values():
            if old.get("host") == d["host"] and set(old["protocols"]) - set(d["protocols"]):
                merged = _merge(d, dict(old, protocols=[p for p in old["protocols"]
                                                         if p not in d["protocols"]]))
                del found[key]
                found[merged["id"]] = merged
                break
    if found:
        # drop stale cache entries for the same IP (e.g. an AirPlay-only record now merged)
        live_hosts = {d["host"] for d in found.values()}
        cache = {k: d for k, d in cache.items() if d.get("host") not in live_hosts}
        cache.update(found)
        try:
            os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
            with open(CACHE_FILE, "w") as f:
                json.dump(list(cache.values()), f)
        except OSError:
            pass
    devices = [dict(d, online=True) for d in found.values()]
    if use_cache:
        devices += [dict(d, online=False) for k, d in cache.items() if k not in found]
    devices.sort(key=lambda d: (not d["online"], d["name"].lower()))
    return devices


# --------------------------------------------------------------------------- Cast v2 protocol

NS_CONN = "urn:x-cast:com.google.cast.tp.connection"
NS_HEARTBEAT = "urn:x-cast:com.google.cast.tp.heartbeat"
NS_RECEIVER = "urn:x-cast:com.google.cast.receiver"
NS_MEDIA = "urn:x-cast:com.google.cast.media"
DEFAULT_MEDIA_RECEIVER = "CC1AD845"


def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _read_varint(buf, pos):
    shift = result = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7


def encode_cast_message(src, dst, ns, payload):
    """Hand-rolled protobuf for extensions/api/cast_channel/cast_channel.proto CastMessage."""
    def s(num, val):
        b = val.encode()
        return _varint(num << 3 | 2) + _varint(len(b)) + b

    def i(num, val):
        return _varint(num << 3) + _varint(val)

    msg = i(1, 0) + s(2, src) + s(3, dst) + s(4, ns) + i(5, 0) + s(6, json.dumps(payload))
    return struct.pack(">I", len(msg)) + msg


def decode_cast_message(buf):
    fields, pos = {}, 0
    while pos < len(buf):
        key, pos = _read_varint(buf, pos)
        num, wt = key >> 3, key & 7
        if wt == 0:
            fields[num], pos = _read_varint(buf, pos)
        elif wt == 2:
            ln, pos = _read_varint(buf, pos)
            fields[num] = bytes(buf[pos:pos + ln])
            pos += ln
        elif wt == 1:
            pos += 8
        elif wt == 5:
            pos += 4
        else:
            raise ValueError("bad wire type")
    payload = fields.get(6, b"{}").decode(errors="replace")
    try:
        data = json.loads(payload)
    except ValueError:
        data = {}
    return {"src": fields.get(2, b"").decode(), "dst": fields.get(3, b"").decode(),
            "ns": fields.get(4, b"").decode(), "data": data}


class CastClient:
    def __init__(self, host, port=8009):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # Cast devices use self-signed certs
        self.sock = ctx.wrap_socket(socket.create_connection((host, port), timeout=10))
        self.sock.settimeout(1.0)
        self.buf = bytearray()
        self.req_id = 0
        self.last_ping = 0.0
        self.send("receiver-0", NS_CONN, {"type": "CONNECT"})

    def send(self, dst, ns, payload, add_id=False):
        if add_id:
            self.req_id += 1
            payload = dict(payload, requestId=self.req_id)
        self.sock.sendall(encode_cast_message("sender-0", dst, ns, payload))
        return self.req_id

    def recv(self):
        """Return the next message or None after ~1s; keeps the heartbeat going."""
        if time.time() - self.last_ping > 5:
            self.send("receiver-0", NS_HEARTBEAT, {"type": "PING"})
            self.last_ping = time.time()
        while True:
            if len(self.buf) >= 4:
                ln = struct.unpack(">I", self.buf[:4])[0]
                if len(self.buf) >= 4 + ln:
                    msg = decode_cast_message(self.buf[4:4 + ln])
                    del self.buf[:4 + ln]
                    if msg["ns"] == NS_HEARTBEAT and msg["data"].get("type") == "PING":
                        self.send(msg["src"], NS_HEARTBEAT, {"type": "PONG"})
                        continue
                    if msg["ns"] == NS_CONN and msg["data"].get("type") == "CLOSE":
                        raise ConnectionError("TV closed the connection")
                    return msg
            try:
                chunk = self.sock.recv(65536)
            except (socket.timeout, ssl.SSLWantReadError):
                return None
            if not chunk:
                raise ConnectionError("connection to TV lost")
            self.buf += chunk

    def wait_for(self, pred, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            m = self.recv()
            if m and pred(m):
                return m
        raise TimeoutError("TV did not respond in time")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def _app_from_status(msg, app_id):
    if msg["data"].get("type") != "RECEIVER_STATUS":
        return None
    for app in msg["data"].get("status", {}).get("applications", []) or []:
        if app.get("appId") == app_id:
            return app
    return None


def cast_session(host, url, title, stop, on_end):
    """Launch the media receiver on the TV, play `url`, keep alive until `stop` is set."""
    cc = None
    session_id = None
    try:
        cc = CastClient(host)
        cc.send("receiver-0", NS_RECEIVER, {"type": "LAUNCH", "appId": DEFAULT_MEDIA_RECEIVER}, add_id=True)
        status = cc.wait_for(lambda m: _app_from_status(m, DEFAULT_MEDIA_RECEIVER) is not None)
        app = _app_from_status(status, DEFAULT_MEDIA_RECEIVER)
        transport, session_id = app["transportId"], app["sessionId"]
        cc.send(transport, NS_CONN, {"type": "CONNECT"})
        cc.send(transport, NS_MEDIA, {
            "type": "LOAD",
            "autoplay": True,
            "media": {
                "contentId": url,
                "contentUrl": url,
                "streamType": "LIVE",
                "contentType": "application/vnd.apple.mpegurl",
                "hlsSegmentFormat": "ts",
                "hlsVideoSegmentFormat": "mpeg2_ts",
                "metadata": {"metadataType": 0, "title": title},
            },
        }, add_id=True)
        log("Sent stream to TV, waiting for playback...")
        last_state = None
        while not stop.is_set():
            m = cc.recv()
            if not m:
                continue
            t = m["data"].get("type")
            if m["ns"] == NS_MEDIA and t == "MEDIA_STATUS":
                for st in m["data"].get("status", []):
                    state = st.get("playerState")
                    if state and state != last_state:
                        log("TV player:", state + (f" ({st.get('idleReason')})" if st.get("idleReason") else ""))
                        last_state = state
                        if state == "IDLE" and st.get("idleReason") in ("ERROR", "FINISHED", "CANCELLED"):
                            log("Playback ended on the TV.")
                            return
            elif m["ns"] == NS_MEDIA and t in ("LOAD_FAILED", "LOAD_CANCELLED", "INVALID_REQUEST"):
                log("TV refused the stream:", m["data"])
                return
            elif m["ns"] == NS_RECEIVER and t == "RECEIVER_STATUS":
                if _app_from_status(m, DEFAULT_MEDIA_RECEIVER) is None:
                    log("Receiver app was closed on the TV.")
                    session_id = None
                    return
    except Exception as e:  # noqa: BLE001
        log("Cast error:", e)
    finally:
        if cc:
            if session_id and stop.is_set():
                try:
                    cc.send("receiver-0", NS_RECEIVER, {"type": "STOP", "sessionId": session_id}, add_id=True)
                    time.sleep(0.3)
                except OSError:
                    pass
            cc.close()
        on_end()


def get_status(host):
    cc = CastClient(host)
    cc.send("receiver-0", NS_RECEIVER, {"type": "GET_STATUS"}, add_id=True)
    m = cc.wait_for(lambda m: m["data"].get("type") == "RECEIVER_STATUS", timeout=10)
    cc.close()
    return m["data"]


# --------------------------------------------------------------------------- ScreenCast portal

class Portal:
    DEST = "org.freedesktop.portal.Desktop"
    PATH = "/org/freedesktop/portal/desktop"
    IFACE = "org.freedesktop.portal.ScreenCast"

    def __init__(self):
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.sender = self.bus.get_unique_name()[1:].replace(".", "_")
        self.n = 0

    def _prop(self, name):
        try:
            v = self.bus.call_sync(self.DEST, self.PATH, "org.freedesktop.DBus.Properties", "Get",
                                   GLib.Variant("(ss)", (self.IFACE, name)), None,
                                   Gio.DBusCallFlags.NONE, -1, None)
            return v.unpack()[0]
        except GLib.Error:
            return 0

    def _request(self, method, sig, args_fn):
        self.n += 1
        token = f"tejucast{os.getpid()}_{self.n}"
        path = f"/org/freedesktop/portal/desktop/request/{self.sender}/{token}"
        loop, result = GLib.MainLoop(), {}

        def on_response(_c, _s, _o, _i, _sig, params):
            result["code"], result["res"] = params.unpack()
            loop.quit()

        sub = self.bus.signal_subscribe(self.DEST, "org.freedesktop.portal.Request", "Response",
                                        path, None, Gio.DBusSignalFlags.NONE, on_response)
        try:
            self.bus.call_sync(self.DEST, self.PATH, self.IFACE, method,
                               GLib.Variant(sig, args_fn(token)), None, Gio.DBusCallFlags.NONE, -1, None)
            loop.run()
        finally:
            self.bus.signal_unsubscribe(sub)
        if result["code"] != 0:
            raise RuntimeError(f"Screen sharing {'cancelled' if result['code'] == 1 else 'failed'} ({method})")
        return result["res"]

    def start(self, remember=True, virtual=False):
        token_file = TOKEN_FILE + ("-extend" if virtual else "")
        res = self._request("CreateSession", "(a{sv})", lambda t: ({
            "handle_token": GLib.Variant("s", t),
            "session_handle_token": GLib.Variant("s", t + "s"),
        },))
        session = res["session_handle"]

        types = 4 if virtual else 1 | 2  # 4 = new virtual monitor (extend the desktop)
        if virtual and not self._prop("AvailableSourceTypes") & 4:
            raise RuntimeError("This desktop can't create virtual monitors (needs KDE Plasma 6 / recent GNOME)")
        opts = {"types": GLib.Variant("u", types), "multiple": GLib.Variant("b", False)}
        if self._prop("AvailableCursorModes") & 2:
            opts["cursor_mode"] = GLib.Variant("u", 2)  # draw cursor into the video
        if remember and self._prop("version") >= 4:
            opts["persist_mode"] = GLib.Variant("u", 2)
            try:
                with open(token_file) as f:
                    opts["restore_token"] = GLib.Variant("s", f.read().strip())
            except OSError:
                pass
        self._request("SelectSources", "(oa{sv})",
                      lambda t: (session, dict(opts, handle_token=GLib.Variant("s", t))))

        res = self._request("Start", "(osa{sv})",
                            lambda t: (session, "", {"handle_token": GLib.Variant("s", t)}))
        if remember and res.get("restore_token"):
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(token_file, "w") as f:
                f.write(res["restore_token"])
        streams = res.get("streams") or []
        if not streams:
            raise RuntimeError("Portal returned no streams")
        node_id = streams[0][0]

        ret, fds = self.bus.call_with_unix_fd_list_sync(
            self.DEST, self.PATH, self.IFACE, "OpenPipeWireRemote",
            GLib.Variant("(oa{sv})", (session, {})), GLib.VariantType("(h)"),
            Gio.DBusCallFlags.NONE, -1, None, None)
        fd = fds.get(ret.unpack()[0])
        return fd, node_id


# --------------------------------------------------------------------------- HTTP + pipeline

def make_http_server(directory, port):
    class Handler(SimpleHTTPRequestHandler):
        extensions_map = {**SimpleHTTPRequestHandler.extensions_map,
                          ".m3u8": "application/vnd.apple.mpegurl", ".ts": "video/mp2t"}

        def end_headers(self):
            self.send_header("Access-Control-Allow-Origin", "*")  # required by the Cast receiver
            self.send_header("Access-Control-Allow-Headers", "*")
            if self.path.endswith(".m3u8"):
                self.send_header("Cache-Control", "no-cache, no-store")
            super().end_headers()

        def do_OPTIONS(self):
            self.send_response(204)
            self.end_headers()

        def log_message(self, fmt, *a):
            if os.environ.get("TEJUCAST_HTTPLOG"):
                log("http", self.client_address[0], fmt % a)

    srv = ThreadingHTTPServer(("0.0.0.0", port), partial(Handler, directory=directory))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def local_ip_towards(host):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, 9))
        return s.getsockname()[0]
    finally:
        s.close()


class TvAudioSink:
    """A virtual speaker named after the TV. While casting it is the default output, so
    sound goes to the TV instead of the laptop, and it shows up in the audio settings."""

    SINK = "tejucast_tv"

    def __init__(self, tv_name):
        self.module = None
        self.previous = None
        self._unload_stale()
        desc = f"{tv_name} (Teju Cast)".replace('"', "")
        props = f'device.description="{desc}" monitor.channel-volumes=true device.icon_name=video-television'
        r = subprocess.run(["pactl", "load-module", "module-null-sink", f"sink_name={self.SINK}",
                            "rate=48000", "channels=2", f"sink_properties={props}"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            raise RuntimeError("could not create TV audio device: " + r.stderr.strip())
        self.module = r.stdout.strip()
        self.previous = subprocess.run(["pactl", "get-default-sink"], capture_output=True, text=True,
                                       timeout=5).stdout.strip()
        subprocess.run(["pactl", "set-default-sink", self.SINK], timeout=5)
        # Move apps that are already playing. Never move internal streams of filter chains
        # (e.g. a laptop's speaker DSP): they are pinned to their hardware and the whole chain
        # is torn down if their target disappears.
        for sid in self._app_streams():
            subprocess.run(["pactl", "move-sink-input", str(sid), self.SINK], capture_output=True, timeout=5)
        log(f'Audio output switched to "{desc}" (pick your speakers in the volume menu to switch back)')

    @staticmethod
    def _app_streams():
        try:
            out = subprocess.run(["pactl", "-f", "json", "list", "sink-inputs"], capture_output=True,
                                 text=True, timeout=5).stdout
            inputs = json.loads(out or "[]")
        except (ValueError, OSError, subprocess.SubprocessError):
            return []  # can't tell apps from internal streams: move nothing
        ids = []
        for si in inputs:
            props = si.get("properties", {})
            if (props.get("node.link-group") or props.get("node.dont-fallback") == "true"
                    or props.get("node.dont-reconnect") == "true" or props.get("node.virtual") == "true"):
                continue
            ids.append(si["index"])
        return ids

    @property
    def monitor(self):
        return self.SINK + ".monitor"

    def _unload_stale(self):
        out = subprocess.run(["pactl", "list", "short", "modules"], capture_output=True, text=True,
                             timeout=5).stdout
        for line in out.splitlines():
            if f"sink_name={self.SINK}" in line:
                subprocess.run(["pactl", "unload-module", line.split("\t", 1)[0]], timeout=5)

    def close(self):
        if self.previous and self.previous != self.SINK:
            subprocess.run(["pactl", "set-default-sink", self.previous], capture_output=True, timeout=5)
        if self.module:
            subprocess.run(["pactl", "unload-module", self.module], capture_output=True, timeout=5)
            self.module = None


def default_monitor_source():
    try:
        sink = subprocess.run(["pactl", "get-default-sink"], capture_output=True, text=True, timeout=5).stdout.strip()
        return sink + ".monitor" if sink else None
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def build_pipeline(args, outdir, fd=None, node=None):
    h = args.height
    w = (h * 16 // 9) // 2 * 2
    fps = args.fps
    if args.test:
        vsrc = "videotestsrc is-live=true pattern=ball ! timeoverlay font-desc=\"Sans 48\""
    else:
        vsrc = (f"pipewiresrc fd={fd} path={node} do-timestamp=true keepalive-time={max(1000 // fps, 20)} "
                f"always-copy=true ! queue leaky=downstream max-size-buffers=2")
    have = Gst.ElementFactory.find
    venc = (f"openh264enc bitrate={args.bitrate * 1000} max-bitrate={args.bitrate * 1500} rate-control=bitrate "
            f"usage-type=screen complexity=low gop-size={fps} multi-thread={min(os.cpu_count() or 4, 4)} "
            f"enable-frame-skip=false")
    video = (f"{vsrc} ! videoconvertscale n-threads=4 add-borders=true ! "
             f"video/x-raw,format=I420,width={w},height={h},pixel-aspect-ratio=1/1 ! "
             f"videorate drop-only=false ! video/x-raw,framerate={fps}/1 ! {venc} ! "
             f"video/x-h264,profile=constrained-baseline ! h264parse config-interval=-1 ! queue ! hls.video")
    audio = ""
    if args.audio:
        aenc = "fdkaacenc bitrate=160000" if have("fdkaacenc") else "avenc_aac bitrate=160000"
        if args.test:
            asrc = "audiotestsrc is-live=true wave=ticks volume=0.3"
        else:
            mon = getattr(args, "audio_monitor", None) or default_monitor_source()
            asrc = f"pulsesrc device={mon} do-timestamp=true" if mon else "pulsesrc do-timestamp=true"
            log("Capturing audio from", mon or "default source")
        audio = (f" {asrc} ! audio/x-raw,rate=48000,channels=2 ! queue ! audioconvert ! audioresample ! "
                 f"audio/x-raw,rate=48000,channels=2 ! {aenc} ! aacparse ! queue ! hls.audio")
    sink = (f"hlssink2 name=hls location={outdir}/seg%05d.ts playlist-location={outdir}/live.m3u8 "
            f"target-duration=1 playlist-length=4 max-files=12")
    desc = f"{sink} {video}{audio}"
    if args.verbose:
        log("Pipeline:", desc)
    return Gst.parse_launch(desc)


def segments_ready(outdir, n=3):
    try:
        with open(os.path.join(outdir, "live.m3u8")) as f:
            return f.read().count("#EXTINF") >= n
    except OSError:
        return False


# --------------------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(description="Teju Cast: stream your screen to a Google TV / Chromecast or an AirPlay receiver.")
    p.add_argument("-d", "--device", help="TV name (substring match) - default: first found")
    p.add_argument("--host", help="TV IP address (skip discovery)")
    p.add_argument("-l", "--list", action="store_true", help="list Cast devices and exit")
    p.add_argument("--height", type=int, default=1080, help="output height: 720/1080 (default 1080)")
    p.add_argument("--fps", type=int, default=30, help="frame rate (default 30)")
    p.add_argument("--bitrate", type=int, default=6000, help="video bitrate in kbit/s (default 6000)")
    p.add_argument("--no-audio", dest="audio", action="store_false", help="don't send system audio")
    p.add_argument("--port", type=int, default=8090, help="local HTTP port (default 8090)")
    p.add_argument("-e", "--extend", action="store_true",
                   help="use the TV as a second monitor (extend the desktop) instead of mirroring")
    p.add_argument("--laptop-audio", action="store_true",
                   help="keep sound on the laptop too (capture it instead of moving it to the TV)")
    p.add_argument("--forget", action="store_true", help="ask again which screen/window to share")
    p.add_argument("--delay", type=int, default=150, help="mirroring target delay in ms (default 150)")
    p.add_argument("--codec", choices=["auto", "h264", "vp8"], default="auto", help="mirroring video codec")
    p.add_argument("--hls", action="store_true", help="use the older HLS video-stream mode (~4 s delay)")
    p.add_argument("--no-cast", action="store_true", help="only serve the HLS stream (for testing in VLC)")
    p.add_argument("--test", action="store_true", help="send a test pattern instead of the screen")
    p.add_argument("-a", "--airplay", action="store_true",
                   help="send over AirPlay (Apple TV, AirPlay TVs, Macs) using doubletake")
    p.add_argument("--pin", metavar="PIN", help="send the AirPlay pairing PIN to a waiting tejucast")
    p.add_argument("--label", help="display name for the TV (with --host)")
    p.add_argument("--json", action="store_true", help="with --list: machine-readable output")
    p.add_argument("--background", action="store_true",
                   help="run as a background user service (replacing any running one)")
    p.add_argument("--stop", action="store_true", help="stop the background service")
    p.add_argument("--status", action="store_true", help="print the current status as JSON")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    if args.stop:
        subprocess.run(["systemctl", "--user", "stop", UNIT], capture_output=True)
        return 0
    if args.pin is not None:
        return send_pin(args.pin)
    if args.status:
        print(json.dumps(read_status()))
        return 0
    if args.background:
        return launch_background()

    def _term(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _term)  # clean up (audio, TV session) even before the main loop
    STATUS.enabled = not args.list
    STATUS.update(state="starting", mode="extend" if args.extend else ("hls" if args.hls else "mirror"),
                  protocol="airplay" if args.airplay else "cast",
                  tv=args.label or args.device or args.host, started=time.time())

    if args.forget:
        for f in (TOKEN_FILE, TOKEN_FILE + "-extend"):
            if os.path.exists(f):
                os.remove(f)

    # ---- pick a TV
    host, name = args.host, args.label or args.host
    if args.list or (not host and not args.no_cast):
        if args.list and args.json:
            print(json.dumps(discover()))
            return 0
        log("Looking for TVs...")
        devs = discover(use_cache=False)
        if args.list:
            for d in devs:
                protos = " + ".join("Google Cast" if p == "cast" else "AirPlay" for p in d["protocols"])
                print(f"  {d['name']:<30} {d['model']:<20} {d['host']:<16} {protos}")
            if not devs:
                print("  (none found)")
            return 0
        proto = "airplay" if args.airplay else "cast"
        devs = [d for d in devs if proto in d["protocols"]]
        if args.device:
            devs = [d for d in devs if args.device.lower() in d["name"].lower()]
        if not devs:
            log(f"No matching {'AirPlay' if args.airplay else 'Google Cast'} device found. "
                "Try --list or --host <ip>.")
            return 1
        host, name = devs[0]["host"], devs[0]["name"]
    if host:
        log(f"Target: {name} ({host})")
        STATUS.update(tv=name, host=host)

    Gst.init(None)
    fd = node = None
    if not args.test and not args.airplay:
        STATUS.update(state="choosing")
        if args.extend:
            log("Creating a virtual monitor for the TV...")
        else:
            log("Choose the screen or window to share in the dialog...")
        try:
            portal = Portal()  # noqa: F841 - keeps the D-Bus session (and the screencast) alive
            fd, node = portal.start(remember=True, virtual=args.extend)
        except (RuntimeError, GLib.Error) as e:
            log(e)
            return 1
        log(f"Got PipeWire stream (node {node})")
        if args.extend:
            log("The TV is now a second display - arrange it in System Settings > Display & Monitor.")

    tv_audio = None
    if args.audio and not args.test and not args.laptop_audio and not args.no_cast:
        try:
            tv_audio = TvAudioSink(name or "TV")
            args.audio_monitor = tv_audio.monitor
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            log(e, "- falling back to capturing the laptop output")
    try:
        if args.airplay:
            return run_airplay(args, host, name)
        if args.hls or args.no_cast:
            return run_hls(args, host, name, fd, node)
        return run_mirror(args, host, name, fd, node)
    finally:
        if tv_audio:
            tv_audio.close()
            log("Audio output switched back to", tv_audio.previous or "the previous device")


class MainLoopRunner:
    """GLib main loop + pipeline bus + Ctrl+C handling shared by both modes."""

    def __init__(self, pipeline, verbose):
        self.pipeline, self.verbose = pipeline, verbose
        self.loop = GLib.MainLoop()
        self.stop = threading.Event()
        self.exit_code = 0
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus)
        try:
            gi.require_version("GLibUnix", "2.0")
            from gi.repository import GLibUnix
            add_signal = GLibUnix.signal_add
        except (ValueError, ImportError):
            add_signal = GLib.unix_signal_add
        add_signal(GLib.PRIORITY_DEFAULT, signal.SIGINT, self.shutdown)
        add_signal(GLib.PRIORITY_DEFAULT, signal.SIGTERM, self.shutdown)

    def shutdown(self, *_):
        if not self.stop.is_set():
            self.stop.set()
            GLib.idle_add(self.loop.quit)
        return False

    def _on_bus(self, _bus, msg):
        if msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            if "target not found" in err.message:
                log("Screen sharing was stopped.")  # user ended it from the desktop: not an error
            else:
                log("Pipeline error:", err.message)
                if self.verbose:
                    log(dbg)
                self.exit_code = 1
            self.shutdown()
        elif msg.type == Gst.MessageType.EOS:
            self.shutdown()
        return True

    def start(self):
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            log("Could not start the capture pipeline (run with -v for details).")
            return False
        return True


# --------------------------------------------------------------------------- mode 1: Cast Streaming (low latency)

def video_source(args, fd, node):
    if args.test:
        return ('videotestsrc is-live=true pattern=ball ! '
                'clockoverlay time-format="%H:%M:%S" font-desc="Sans 64" valignment=top ! '
                'timeoverlay font-desc="Sans 64" valignment=bottom')
    fps = args.fps
    return (f"pipewiresrc fd={fd} path={node} do-timestamp=true keepalive-time={max(1000 // fps, 20)} "
            f"always-copy=true ! queue leaky=downstream max-size-buffers=1")


def audio_source(args):
    if args.test:
        return "audiotestsrc is-live=true wave=ticks volume=0.3"
    mon = getattr(args, "audio_monitor", None) or default_monitor_source()
    log("Capturing audio from", mon or "default source")
    dev = f"device={mon} " if mon else ""
    return f"pulsesrc {dev}do-timestamp=true buffer-time=40000 latency-time=10000"


def build_mirror_pipeline(args, codec, w, h, kbps, fd, node, with_audio):
    fps = args.fps
    threads = min(os.cpu_count() or 4, 4)
    if codec == "h264":
        venc = (f"openh264enc bitrate={kbps * 1000} max-bitrate={kbps * 1300} rate-control=bitrate "
                f"usage-type=screen complexity=low gop-size={fps * 10} multi-thread={threads} "
                f"enable-frame-skip=false ! video/x-h264,stream-format=byte-stream,alignment=au ! "
                f"h264parse config-interval=-1 ! video/x-h264,stream-format=byte-stream,alignment=au")
    else:
        venc = (f"vp8enc deadline=1 cpu-used=8 end-usage=cbr target-bitrate={kbps * 1000} "
                f"keyframe-max-dist={fps * 10} lag-in-frames=0 threads={threads} buffer-size=200 "
                f"buffer-initial-size=100 buffer-optimal-size=150 undershoot=95")
    desc = (f"{video_source(args, fd, node)} ! videoconvertscale n-threads=4 add-borders=true ! "
            f"video/x-raw,format=I420,width={w},height={h},pixel-aspect-ratio=1/1 ! "
            f"videorate ! video/x-raw,framerate={fps}/1 ! {venc} ! "
            f"appsink name=vsink sync=false emit-signals=true max-buffers=8")
    if with_audio:
        desc += (f" {audio_source(args)} ! audio/x-raw,rate=48000,channels=2 ! audioconvert ! "
                 f"audioresample ! audio/x-raw,rate=48000,channels=2 ! "
                 f"opusenc bitrate=128000 frame-size=10 ! "
                 f"appsink name=asink sync=false emit-signals=true max-buffers=50")
    if args.verbose:
        log("Pipeline:", desc)
    return Gst.parse_launch(desc)


def run_mirror(args, host, name, fd, node):
    import random

    import castmirror as cm
    gi.require_version("GstVideo", "1.0")
    from gi.repository import GstVideo

    h = args.height
    w = (h * 16 // 9) // 2 * 2
    delay = args.delay
    streams, offers = {}, []
    if args.audio:
        streams["audio"] = cm.RtpStream("audio", "opus", random.randint(1, 500000), cm.PT_AUDIO, 48000, delay)
        offers.append(streams["audio"].offer(len(offers), channels=2, bitRate=128000))
    codecs = ["h264", "vp8"] if args.codec == "auto" else [args.codec]
    for c in codecs:
        s = cm.RtpStream("video", c, random.randint(500001, 1000000), cm.PT_VIDEO, 90000, delay)
        streams[c] = s
        offers.append(s.offer(len(offers), channels=1, maxFrameRate=f"{args.fps * 1000}/1000",
                              maxBitRate=args.bitrate * 1000, resolutions=[{"width": w, "height": h}]))

    STATUS.update(state="connecting")
    log(f"Connecting to {name}...")
    try:
        cc = CastClient(host)
        transport, session_id, answer = cm.negotiate(cc, offers, log, NS_RECEIVER, NS_CONN, _app_from_status)
    except Exception as e:  # noqa: BLE001
        log("Could not start mirroring:", e)
        log("Tip: `tejucast --hls` uses the slower but more compatible video-stream mode.")
        return 1
    if args.verbose:
        log("ANSWER:", json.dumps(answer))

    chosen = [offers[i] for i in answer.get("sendIndexes", []) if 0 <= i < len(offers)]
    vids = [o for o in chosen if o["type"] == "video_source"]
    if not vids:
        log("TV did not accept any of the offered video codecs.")
        cc.close()
        return 1
    vstream = streams[vids[0]["codecName"]]
    astream = streams.get("audio") if any(o["type"] == "audio_source" for o in chosen) else None

    # Respect the TV's limits, if it sent any.
    vcons = (answer.get("constraints") or {}).get("video") or {}
    kbps = args.bitrate
    if vcons.get("maxBitRate"):
        kbps = min(kbps, int(vcons["maxBitRate"]) // 1000)
    dims = vcons.get("maxDimensions") or {}
    if dims.get("height") and dims["height"] < h:
        h = int(dims["height"]) // 2 * 2
        w = (h * 16 // 9) // 2 * 2
    log(f"TV accepted: {vstream.codec.upper()} {w}x{h}@{args.fps}"
        + (" + Opus audio" if astream else "") + f", target delay {delay} ms, UDP port {answer['udpPort']}")

    pipeline = build_mirror_pipeline(args, vstream.codec, w, h, kbps, fd, node, astream is not None)
    runner = MainLoopRunner(pipeline, args.verbose)

    def clock():
        c = pipeline.get_clock()
        if c is None:
            return 0, time.time()
        return max(c.get_time() - pipeline.get_base_time(), 0), time.time()

    active = [s for s in (astream, vstream) if s]
    transport_udp = cm.MirrorTransport(host, int(answer["udpPort"]), active, clock)

    vsink = pipeline.get_by_name("vsink")
    last_key = [0.0]

    def request_keyframe():
        now = time.monotonic()
        if now - last_key[0] < 0.25:
            return
        last_key[0] = now
        vsink.get_static_pad("sink").send_event(
            GstVideo.video_event_new_upstream_force_key_unit(Gst.CLOCK_TIME_NONE, True, 0))

    vstream.on_keyframe_needed = request_keyframe

    def make_cb(stream):
        def on_sample(sink):
            sample = sink.emit("pull-sample")
            buf = sample.get_buffer()
            ok, info = buf.map(Gst.MapFlags.READ)
            if not ok:
                return Gst.FlowReturn.OK
            try:
                data = bytes(info.data)
            finally:
                buf.unmap(info)
            pts = buf.pts if buf.pts != Gst.CLOCK_TIME_NONE else clock()[0]
            key = stream.kind == "audio" or not buf.has_flags(Gst.BufferFlags.DELTA_UNIT)
            stream.send_frame(data, pts, key)
            return Gst.FlowReturn.OK
        return on_sample

    vsink.connect("new-sample", make_cb(vstream))
    if astream:
        pipeline.get_by_name("asink").connect("new-sample", make_cb(astream))

    if not runner.start():
        cc.close()
        transport_udp.close()
        return 1

    def reports():
        if runner.stop.is_set():
            return False
        transport_udp.send_reports()
        return True

    GLib.timeout_add(100, reports)

    def keepalive():
        last_stats = time.time()
        try:
            while not runner.stop.is_set():
                m = cc.recv()
                if time.time() - last_stats > 5:
                    last_stats = time.time()
                    STATUS.update(stats={s.kind: dict(s.stats) for s in active})
                    if args.verbose:
                        log("stats", {s.kind: s.stats for s in active})
                if not m:
                    continue
                if m["ns"] == NS_RECEIVER and m["data"].get("type") == "RECEIVER_STATUS":
                    if _app_from_status(m, cm.MIRROR_APP) is None:
                        log("Mirroring was closed on the TV.")
                        runner.shutdown()
                        return
                elif m["ns"] == cm.NS_WEBRTC and args.verbose:
                    log("TV says:", m["data"])
        except Exception as e:  # noqa: BLE001
            log("Lost connection to TV:", e)
            runner.shutdown()

    th = threading.Thread(target=keepalive, daemon=True)
    th.start()
    STATUS.update(state="streaming", codec=vstream.codec, width=w, height=h, fps=args.fps, delay=delay,
                  audio=bool(astream), streaming_since=time.time())
    log(f"Mirroring to {name}. Ctrl+C to stop.")
    try:
        runner.loop.run()
    finally:
        runner.stop.set()
        log("Stopping...")
        th.join(timeout=3)
        try:
            cc.send("receiver-0", NS_RECEIVER, {"type": "STOP", "sessionId": session_id}, add_id=True)
            time.sleep(0.3)
        except OSError:
            pass
        cc.close()
        pipeline.set_state(Gst.State.NULL)
        transport_udp.close()
    return runner.exit_code


# --------------------------------------------------------------------------- mode 3: AirPlay (via doubletake)

PIN_FIFO = os.path.join(STATUS_DIR, "pin")
_TS = re.compile(r"^\d{4}/\d\d/\d\d \d\d:\d\d:\d\d ")
_PROMPT = re.compile(rb"Enter the (PIN|password)[^:\n]*: ")


def find_doubletake():
    for c in (os.environ.get("TEJUCAST_DOUBLETAKE"), shutil.which("doubletake"),
              os.path.expanduser("~/src/doubletake/bin/doubletake")):
        if c and os.access(c, os.X_OK):
            return c
    return None


def doubletake_supports(exe, flag):
    try:
        out = subprocess.run([exe, "-h"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return f"\n  {flag}" in "\n" + out.stdout + out.stderr


def send_pin(pin):
    try:
        fd = os.open(PIN_FIFO, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        print("No AirPlay pairing is waiting for a PIN.", file=sys.stderr)
        return 1
    try:
        os.write(fd, (pin.strip() + "\n").encode())
    finally:
        os.close(fd)
    return 0


def run_airplay(args, host, name):
    """Drive doubletake (AirPlay mirroring sender) and translate its output into our status."""
    import select
    exe = find_doubletake()
    if not exe:
        log("AirPlay needs doubletake (https://github.com/omarroth/doubletake) on your PATH.")
        return 1
    cmd = [exe, "-target", host, "-hwaccel", "openh264", "-fps", str(args.fps)]
    if not args.audio:
        cmd.append("-no-audio")
    if args.test:
        cmd.append("-test")
    env = dict(os.environ)
    if args.extend:
        if doubletake_supports(exe, "-virtual-display"):
            cmd.append("-virtual-display")  # upstream flag (omarroth/doubletake#81)
        else:
            env["DOUBLETAKE_PORTAL_SOURCE"] = "virtual"  # older builds with patches/doubletake-virtual-monitor.patch
    os.makedirs(STATUS_DIR, exist_ok=True)
    try:
        os.unlink(PIN_FIFO)
    except FileNotFoundError:
        pass
    os.mkfifo(PIN_FIFO, 0o600)
    pin_fd = os.open(PIN_FIFO, os.O_RDONLY | os.O_NONBLOCK)

    STATUS.update(state="connecting", protocol="airplay")
    log(f"Connecting to {name} over AirPlay...")
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            env=env)
    out_fd = proc.stdout.fileno()
    tty = sys.stdin if sys.stdin and sys.stdin.isatty() else None
    buf = b""
    waiting_pin = False
    failed = None

    def give_pin(text):
        nonlocal waiting_pin
        text = text.strip()
        if not text or proc.poll() is not None:
            return
        proc.stdin.write((text + "\n").encode())
        proc.stdin.flush()
        waiting_pin = False
        STATUS.update(state="connecting")
        log("PIN sent, pairing...")

    def handle(line):
        nonlocal failed
        line = _TS.sub("", line).strip()
        if not line:
            return
        if args.verbose:
            log("doubletake:", line)
        if "pairing complete" in line:
            log(f"Paired with {name}.")
        elif "screen capture started at" in line:
            m = re.search(r"at (\d+)x(\d+)", line)
            if m:
                STATUS.update(width=int(m.group(1)), height=int(m.group(2)))
        elif "mirror session ready" in line:
            STATUS.update(state="streaming", codec="airplay", fps=args.fps, audio=args.audio,
                          streaming_since=time.time())
            what = "Extending desktop" if args.extend else "Mirroring"
            log(f"{what} to {name} over AirPlay. Ctrl+C to stop.")
        elif re.search(r"fail|error|refused|denied|unable|cannot", line, re.I):
            failed = line
            if not args.verbose:
                log(line)

    try:
        while True:
            fds = [out_fd, pin_fd] + ([tty] if tty else [])
            r, _, _ = select.select(fds, [], [], 1.0)
            if out_fd in r:
                chunk = os.read(out_fd, 65536)
                if not chunk:
                    break
                buf += chunk
                m = _PROMPT.search(buf)
                if m:
                    for line in buf[:m.start()].split(b"\n"):
                        handle(line.decode(errors="replace"))
                    buf = buf[m.end():]
                    waiting_pin = True
                    STATUS.update(state="pin", message=f"Enter the {m.group(1).decode()} shown on {name}")
                    log(f"Enter the {m.group(1).decode()} shown on {name}"
                        + (" (type it here)" if tty else " (tejucast --pin 1234)"))
                *lines, buf = buf.split(b"\n")
                for line in lines:
                    handle(line.decode(errors="replace"))
            if pin_fd in r:
                data = os.read(pin_fd, 256)
                if data:
                    give_pin(data.decode(errors="replace"))
                else:  # writer closed: reopen so `select` stops reporting EOF
                    os.close(pin_fd)
                    pin_fd = os.open(PIN_FIFO, os.O_RDONLY | os.O_NONBLOCK)
            if tty and tty in r:
                line = tty.readline()
                if waiting_pin:
                    give_pin(line)
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        os.close(pin_fd)
        try:
            os.unlink(PIN_FIFO)
        except FileNotFoundError:
            pass
    code = proc.wait()
    if code != 0 and failed:
        if "pair" in failed.lower():
            failed += " (wrong or expired PIN? try again)"
        STATUS.update(message=failed)
    return 0 if code in (0, -signal.SIGINT) else 1


# --------------------------------------------------------------------------- mode 2: HLS (compatible, ~4 s delay)

def run_hls(args, host, name, fd, node):
    outdir = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), f"tejucast-{os.getpid()}")
    os.makedirs(outdir, exist_ok=True)
    pipeline = build_pipeline(args, outdir, fd, node)
    ip = local_ip_towards(host or "192.0.2.1")
    srv = make_http_server(outdir, args.port)
    url = f"http://{ip}:{args.port}/live.m3u8"
    runner = MainLoopRunner(pipeline, args.verbose)
    if not runner.start():
        shutil.rmtree(outdir, ignore_errors=True)
        return 1
    log("Encoding... stream at", url)

    def caster():
        t0 = time.time()
        while not segments_ready(outdir) and not runner.stop.is_set():
            if time.time() - t0 > 20:
                log("No video segments produced after 20s - is the capture working? (try -v)")
                runner.shutdown()
                return
            time.sleep(0.2)
        if runner.stop.is_set():
            return
        if args.no_cast:
            log("Serving only (--no-cast). Open the URL above in VLC/mpv. Ctrl+C to stop.")
            return
        log(f"Casting to {name}... Ctrl+C to stop.")
        STATUS.update(state="streaming", codec="hls", streaming_since=time.time())
        cast_session(host, url, "Desktop", runner.stop, runner.shutdown)

    th = threading.Thread(target=caster, daemon=True)
    th.start()
    try:
        runner.loop.run()
    finally:
        runner.stop.set()
        log("Stopping...")
        th.join(timeout=5)
        pipeline.set_state(Gst.State.NULL)
        srv.shutdown()
        shutil.rmtree(outdir, ignore_errors=True)
    return runner.exit_code


def read_status():
    try:
        with open(STATUS_FILE) as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = {"state": "stopped"}
    alive = False
    try:
        os.kill(int(st.get("pid", 0)), 0)
        alive = bool(st.get("pid"))
    except (OSError, ValueError):
        pass
    if not alive and st.get("state") not in ("stopped", "error"):
        st["state"] = "stopped"
    return st


def launch_background():
    """Re-run ourselves (minus --background) as a transient systemd user service."""
    argv = [a for a in sys.argv[1:] if a != "--background"]
    subprocess.run(["systemctl", "--user", "stop", UNIT], capture_output=True)
    subprocess.run(["systemctl", "--user", "reset-failed", UNIT], capture_output=True)
    cmd = ["systemd-run", "--user", f"--unit={UNIT}", "--collect", "--quiet",
           "--description=tejucast screen casting", "-p", "KillSignal=SIGTERM",
           sys.executable, os.path.realpath(__file__)] + argv
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr.strip(), file=sys.stderr)
        return 1
    print(f"tejucast started in the background (journalctl --user -u {UNIT} -f to watch, tejucast --stop to stop)")
    return 0


def entry():
    code = 1
    try:
        code = main()
    except KeyboardInterrupt:
        code = 0
    finally:
        if STATUS.enabled:
            if code:
                STATUS.update(state="error")
            else:
                STATUS.update(state="stopped", message="Stopped")
    return code


if __name__ == "__main__":
    sys.exit(entry())
