"""Checks agent/scan.py against remote/mobile/test/camlab.py (run both inside the "camlab" network namespace)."""
import os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agent"))
import scan  # noqa: E402

fails = 0
def check(ok, what):
    global fails
    print(("ok   " if ok else "FAIL ") + what)
    fails += 0 if ok else 1

check(len(scan.subnet("10.77.0.2")) == 253, "a /24 gives 253 addresses without itself")
check(scan.subnet("8.8.8.8") == [], "public addresses are never scanned")
updates = []
t0 = time.time()
hosts = scan.discover(scan.subnet("10.77.0.2"), probe_to=("10.77.0.10", 3702), on_update=updates.append)
ms = (time.time() - t0) * 1000
by = {h["ip"]: h for h in hosts}
for h in hosts:
    print("    ", h["ip"], h["kind"], [p["port"] for p in h["ports"]], h["manufacturer"], h["model"], h["onvif"])
check(len(hosts) == 3, "3 devices found")
hik = by.get("10.77.0.10", {})
check(hik.get("kind") == "camera" and hik.get("manufacturer") == "HIKVISION" and hik.get("model") == "DS-2CD2143G0-I", "ONVIF camera with brand and model")
check({p["port"] for p in hik.get("ports", [])} == {80, 554}, "its ports 80 and 554")
check(by.get("10.77.0.11", {}).get("kind") == "camera", "37777/554 device is a camera")
check(by.get("10.77.0.12", {}).get("kind") == "maybe", "web-only device is 'maybe'")
check(len(updates) >= 6, f"progress updates while scanning ({len(updates)})")
check(ms < 9000, f"a /24 in under 9 s ({ms:.0f} ms)")
print("ALL OK" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
