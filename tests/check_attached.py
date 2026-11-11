"""Attached cameras (M7d): signals with a camera id reach that camera; its replies are labelled with it.
Runs without aiortc: the stream itself fails (no aiortc), which must come back as an error for THAT camera."""
import os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agent"))
import camera  # noqa: E402

fails = 0
def check(ok, what):
    global fails
    print(("ok   " if ok else "FAIL ") + what)
    fails += 0 if ok else 1

cams = [
    {"id": "front", "name": "Front door", "rtsp": "rtsp://admin:x@10.77.0.10:554/Streaming/Channels/102"},
    {"id": "garden", "name": "Garden", "rtsp": "rtsp://admin:x@10.77.0.11:554/cam/realmonitor?channel=1&subtype=1"},
    {"id": "old", "name": "Old style", "token": "own-token", "rtsp": "rtsp://10.77.0.10/x"},
]
check(camera.validate_cameras(cams) == [], "cameras without a token are valid (attached)")
m = camera.CameraManager("ws://127.0.0.1:1/agent", False, cams)
check([d.id for d in m.attached()] == ["front", "garden"], "attached = the ones without their own token")
camera.AIORTC_OK = True  # start the camera loops even though aiortc isn't installed here
for d in m.attached():
    d.start()
m.running = True
cap = m.capability()
check(cap == {"cap": "camera", "cameras": [{"id": "front", "name": "Front door", "source": "rtsp"}, {"id": "garden", "name": "Garden", "source": "rtsp"}]},
      "camera capability lists the attached cameras for the picker")
sent = []
m.set_sender(lambda viewer, data: sent.append((viewer, data)))
time.sleep(0.3)
check(m.on_signal("viewer-1", {"kind": "request", "camera": "garden"}) is True, "a request for 'garden' is handled")
check(m.on_signal("viewer-1", {"kind": "request", "camera": "nope"}) is False, "an unknown camera is not")
for _ in range(50):
    if sent:
        break
    time.sleep(0.1)
check(len(sent) == 1 and sent[0][0] == "viewer-1" and sent[0][1].get("camera") == "garden" and sent[0][1].get("kind") == "error",
      f"the reply goes to the viewer, labelled 'garden' ({sent})")
for d in m.attached():
    d.stop()
print("ALL OK" if not fails else f"{fails} FAILED")
sys.exit(1 if fails else 0)
