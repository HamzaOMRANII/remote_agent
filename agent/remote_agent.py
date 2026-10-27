#!/usr/bin/env python3
"""
Remote device agent.

Dials OUT to remote.omranisystems.com over a WebSocket (no inbound ports),
authenticates with the shared agent token, advertises its capabilities, and
handles actions the dashboard sends. One codebase for the Raspberry Pi and the
laptop; GPIO is only active on the Pi (and falls back to a mock elsewhere so
the control path can be tested without hardware).

M1 scope: the `gpio` capability (digital pin = light, PWM pin = servo).
Camera / scripts / shell arrive in later milestones.

Run:  python3 agent/remote_agent.py --config config.json
Deps: see requirements.txt  (websockets; gpiozero only on the Pi)
"""

import argparse
import asyncio
import json
import os
import sys

try:
    import websockets
except ImportError:
    sys.exit("missing dependency: pip install websockets")

from gpio import GpioController  # noqa: E402  (local module, same folder)


def load_config(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


class Agent:
    def __init__(self, cfg):
        self.cfg = cfg
        self.device = cfg["device"]
        self.token = cfg["token"]
        self.url = cfg["server"]
        self.gpio = GpioController(cfg.get("gpio", {}).get("pins", []))

    def capabilities(self):
        caps = []
        pins = self.cfg.get("gpio", {}).get("pins", [])
        if pins:
            caps.append({"cap": "gpio", "pins": pins})
        return caps

    async def handle_action(self, msg):
        """Return a result dict for an incoming action message."""
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

    async def run_once(self):
        async with websockets.connect(self.url, max_size=2 ** 20) as ws:
            await ws.send(json.dumps({
                "type": "hello",
                "token": self.token,
                "device": self.device,
                "capabilities": self.capabilities(),
            }))
            print(f"[agent] connected as {self.device['id']} -> {self.url}")

            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                mtype = msg.get("type")
                if mtype == "hello-ok":
                    print("[agent] registered; capabilities:",
                          ", ".join(c["cap"] for c in self.capabilities()) or "(none)")
                elif mtype == "action":
                    result = await self.handle_action(msg)
                    await ws.send(json.dumps({"type": "result", "reqId": msg.get("reqId"), **result}))

    async def run_forever(self):
        delay = 1
        while True:
            try:
                await self.run_once()
            except Exception as exc:  # noqa: BLE001 — reconnect on any drop
                print(f"[agent] disconnected: {exc}")
            print(f"[agent] reconnecting in {delay}s…")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15)


def main():
    ap = argparse.ArgumentParser(description="Remote device agent")
    ap.add_argument("--config", default=os.path.join(os.path.dirname(__file__), "..", "config.json"))
    args = ap.parse_args()
    cfg = load_config(args.config)
    agent = Agent(cfg)
    try:
        asyncio.run(agent.run_forever())
    except KeyboardInterrupt:
        print("\n[agent] bye")
    finally:
        agent.gpio.close()


if __name__ == "__main__":
    main()
