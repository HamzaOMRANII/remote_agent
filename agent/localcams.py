"""
The cameras of THIS computer (M9): built-in webcam and any USB camera plugged in. Each one becomes an entry
in this device's camera picker on the dashboard, next to the network cameras — nothing to type.

  Windows  names from the PnP "Camera"/"Image" classes (PowerShell)  -> ffmpeg dshow   "video=<name>"
  macOS    names from system_profiler SPCameraDataType               -> ffmpeg avfoundation "<index>:none"
  Linux    /sys/class/video4linux (capture nodes only)                -> ffmpeg v4l2   "/dev/videoN"

Listing is pure standard library; playing them needs aiortc (bundled ffmpeg), like the IP cameras.
"""

import json
import os
import platform
import re
import subprocess


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "camera"


def _run(cmd, timeout=10):
    try:
        flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW: no console flash on Windows
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=flags).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _windows():
    out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                "Get-CimInstance Win32_PnPEntity | Where-Object { ($_.PNPClass -eq 'Camera' -or $_.PNPClass -eq 'Image')"
                " -and $_.Status -eq 'OK' } | Select-Object -ExpandProperty Name | ConvertTo-Json"])
    try:
        names = json.loads(out) if out.strip() else []
    except ValueError:
        names = []
    if isinstance(names, str):
        names = [names]
    seen, cams = set(), []
    for n in names:
        n = (n or "").strip()
        if not n or n in seen or "scanner" in n.lower():
            continue
        seen.add(n)
        cams.append({"name": n, "input": f"video={n}", "format": "dshow", "options": {}})
    return cams


def _macos():
    out = _run(["system_profiler", "SPCameraDataType", "-json"])
    try:
        items = json.loads(out).get("SPCameraDataType", [])
    except ValueError:
        items = []
    cams = []
    for i, it in enumerate(items):
        name = it.get("_name") or f"Camera {i + 1}"
        # avfoundation needs an explicit frame rate most cameras support; the device name works as the input
        cams.append({"name": name, "input": f"{name}:none", "format": "avfoundation",
                     "options": {"framerate": "30", "video_size": "1280x720", "pixel_format": "uyvy422"}})
    return cams


def _linux(root="/sys/class/video4linux"):
    cams = []
    try:
        nodes = sorted(os.listdir(root), key=lambda n: int(re.sub(r"\D", "", n) or 0))
    except OSError:
        return cams
    for node in nodes:
        base = os.path.join(root, node)
        try:
            # a USB webcam has several /dev/video nodes; index 0 is the picture, the others are metadata
            with open(os.path.join(base, "index")) as fh:
                if fh.read().strip() != "0":
                    continue
        except OSError:
            pass
        try:
            with open(os.path.join(base, "name")) as fh:
                name = fh.read().strip()
        except OSError:
            name = node
        if "bcm2835-isp" in name or "codec" in name.lower():  # Pi internal blocks, not cameras
            continue
        cams.append({"name": name, "input": f"/dev/{node}", "format": "v4l2",
                     "options": {"framerate": "30", "video_size": "1280x720"}})
    return cams


def list_local(system=None):
    """This computer's cameras: [{id, name, input, format, options, source: "local"}]."""
    system = system or platform.system()
    if system == "Windows":
        found = _windows()
    elif system == "Darwin":
        found = _macos()
    else:
        found = _linux()
    out, used = [], set()
    for c in found:
        cid = "local-" + _slug(c["name"])
        n = 2
        while cid in used:
            cid = f"local-{_slug(c['name'])}-{n}"
            n += 1
        used.add(cid)
        out.append({**c, "id": cid, "source": "local"})
    return out
