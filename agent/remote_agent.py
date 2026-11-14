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
import ipaddress
import json
import os
import platform
import sys
import threading
import time
import urllib.error
import urllib.request

from ws_client import WebSocketClient, WebSocketError
from gpio import GpioController
from shell import ShellManager
from camera import CameraManager, validate_cameras
import camfind
import localcams
import scan


def load_config(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_config(path, cfg):
    """Atomic write, mode 600 (it holds the device token and the cameras' passwords)."""
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


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

    save_config(path, cfg)  # holds a secret: mode 600
    print(f"[agent] paired as {device['id']} -> wrote {os.path.abspath(path)}")


class Agent:
    def __init__(self, cfg, cams=None, config_path=None):
        self.cfg = cfg
        self.config_path = config_path
        self.cams = cams  # CameraManager: attached cameras show in this device's camera picker (M7)
        self.ws = None
        self._cfg_lock = threading.Lock()
        self.discovery_networks = (cfg.get("discovery") or {}).get("networks", [])
        self._scanning = threading.Lock()
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
        camera = self.cams.capability() if self.cams else None
        if camera:
            caps.append(camera)
        # camera discovery (M7) + adding cameras from the dashboard (M9). The network scan only runs on one of the
        # owner's networks; on another private network the dashboard offers "This is my network" (discovery/trust).
        caps.append(self._discovery_cap())
        return caps

    def _discovery_cap(self):
        trusted = scan.trusted_network(self.discovery_networks)
        if trusted:
            net = {"name": str(trusted[1]), "subnet": str(trusted[1]), "trusted": True}
        else:
            ips = scan.local_ips()
            sub = str(ipaddress.IPv4Network(f"{ips[0]}/24", strict=False)) if ips else ""
            net = {"name": sub, "subnet": sub, "trusted": False}
        added = [{"id": c["id"], "name": c.get("name", c["id"]), "ip": c.get("ip", "")}
                 for c in self.cfg.get("cameras", []) if not c.get("token")]
        local = [{"id": d.id, "name": d.name} for d in (self.cams.devices if self.cams else []) if d.source == "local"]
        return {"cap": "discovery", "network": net, "canAdd": True, "added": added, "local": local,
                "media": bool(self.cams and camera_ok())}

    def send_caps(self):
        ws = self.ws
        if ws:
            try:
                ws.send(json.dumps({"type": "caps", "capabilities": self.capabilities()}))
            except Exception:  # noqa: BLE001 — the next hello carries them anyway
                pass

    def refresh_local(self):
        """Re-lists this computer's cameras (one plugged in / out). True if the list changed."""
        if not self.cams or self.cfg.get("local_cameras") is False:
            return False
        now = {c["id"]: c for c in localcams.list_local()}
        have = {d.id for d in self.cams.devices if d.source == "local"}
        for cid in have - set(now):
            self.cams.remove(cid)
        for cid in set(now) - have:
            self.cams.add(now[cid])
        return have != set(now)

    def _scan(self, ws, req):
        """discovery/scan: this computer's cameras, then every camera on the network (streamed as found)."""
        if not self._scanning.acquire(blocking=False):
            self._reply(ws, req, {"ok": False, "error": "A scan is already running."})
            return
        try:
            if self.refresh_local():
                self.send_caps()
            local = [{"id": d.id, "name": d.name} for d in (self.cams.devices if self.cams else []) if d.source == "local"]
            trusted = scan.trusted_network(self.discovery_networks)
            if not trusted:
                self._reply(ws, req, {"ok": True, "data": {"hosts": [], "local": local, "untrusted": True}})
                return
            ip, net = trusted
            targets = scan.subnet(ip, net.prefixlen)
            hosts = scan.discover(targets, on_update=lambda h: ws.send(json.dumps({"type": "stream", "reqId": req, "chunk": h})))
            self._reply(ws, req, {"ok": True, "data": {"hosts": hosts, "local": local}})
        except Exception as exc:  # noqa: BLE001
            self._reply(ws, req, {"ok": False, "error": str(exc)})
        finally:
            self._scanning.release()

    def _trust(self, ws, req):
        """discovery/trust: the owner marks the network this computer is on as theirs (saved in config.json)."""
        ips = scan.local_ips()
        if not ips:
            self._reply(ws, req, {"ok": False, "error": "This computer isn't on a home/office network (no private address)."})
            return
        net = str(ipaddress.IPv4Network(f"{ips[0]}/24", strict=False))
        with self._cfg_lock:
            nets = self.cfg.setdefault("discovery", {}).setdefault("networks", [])
            if net not in nets:
                nets.append(net)
            self.discovery_networks = nets
            self._save()
        self.send_caps()
        self._reply(ws, req, {"ok": True, "data": {"network": net}})

    def _add_camera(self, ws, req, args):
        """discovery/add: the owner gives the camera's own username/password; the stream link is found here."""
        try:
            ip = str(args.get("ip", ""))
            if not scan.is_private(ip):
                raise ValueError("Only cameras on your own network can be added.")
            if not scan.trusted_network(self.discovery_networks):
                raise ValueError("Cameras can only be added on your network.")
            if not (self.cams and camera_ok()):
                raise ValueError("Video isn't installed on this computer yet: run  pip install aiortc  and restart the agent.")
            user, password = str(args.get("username", "")), str(args.get("password", ""))
            brand = str(args.get("manufacturer", ""))
            found = camfind.detect(ip, args.get("ports") or [], brand, str(args.get("onvif", "")), user, password)
            name = (str(args.get("name", "")).strip() or f"{brand or 'Camera'} {ip}").strip()[:60]
            cam_id = "cam-" + ip.replace(".", "-")
            cam = {"id": cam_id, "name": name, "ip": ip, "brand": brand,
                   "rtsp": camfind.with_credentials(found["main"], user, password),
                   "sub": camfind.with_credentials(found["sub"], user, password) if found["sub"] else ""}
            with self._cfg_lock:
                cams = [c for c in self.cfg.get("cameras", []) if c.get("id") != cam_id]
                cams.append(cam)
                self.cfg["cameras"] = cams
                self._save()
            self.cams.add(cam)
            self.send_caps()
            self._reply(ws, req, {"ok": True, "data": {"id": cam_id, "name": name, "ip": ip,
                                                       "stream": found["main"], "sub": found["sub"]}})
        except camfind.AuthFailed as exc:
            self._reply(ws, req, {"ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            self._reply(ws, req, {"ok": False, "error": str(exc)})

    def _remove_camera(self, ws, req, cam_id):
        with self._cfg_lock:
            before = self.cfg.get("cameras", [])
            self.cfg["cameras"] = [c for c in before if c.get("id") != cam_id or c.get("token")]
            changed = len(before) != len(self.cfg["cameras"])
            if changed:
                self._save()
        if changed and self.cams:
            self.cams.remove(cam_id)
            self.send_caps()
        self._reply(ws, req, {"ok": changed, **({} if changed else {"error": "No such camera."})})

    def _save(self):
        if self.config_path:
            save_config(self.config_path, self.cfg)

    def _reply(self, ws, req, body):
        ws.send(json.dumps({"type": "result", "reqId": req, **body}))

    def dispatch(self, ws, msg):
        cap, op, req = msg.get("cap"), msg.get("do"), msg.get("reqId")
        args = msg.get("args") or {}
        try:
            if cap == "discovery" and op == "scan":
                threading.Thread(target=self._scan, args=(ws, req), daemon=True).start()  # a few seconds
            elif cap == "discovery" and op == "trust":
                self._trust(ws, req)
            elif cap == "discovery" and op == "add":
                threading.Thread(target=self._add_camera, args=(ws, req, args), daemon=True).start()  # ~2-20 s
            elif cap == "discovery" and op == "remove":
                self._remove_camera(ws, req, str(args.get("id", "")))
            elif cap == "gpio" and op == "set":
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
        self.ws = ws
        self.shellmgr = ShellManager(ws, self.scripts, self.shell_enabled)
        if self.cams:
            self.cams.set_sender(lambda viewer, data: ws.send(json.dumps({"type": "signal", "to": viewer, "data": data})))
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
                elif mtype == "signal" and self.cams:
                    self.cams.on_signal(msg.get("from"), msg.get("data") or {})
        finally:
            self.ws = None
            if self.cams:
                self.cams.set_sender(None)
                self.cams.drop_viewers()
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


def camera_ok():
    import camera as _camera
    return _camera.AIORTC_OK


def _watch_local(agent, every=60):
    """A camera plugged in or out shows up on the dashboard within a minute."""
    while True:
        time.sleep(every)
        try:
            if agent.refresh_local():
                agent.send_caps()
        except Exception as exc:  # noqa: BLE001
            print(f"[camera] local camera check failed: {exc}")


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
        local = localcams.list_local()
        print(f"This computer's cameras: {len(local)}")
        for c in local:
            print(f"  {c['name']}   ({c['input']})")
        print()
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
    if cfg.get("local_cameras") is not False:  # this computer's own cameras (M9), unless turned off in config.json
        cameras = cameras + localcams.list_local()
    cams = CameraManager(cfg["server"], bool(cfg.get("insecure", False)), cameras)
    cams.start()

    agent = Agent(cfg, cams, os.path.abspath(args.config))
    threading.Thread(target=_watch_local, args=(agent,), daemon=True).start()
    try:
        agent.run_forever()
    except KeyboardInterrupt:
        print("\n[agent] bye")
    finally:
        cams.stop()
        agent.gpio.close()


if __name__ == "__main__":
    main()
