# tvcast

Stream your Linux desktop to a **Google TV / Chromecast** with low delay: mirror your screen, or use the TV as a **second monitor**. Sound plays through the TV.

tvcast uses the same Cast Streaming protocol as Chrome's "Cast screen". It's a single Python tool that relies only on what your distro already ships (GStreamer, PipeWire, PyGObject, OpenSSL). There's no pip install and nothing to install on the TV.

```
$ tvcast
[tvcast] Target: Living Room TV (192.168.1.40)
[tvcast] Got PipeWire stream (node 131)
[tvcast] Audio output switched to "Living Room TV (tvcast)"
[tvcast] TV accepted: H264 1920x1080@30 + Opus audio, target delay 150 ms
[tvcast] Mirroring to Living Room TV. Ctrl+C to stop.
```

## Features

- **Low delay mirroring.** The TV shows each frame 150 ms after capture by default (`--delay`). There's no buffering phase like with video casting.
- **Extend the desktop** (`--extend`). Creates a virtual 1920×1080 monitor that you can drag windows onto and arrange in your display settings.
- **The TV shows up as a speaker.** While casting, a "*TV name* (tvcast)" output appears in your audio settings and becomes the default, so sound moves from the laptop to the TV. The volume slider controls the TV's volume. Your previous output is restored when you stop.
- **Pick a screen or a window** through the standard Wayland screen-sharing dialog. Your choice is remembered for next time.
- **Handles Wi-Fi packet loss.** Lost packets are re-sent when the TV asks for them, and the TV can request a fresh keyframe.
- **HLS fallback** (`--hls`). If mirroring doesn't work on your device, this mode has the TV's media player play a live video stream. It's more compatible but has a 3–5 s delay.

## Requirements

- Linux on **Wayland** with a ScreenCast portal (tested on **KDE Plasma 6.8**, Fedora 45, aarch64). `--extend` needs a portal that supports virtual monitors (Plasma 6, recent GNOME).
- PipeWire with `pactl` (pipewire-pulseaudio)
- Python 3 with PyGObject (`gi`)
- GStreamer 1.x: `pipewiresrc`, `openh264enc` (or `vp8enc`), `opusenc`, `h264parse`, `videoconvertscale`
- OpenSSL `libcrypto`, and `avahi-browse` for finding the TV
- A Google TV, Android TV or Chromecast on the same network

On Fedora:

```sh
sudo dnf install python3-gobject gstreamer1-plugins-good gstreamer1-plugins-bad-free \
    gstreamer1-plugin-openh264 gstreamer1-plugins-base pipewire-gstreamer avahi-tools
```

## Install

```sh
git clone https://github.com/adurysurya/tvcast.git
ln -s "$PWD/tvcast/tvcast.py" ~/.local/bin/tvcast
```

## Usage

```sh
tvcast                 # mirror a screen/window to the first TV found
tvcast --extend        # use the TV as a second monitor
tvcast --list          # list Cast devices
tvcast -d bedroom      # pick a TV by name (substring)
tvcast --test          # send a test pattern + ticking audio
tvcast --forget        # ask again which screen/window to share
```

| Option | Default | |
|---|---|---|
| `--delay MS` | 150 | Target delay. Lower means snappier, higher handles bad Wi-Fi better. |
| `--height` | 1080 | Output height (16:9 output, letterboxed if your screen isn't 16:9) |
| `--fps` | 30 | Frame rate |
| `--bitrate KBPS` | 6000 | Video bitrate |
| `--codec` | auto | `h264` or `vp8`. By default both are offered and the TV picks. |
| `--laptop-audio` | | Keep sound on the laptop as well as sending it to the TV |
| `--no-audio` | | Video only |
| `--hls` | | Use the HLS fallback mode |
| `-v` | | Verbose: pipeline, negotiation, per-stream stats |

Stop with **Ctrl+C**, or press Back on the TV remote.

## How it works

1. **Capture.** The xdg-desktop-portal ScreenCast API hands over a PipeWire stream of a monitor, a window, or a new virtual monitor.
2. **Encode.** GStreamer turns it into H.264 (OpenH264, screen-content mode) or VP8, and encodes audio from the virtual TV speaker as Opus in 10 ms frames.
3. **Negotiate.** A small built-in Cast v2 client (TLS + hand-rolled protobuf, port 8009) launches the TV's mirroring receiver and sends an `OFFER` on `urn:x-cast:com.google.cast.webrtc`. The TV answers with the codecs it accepts and a UDP port.
4. **Stream** (`castmirror.py`). Each encoded frame is encrypted with AES-128-CTR, using a per-frame nonce built from the frame ID and the IV mask. It's split into RTP packets with the Cast header (frame ID, packet ID, keyframe and reference frame IDs) and sent over UDP. RTCP Sender Reports let the TV sync audio and video. The TV's Cast feedback (ACKs and NACKs) triggers retransmits, and its picture-loss requests force a keyframe.

The wire format follows Google's open-source [Open Screen](https://chromium.googlesource.com/openscreen/) reference implementation (`cast/streaming`).

## Limitations

- It's only been tested on one Google TV model (KDE Plasma 6.8, Fedora 45 on Apple Silicon). Reports from other setups are welcome.
- No hardware encoding yet. OpenH264 1080p30 on the CPU is fine on modern machines.
- Fixed bitrate: there's no bandwidth adaptation yet.
- On KDE the virtual monitor is fixed at 1920×1080.
- Running tvcast on X11 isn't supported (it needs the Wayland portal).

## License

MIT, see [LICENSE](LICENSE).
