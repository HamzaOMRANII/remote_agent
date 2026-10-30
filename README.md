# remote_agent

The device agent for [Remote](https://github.com/HamzaOMRANII/remote). Install it
on a device you own (Raspberry Pi, laptop); it **dials out** to
`remote.omranisystems.com` over a WebSocket — no inbound ports — authenticates
with its own per-device token (created in the Remote back office), advertises its
capabilities, and handles the actions the dashboard sends.

One codebase for Pi + laptop, with **zero dependencies** — the WebSocket client
is pure Python standard library (`agent/ws_client.py`), so it runs on any device
with Python 3.8+ (no pip install). On a Pi it drives real GPIO via `gpiozero`;
anywhere else it runs GPIO in **mock mode** so the whole path can be tested
without hardware.

## Status — M1

- ✅ Connect + auto-reconnect, `hello` handshake, capability advertisement.
- ✅ `gpio` capability: digital pin (light on/off) and PWM pin (servo angle).
- ✅ `scripts` / `shell`, one-time **pairing**.
- ✅ `camera`: relay **local IP cameras** (RTSP) to the dashboard — see below.

## Install

No pip packages needed (Python 3.8+, stdlib only). On a Raspberry Pi, add
`sudo apt install python3-gpiozero` for real GPIO; elsewhere GPIO runs in mock mode.

**Pair (recommended):** in the dashboard, open the device card → **Pair (QR)**,
then on the device, from this folder:
```bash
python3 agent/remote_agent.py --pair ABCD-EFGH --url https://remote.omranisystems.com
```
The one-time code (10 min) is exchanged over HTTPS for the device's credentials,
which are written into `config.json` (mode 600; existing gpio/scripts/shell
settings are kept), and the agent starts. Add `--pair-only` to just write the
config. Later runs: `python3 agent/remote_agent.py`.

**Manual:** `cp config.example.json config.json`, paste the token from
**Reveal config**, then `python3 agent/remote_agent.py --config config.json`.

## Config (`config.json`, gitignored)

```json
{
  "server": "wss://remote.omranisystems.com/agent",
  "token":  "one-time per-device token from Devices -> Add device",
  "device": { "id": "rpi-3", "name": "Raspberry Pi 3", "platform": "linux" },
  "gpio": { "pins": [
    { "id": 17, "kind": "digital", "label": "Desk light" },
    { "id": 18, "kind": "pwm", "label": "Servo", "min_angle": -90, "max_angle": 90 }
  ] }
}
```

Pin `kind`: `digital` renders a switch in the dashboard; `pwm` renders a servo
slider (`min_angle`/`max_angle`).

## IP cameras (RTSP → dashboard)

Relay the Wi-Fi/IP cameras on your local network to the dashboard. The agent (on
a laptop, PC or Pi that stays on, **on the same network as the cameras**) pulls
each camera's RTSP stream and relays it over WebRTC. Each camera shows up as its
own tile, just like a phone. The server and dashboard need no changes.

**1. Install the media library** (only needed for cameras):
```bash
pip install aiortc          # bundles ffmpeg via PyAV; nothing else to install
```

**2. Find your cameras' addresses:**
```bash
python3 agent/remote_agent.py --scan
```
This lists devices on your network with the camera port (554) open. It can't log
in to them — you still need each camera's username/password and stream path.

**3. Give each camera its own device access:** in the dashboard, **Devices → Add
device**, once per camera. Copy the `id` and `token`.

**4. Add a `cameras` list to `config.json`:**
```json
"cameras": [
  { "id": "front-door", "name": "Front door",
    "token": "the-token-for-this-camera",
    "rtsp": "rtsp://user:password@192.168.1.50:554/Streaming/Channels/101",
    "audio": false }
]
```
Restart the agent. Each camera appears online in the dashboard within seconds;
the stream opens when you click it (the camera is only pulled while watched).

**RTSP paths by brand** (put your camera's user/password and LAN IP in):
| Brand | Typical main-stream URL |
|-------|-------------------------|
| Hikvision | `rtsp://user:pass@IP:554/Streaming/Channels/101` |
| Dahua | `rtsp://user:pass@IP:554/cam/realmonitor?channel=1&subtype=0` |
| Reolink | `rtsp://user:pass@IP:554/h264Preview_01_main` |
| TP-Link Tapo | `rtsp://user:pass@IP:554/stream1` |
| Imou / EZVIZ | `rtsp://user:pass@IP:554/cam/realmonitor?channel=1&subtype=0` |
| ONVIF (generic) | use the camera's app, or try the paths above |

Enable RTSP in the camera's own app first (often under *Advanced / Network /
RTSP*), and create a viewer account there rather than using the admin login.
If a camera is slow or drops, set `"transport": "udp"` on that entry.

## Run as a service (later)

On the Pi, a systemd unit keeps it running and reconnecting across reboots
(added with the M5/M6 polish). Until then, run it under `tmux` or `systemd-run`.

## Security

Each device has its own token, created in the back office and revocable there
(rotate / disable / delete), sent only over TLS (`wss://`). Keep `config.json`
(it holds the token) out of git — it already is, via `.gitignore`. In-person
pairing + 2FA are the remaining M4 work.

## Scripts & shell (M3)

- **scripts** — add allow-listed commands to `config.json`; each shows as a
  button on the dashboard and streams its output. Only the id/label are sent to
  the server — the command stays on the device.
- **shell** — set `"shell": { "enabled": true }` to offer an interactive
  terminal. It's **off by default**, **admin-only** on the dashboard, gated
  behind a password re-prompt, and every command is audit-logged. Uses a pty
  (POSIX only).
