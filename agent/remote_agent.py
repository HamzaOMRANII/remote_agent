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
"""

import argparse
import json
import os
import sys
import time

from ws_client import WebSocketClient, WebSocketError
from gpio import GpioController
from shell import ShellManager


def load_config(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


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
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(line_buffering=True)  # prompt logs under systemd / redirect
    except Exception:
        pass
    cfg = load_config(args.config)
    agent = Agent(cfg)
    try:
        agent.run_forever()
    except KeyboardInterrupt:
        print("\n[agent] bye")
    finally:
        agent.gpio.close()


if __name__ == "__main__":
    main()
