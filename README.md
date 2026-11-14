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
- ✅ M9: this computer's own cameras + **Add** network cameras from the dashboard with only their password.

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

## Cameras — the easy way (M9)

1. Install video once: `pip install aiortc` (the Windows installer in `service\` does it for you).
2. Start the agent. **This computer's own cameras** (built-in webcam, any USB camera) appear by themselves in
   the device's camera list on the dashboard; one plugged in later shows up within a minute.
3. On the dashboard, in the device's **Cameras** panel: **This is my network** (once, on your home Wi-Fi), then
   **Scan for cameras**. Each network camera gets an **Add** button: type only the camera's own username and
   password. The agent finds the stream link itself (ONVIF, else the usual links of the brand), checks it, and
   saves it in `config.json` (mode 600, never sent to the server). **Remove** takes it away.

If a camera answers but no video is found: turn on **RTSP / ONVIF** in the camera's own app (Tapo, Ezviz, Imou
and some others ship with it off), then press Add again. To hide this computer's cameras:
`"local_cameras": false` in `config.json`.

The manual way below still works.

## IP cameras (RTSP → dashboard)

Relay the Wi-Fi/IP cameras on your local network to the dashboard. The agent (on
a laptop, PC or Pi that stays on, **on the same network as the cameras**) pulls
each camera's RTSP stream and relays it over WebRTC (the camera is only pulled
while someone watches).

**1. Install the media library** (only needed for cameras):
```bash
pip install aiortc          # bundles ffmpeg via PyAV; nothing else to install
```

**2. Find your cameras (M7):**
```bash
python3 agent/remote_agent.py --scan
```
Asks the network who speaks ONVIF (brand, model, service address) and checks the
usual camera ports (554/8554 video, 80/8080/443 web page, 8000 Hikvision, 37777
Dahua, 1935 RTMP) on this computer's /24. Private addresses only; it never tries a
password. To scan **from the dashboard** too (button *Scan for cameras*), list
your own networks in `config.json`; the button only appears while the computer
is on one of them:
```json
"discovery": { "networks": ["192.168.1.0/24"] }
```

**3. Add them to `config.json` — no extra device access needed:**
```json
"cameras": [
  { "id": "front-door", "name": "Front door",
    "rtsp": "rtsp://user:password@192.168.1.50:554/Streaming/Channels/102" },
  { "id": "garden", "name": "Garden",
    "rtsp": "rtsp://user:password@192.168.1.51:554/cam/realmonitor?channel=1&subtype=1" }
]
```
Restart the agent. The cameras appear in **this device's camera picker** on the
dashboard (one device, many cameras). Prefer each camera's sub stream (lower
resolution, e.g. `/102` on Hikvision) to save your home upload.

*Older setup, still supported:* an entry with its own `"token"` (from **Devices
→ Add device**) appears as its own device instead.

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

## Start by itself (M8a)

After pairing, install the agent as a service once: it then starts by itself and comes back after a crash or
a reboot. From this folder:

| System | Install | Remove | Logs |
|---|---|---|---|
| Linux / Raspberry Pi | `sudo ./service/install-linux.sh` | `sudo ./service/install-linux.sh --uninstall` | `journalctl -u remote-agent -f` |
| Windows | `powershell -ExecutionPolicy Bypass -File service\install-windows.ps1` | same with `-Uninstall` | `logs\agent.log` |
| macOS | `./service/install-macos.sh` | `./service/install-macos.sh --uninstall` | `~/Library/Logs/remote-agent.log` |

- **Linux / Pi:** a systemd service, started at boot once the network is up (no sign-in needed), restarted 5 s
  after any crash, run as the user who called `sudo` (`--user NAME` to choose). `--print` shows the unit only.
- **Windows:** a scheduled task that starts when you sign in, restarts every minute after a failure and keeps
  running on battery. Install real Python from python.org (the Microsoft Store placeholder is skipped).
- **macOS:** a LaunchAgent that starts when you sign in and is restarted if it stops.
- After changing `config.json`, restart it: `sudo systemctl restart remote-agent` (Linux), sign out/in or
  *Task Scheduler → Remote agent → End/Run* (Windows), `./service/install-macos.sh` again (macOS).

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
