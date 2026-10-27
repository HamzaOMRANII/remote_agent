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

    def capabilities(self):
        caps = []
        pins = self.cfg.get("gpio", {}).get("pins", [])
        if pins:
            caps.append({"cap": "gpio", "pins": pins})
        return caps

    def handle_action(self, msg):
        cap = msg.get("cap")
        op = msg.get("do")
        args = msg.get("args") or {}
        try:
            if cap == "gpio" and op == "set":
                data = self.gpio.set(int(args["pin"]), args["value"])
                return {"ok": True, "data": data}
            return {"ok": False, "error": f"unsupported: {cap}/{op}"}
        except Exception as exc:  # noqa: BLE001 — report any failure back to the UI
            return {"ok": False, "error": str(exc)}

    def run_once(self):
        ws = WebSocketClient(self.url, insecure=self.insecure)
        ws.connect()
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
                    result = self.handle_action(msg)
                    ws.send(json.dumps({"type": "result", "reqId": msg.get("reqId"), **result}))
        finally:
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
