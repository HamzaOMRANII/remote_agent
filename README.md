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
- ⏳ `camera` (M2, WebRTC via `aiortc`), `scripts` / `shell` (M3), `media` (M6).

## Install

**Raspberry Pi:**
```bash
sudo apt install python3-gpiozero        # or: pip install gpiozero
pip install websockets
cp config.example.json config.json       # set server URL, token, your pins
python3 agent/remote_agent.py --config config.json
```

**Laptop (mock GPIO, for testing):**
```bash
pip install websockets
cp config.example.json config.json       # point server at ws://127.0.0.1:16010/agent
python3 agent/remote_agent.py --config config.json
```

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

## Run as a service (later)

On the Pi, a systemd unit keeps it running and reconnecting across reboots
(added with the M5/M6 polish). Until then, run it under `tmux` or `systemd-run`.

## Security

Each device has its own token, created in the back office and revocable there
(rotate / disable / delete), sent only over TLS (`wss://`). Keep `config.json`
(it holds the token) out of git — it already is, via `.gitignore`. In-person
pairing + 2FA are the remaining M4 work.
