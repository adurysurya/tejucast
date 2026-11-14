# Teju Cast

Stream your Linux desktop to a **Google TV / Chromecast** or an **Apple TV / AirPlay** receiver with low delay: mirror your screen, or use the TV as a **second monitor**. Sound plays through the TV.

Teju Cast uses the same Cast Streaming protocol as Chrome's "Cast screen". It's a single Python tool that relies only on what your distro already ships (GStreamer, PipeWire, PyGObject, OpenSSL). There's no pip install and nothing to install on the TV.

```
$ tejucast
[tejucast] Target: Living Room TV (192.168.1.40)
[tejucast] Got PipeWire stream (node 131)
[tejucast] Audio output switched to "Living Room TV (Teju Cast)"
[tejucast] TV accepted: H264 1920x1080@30 + Opus audio, target delay 150 ms
[tejucast] Mirroring to Living Room TV. Ctrl+C to stop.
```

## Features

- **Low delay mirroring.** The TV shows each frame 150 ms after capture by default (`--delay`). There's no buffering phase like with video casting.
- **Extend the desktop** (`--extend`). Creates a virtual 1920×1080 monitor that you can drag windows onto and arrange in your display settings.
- **The TV shows up as a speaker.** While casting, a "*TV name* (Teju Cast)" output appears in your audio settings and becomes the default, so sound moves from the laptop to the TV. The volume slider controls the TV's volume. Your previous output is restored when you stop.
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
git clone https://github.com/adurysurya/tejucast.git
ln -s "$PWD/tejucast/tejucast.py" ~/.local/bin/tejucast
```

## Usage

```sh
tejucast                 # mirror a screen/window to the first TV found
tejucast --extend        # use the TV as a second monitor
tejucast --list          # list Cast devices
tejucast -d bedroom      # pick a TV by name (substring)
tejucast --test          # send a test pattern + ticking audio
tejucast --forget        # ask again which screen/window to share
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

### Running in the background

```sh
tejucast --background --extend   # start as a systemd user service (replaces a running one)
tejucast --status                # current state as JSON
tejucast --stop
```

## AirPlay (Apple TV, AirPlay TVs, Macs)

Teju Cast can also send to **AirPlay** receivers. It uses [doubletake](https://github.com/omarroth/doubletake), an open-source AirPlay mirroring sender, which has a delay of about 75–85 ms. Teju Cast finds AirPlay devices alongside Cast ones, moves sound to the TV, reports status and runs the tray widget. doubletake does the AirPlay part.

```sh
tejucast --airplay                       # mirror to the first AirPlay receiver found
tejucast --airplay -d "living room" -e   # extend the desktop over AirPlay
tejucast --pin 1234                      # send the pairing PIN (or type it in the terminal)
```

The first time, the receiver shows a **PIN** on screen. Type it in the terminal, run `tejucast --pin`, or enter it in the tray popup. After that the pairing is remembered.

Setup:

1. Build doubletake (needs Go 1.25+):
   ```sh
   git clone https://github.com/omarroth/doubletake ~/src/doubletake
   cd ~/src/doubletake && make && ln -s "$PWD/bin/doubletake" ~/.local/bin/doubletake
   ```
2. **Extend over AirPlay** needs doubletake's `-virtual-display` flag, proposed upstream in [omarroth/doubletake#81](https://github.com/omarroth/doubletake/pull/81). Until it's merged, apply [`patches/doubletake-virtual-monitor.patch`](patches/doubletake-virtual-monitor.patch) with `git apply` before building. Teju Cast detects the flag automatically. Mirroring works with stock doubletake.

Tested with a Google TV that also supports AirPlay. That TV doesn't use FairPlay, so doubletake connects through pairing alone. Apple TVs are covered by doubletake's own testing.

## KDE Plasma system tray widget

The `plasma/` folder contains a **Teju Cast** widget that sits in the system tray next to the audio, Wi-Fi and battery icons. Clicking it opens a popup that works like the Bluetooth and network ones:

- It lists the TVs on your network, both Google Cast and AirPlay. Each row has a **Mirror** / **Stop** button, and you can expand it for **Extend Desktop**. TVs that support both protocols get extra AirPlay options.
- A PIN box appears when an AirPlay receiver needs pairing.
- It shows live status, such as "Mirroring · 1080p · 150 ms" or "Choose what to share in the dialog…".
- It has settings for sound (TV only, TV and computer, or off), quality (1080p or 720p) and delay.
- The tray icon gets a green dot while casting. Middle-click the icon to stop.

Install it (needs `tejucast` on your `PATH`, e.g. `~/.local/bin/tejucast`):

```sh
kpackagetool6 -t Plasma/Applet -i plasma      # use -u to upgrade later
```

Then go to **System Tray Settings → Entries**, find **Teju Cast** and set it to *Always shown*.

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
- Running tejucast on X11 isn't supported (it needs the Wayland portal).

## License

MIT, see [LICENSE](LICENSE).
