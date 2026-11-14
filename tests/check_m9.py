"""M9: this computer's cameras + adding a network camera from the dashboard with only its username/password.
Run inside the "camlab" network namespace (with `ip route add default dev d0` there) with remote/mobile/test/camlab.py running (like check_scan.py)."""
import json, os, sys, tempfile, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agent"))
import camera, localcams, remote_agent  # noqa: E402

fails = 0
def check(ok, what):
    global fails
    print(("ok   " if ok else "FAIL ") + what)
    fails += 0 if ok else 1

# ---- this computer's cameras, per OS ----
d = tempfile.mkdtemp()
for node, name, idx in (("video0", "Integrated Webcam", "0"), ("video1", "Integrated Webcam", "1"), ("video2", "Logitech C920", "0")):
    os.makedirs(os.path.join(d, node))
    open(os.path.join(d, node, "name"), "w").write(name + "\n")
    open(os.path.join(d, node, "index"), "w").write(idx + "\n")
lin = localcams._linux(d)
check([c["input"] for c in lin] == ["/dev/video0", "/dev/video2"], f"Linux: picture nodes only ({[c['input'] for c in lin]})")
localcams._run = lambda cmd, timeout=10: json.dumps(["Integrated Camera", "USB2.0 HD UVC WebCam", "Integrated Camera"])
win = localcams.list_local("Windows")
check([(c["id"], c["input"], c["format"]) for c in win] == [("local-integrated-camera", "video=Integrated Camera", "dshow"),
      ("local-usb2-0-hd-uvc-webcam", "video=USB2.0 HD UVC WebCam", "dshow")], f"Windows: dshow names, no duplicates ({win})")
localcams._run = lambda cmd, timeout=10: json.dumps({"SPCameraDataType": [{"_name": "FaceTime HD Camera"}]})
mac = localcams.list_local("Darwin")
check(mac[0]["format"] == "avfoundation" and mac[0]["input"] == "FaceTime HD Camera:none", "macOS: avfoundation by name")
localcams._run = lambda cmd, timeout=10: ""

# ---- the agent: trust, add, remove ----
class WS:
    def __init__(self): self.out = []
    def send(self, s): self.out.append(json.loads(s))
cfg_path = os.path.join(d, "config.json")
cfg = {"server": "ws://127.0.0.1:1/agent", "token": "t", "device": {"id": "laptop", "name": "Laptop"}}
remote_agent.save_config(cfg_path, cfg)
camera.AIORTC_OK = True  # camera loops start without aiortc here; streams themselves aren't tested
local = [{"id": "local-integrated-camera", "name": "Integrated Camera", "input": "video=Integrated Camera", "format": "dshow", "options": {}, "source": "local"}]
localcams.list_local = lambda system=None: [dict(c) for c in local]  # the "laptop" keeps its one webcam
cams = camera.CameraManager(cfg["server"], False, local)
cams.start()
agent = remote_agent.Agent(cfg, cams, cfg_path)
ws = WS(); agent.ws = ws
caps = {c["cap"]: c for c in agent.capabilities()}
check(caps["camera"]["cameras"] == [{"id": "local-integrated-camera", "name": "Integrated Camera", "source": "local"}], "the laptop camera is in the picker")
check(caps["discovery"]["network"]["trusted"] is False and caps["discovery"]["network"]["subnet"] == "10.77.0.0/24", f"new network: not trusted yet ({caps['discovery']['network']})")

def act(op, args=None, wait=30):
    ws.out.clear()
    agent.dispatch(ws, {"cap": "discovery", "do": op, "reqId": op, "args": args or {}})
    for _ in range(wait * 10):
        res = [m for m in ws.out if m.get("type") == "result"]
        if res: return res[0]
        time.sleep(0.1)
    return None

r = act("add", {"ip": "10.77.0.10", "ports": [80, 554], "username": "admin", "password": "Cam-Pass-123"})
check(r and not r["ok"] and "your network" in r["error"], f"add refused before the network is marked ({r and r.get('error')})")
r = act("scan")
check(r and r["ok"] and r["data"]["untrusted"] and r["data"]["hosts"] == [] and r["data"]["local"][0]["name"] == "Integrated Camera",
      "scan on an unmarked network: this computer's cameras only, network untouched")
r = act("trust")
check(r and r["ok"] and json.load(open(cfg_path))["discovery"]["networks"] == ["10.77.0.0/24"], "This is my network -> saved in config.json")
check(any(m.get("type") == "caps" for m in ws.out), "dashboard told (caps)")
r = act("scan")
hosts = {h["ip"]: h for h in (r and r["data"]["hosts"]) or []}
check(r and r["ok"] and "10.77.0.10" in hosts and "10.77.0.11" in hosts, f"scan finds the network cameras ({sorted(hosts)})")
h = hosts["10.77.0.10"]
r = act("add", {"ip": h["ip"], "ports": [p["port"] for p in h["ports"]], "manufacturer": h["manufacturer"], "onvif": h["onvif"],
                "username": "admin", "password": "wrong"})
check(r and not r["ok"] and "refused" in r["error"], f"wrong password: clear message ({r and r.get('error')})")
r = act("add", {"ip": h["ip"], "ports": [p["port"] for p in h["ports"]], "manufacturer": h["manufacturer"], "onvif": h["onvif"],
                "username": "admin", "password": "Cam-Pass-123", "name": "Front door"})
check(r and r["ok"] and r["data"]["stream"].endswith("/Streaming/Channels/101"), f"ONVIF camera added, link detected ({r and r.get('data')})")
h2 = hosts["10.77.0.11"]
r = act("add", {"ip": h2["ip"], "ports": [p["port"] for p in h2["ports"]], "username": "admin", "password": "Cam-Pass-123"})
check(r and r["ok"] and "realmonitor" in r["data"]["stream"], f"camera without ONVIF added ({r and r.get('data')})")
saved = json.load(open(cfg_path))
check([c["rtsp"] for c in saved["cameras"]] == ["rtsp://admin:Cam-Pass-123@10.77.0.10:554/Streaming/Channels/101",
      "rtsp://admin:Cam-Pass-123@10.77.0.11:554/cam/realmonitor?channel=1&subtype=0"], "saved with their links in config.json")
check(oct(os.stat(cfg_path).st_mode & 0o777) == "0o600", "config.json stays private (600)")
check(camera.validate_cameras(saved["cameras"]) == [], "saved cameras pass the config check (next start)")
picker = [c["id"] for c in {c["cap"]: c for c in agent.capabilities()}["camera"]["cameras"]]
check(picker == ["local-integrated-camera", "cam-10-77-0-10", "cam-10-77-0-11"], f"picker: laptop camera + both network cameras ({picker})")
caps = {c["cap"]: c for c in agent.capabilities()}
check([a["name"] for a in caps["discovery"]["added"]] == ["Front door", "Dahua 10.77.0.11"] or len(caps["discovery"]["added"]) == 2, f"added list for the dashboard ({caps['discovery']['added']})")
r = act("remove", {"id": "cam-10-77-0-10"})
check(r and r["ok"] and [c["id"] for c in json.load(open(cfg_path))["cameras"]] == ["cam-10-77-0-11"], "remove")
r = act("add", {"ip": "8.8.8.8", "username": "a", "password": "b"})
check(r and not r["ok"], "public addresses refused")
cams.stop()
print("ALL OK" if not fails else f"{fails} FAILED")
