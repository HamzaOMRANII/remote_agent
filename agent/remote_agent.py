#!/usr/bin/env python3
"""
Remote device agent.

Dials OUT to remote.omranisystems.com over a WebSocket (no inbound ports),
authenticates with its own per-device token (created in the Remote back office),
advertises its capabilities, and handles the actions the dashboard sends.

Zero dependencies: the WebSocket client is pure standard library
(agent/ws_client.py), so this runs on any device with Python 3.8+ — no pip
install. gpiozero is used only if present (real GPIO on a Pi); otherwise GPIO
runs in mock mode.

M1 scope: the `gpio` capability (digital pin = light, PWM pin = servo).
Camera / scripts / shell arrive in later milestones.

Run:  python3 agent/remote_agent.py --config config.json

Pair (M4): instead of copying the token, open "Pair (QR)" on the device card in
the dashboard and run, once:
      python3 agent/remote_agent.py --pair ABCD-EFGH --url https://remote.omranisystems.com
The one-time code is exchanged over HTTPS for this device's credentials, which
are merged into config.json (gpio / scripts / shell settings are kept).
"""

import argparse
import json
import os
import platform
import sys
import time
import urllib.error
import urllib.request

from ws_client import WebSocketClient, WebSocketError
from gpio import GpioController
from shell import ShellManager
from camera import CameraManager, validate_cameras
import scan


def load_config(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def pair(code, base_url, path):
    """Claim a one-time pairing code and write the credentials into config.json."""
    cfg = {}
    if os.path.exists(path):
        cfg = load_config(path)
    if not base_url:
        server = cfg.get("server", "wss://remote.omranisystems.com/agent")
        base_url = server.replace("wss://", "https://").replace("ws://", "http://").rsplit("/agent", 1)[0]
    req = urllib.request.Request(
        base_url.rstrip("/") + "/api/pair/claim",
        data=json.dumps({"code": code}).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "remote-agent"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            got = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            msg = json.loads(exc.read().decode()).get("error")
        except Exception:  # noqa: BLE001
            msg = None
        sys.exit(f"[agent] pairing failed: {msg or exc}")
    except urllib.error.URLError as exc:
        sys.exit(f"[agent] pairing failed: cannot reach {base_url} ({exc.reason})")

    device = cfg.get("device", {})
    device.update({"id": got["device"]["id"], "name": got["device"].get("name") or got["device"]["id"]})
    device.setdefault("platform", platform.system().lower() or "linux")
    cfg.update({"server": got["server"], "token": got["token"], "device": device})
    cfg.pop("_comment", None)
    cfg.setdefault("gpio", {"pins": []})

    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # holds a secret
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    print(f"[agent] paired as {device['id']} -> wrote {os.path.abspath(path)}")


class Agent:
    def __init__(self, cfg):
        self.cfg = cfg
        self.device = cfg["device"]
        self.token = cfg["token"]
        self.url = cfg["server"]
        self.insecure = bool(cfg.get("insecure", False))
        self.gpio = GpioController(cfg.get("gpio", {}).get("pins", []))
        self.scripts = cfg.get("scripts", [])
        self.shell_enabled = bool(cfg.get("shell", {}).get("enabled", False))
        self.shellmgr = None  # created per connection (needs the live ws)

    def capabilities(self):
        caps = []
        pins = self.cfg.get("gpio", {}).get("pins", [])
        if pins:
            caps.append({"cap": "gpio", "pins": pins})
        if self.scripts:
            # advertise id + label only; commands stay on the device
            caps.append({"cap": "scripts", "scripts": [{"id": s["id"], "label": s.get("label", s["id"])} for s in self.scripts]})
        if self.shell_enabled:
            caps.append({"cap": "shell"})
        return caps

    def _reply(self, ws, req, body):
        ws.send(json.dumps({"type": "result", "reqId": req, **body}))

    def dispatch(self, ws, msg):
        cap, op, req = msg.get("cap"), msg.get("do"), msg.get("reqId")
        args = msg.get("args") or {}
        try:
            if cap == "gpio" and op == "set":
                self._reply(ws, req, {"ok": True, "data": self.gpio.set(int(args["pin"]), args["value"])})
            elif cap == "scripts" and op == "run":
                self.shellmgr.run_script(req, args.get("id"))          # streams + sends its own result
            elif cap == "shell" and op == "open":
                self.shellmgr.open(req, args.get("sid"))               # streams + sends its own result
            elif cap == "shell" and op == "input":
                self.shellmgr.input(args.get("sid"), args.get("data", "")); self._reply(ws, req, {"ok": True})
            elif cap == "shell" and op == "resize":
                self.shellmgr.resize(args.get("sid"), args.get("cols", 80), args.get("rows", 24)); self._reply(ws, req, {"ok": True})
            elif cap == "shell" and op == "close":
                self.shellmgr.close(args.get("sid")); self._reply(ws, req, {"ok": True})
            else:
                self._reply(ws, req, {"ok": False, "error": f"unsupported: {cap}/{op}"})
        except Exception as exc:  # noqa: BLE001 — surface any failure to the UI
            self._reply(ws, req, {"ok": False, "error": str(exc)})

    def run_once(self):
        ws = WebSocketClient(self.url, insecure=self.insecure)
        ws.connect()
        self.shellmgr = ShellManager(ws, self.scripts, self.shell_enabled)
        ws.send(json.dumps({
            "type": "hello",
            "token": self.token,
            "device": self.device,
            "capabilities": self.capabilities(),
        }))
        print(f"[agent] connected as {self.device['id']} -> {self.url}")
        try:
            while True:
                raw = ws.recv()
                if raw is None:
                    break
                try:
                    msg = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                mtype = msg.get("type")
                if mtype == "hello-ok":
                    print("[agent] registered; capabilities:",
                          ", ".join(c["cap"] for c in self.capabilities()) or "(none)")
                elif mtype == "action":
                    self.dispatch(ws, msg)
        finally:
            if self.shellmgr:
                self.shellmgr.shutdown()
            ws.close()

    def run_forever(self):
        delay = 1
        while True:
            try:
                self.run_once()
            except (WebSocketError, OSError) as exc:
                print(f"[agent] disconnected: {exc}")
            except Exception as exc:  # noqa: BLE001
                print(f"[agent] error: {exc}")
            print(f"[agent] reconnecting in {delay}s…")
            time.sleep(delay)
            delay = min(delay * 2, 15)


def main():
    ap = argparse.ArgumentParser(description="Remote device agent")
    ap.add_argument("--config", default=os.path.join(os.path.dirname(__file__), "..", "config.json"))
    ap.add_argument("--pair", metavar="CODE", help="one-time pairing code from the dashboard (Pair (QR))")
    ap.add_argument("--url", help="dashboard URL for --pair (default: from config, else remote.omranisystems.com)")
    ap.add_argument("--pair-only", action="store_true", help="with --pair: write config.json and exit")
    ap.add_argument("--scan", action="store_true", help="find IP cameras on the local network and exit")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(line_buffering=True)  # prompt logs under systemd / redirect
    except Exception:
        pass
    if args.scan:
        scan.run()
        return
    if args.pair:
        pair(args.pair, args.url, args.config)
        if args.pair_only:
            return
    cfg = load_config(args.config)

    cameras = cfg.get("cameras", [])
    problems = validate_cameras(cameras)
    if problems:
        sys.exit("[agent] camera config errors:\n  " + "\n  ".join(problems))
    cams = CameraManager(cfg["server"], bool(cfg.get("insecure", False)), cameras)
    cams.start()

    agent = Agent(cfg)
    try:
        agent.run_forever()
    except KeyboardInterrupt:
        print("\n[agent] bye")
    finally:
        cams.stop()
        agent.gpio.close()


if __name__ == "__main__":
    main()
