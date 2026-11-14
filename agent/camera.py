"""
IP-camera relay: brings RTSP cameras on the local network into the Remote
dashboard. Each configured camera connects to the server as its own device
(its own id + token), so it shows up as a normal camera tile next to phones —
no server or dashboard changes needed.

How it works
------------
The server relays WebRTC signaling between a dashboard viewer and this device
(ARCHITECTURE.md §4, protocol/messages.js). For a camera the flow is:

    viewer -> agent : signal { kind:"request" }
    agent  -> viewer: signal { kind:"offer", sdp }     (our RTSP video, as WebRTC)
    viewer -> agent : signal { kind:"answer", sdp }
    viewer -> agent : signal { kind:"ice", candidate } (trickled; we add them)

We pull the RTSP stream with aiortc's MediaPlayer (PyAV / bundled ffmpeg) and
hand its track to a WebRTC PeerConnection. aiortc gathers its ICE candidates
into the offer SDP, so we don't trickle any back — which keeps the browser side
unchanged. The camera is only opened while someone is watching; the player is
released when the last viewer leaves.

aiortc is only needed when cameras are configured:  pip install aiortc
"""

import asyncio
import json
import threading
import urllib.request

try:
    from aiortc import (
        RTCConfiguration,
        RTCIceServer,
        RTCPeerConnection,
        RTCSessionDescription,
    )
    from aiortc.contrib.media import MediaPlayer, MediaRelay
    from aiortc.sdp import candidate_from_sdp

    AIORTC_OK = True
    AIORTC_ERROR = ""
except Exception as exc:  # noqa: BLE001 — reported once, clearly, at startup
    AIORTC_OK = False
    AIORTC_ERROR = str(exc)

from ws_client import WebSocketClient, WebSocketError


def _http_base(ws_url):
    # wss://host/agent -> https://host   (for /api/ice)
    base = ws_url
    if base.startswith("wss://"):
        base = "https://" + base[len("wss://"):]
    elif base.startswith("ws://"):
        base = "http://" + base[len("ws://"):]
    if base.endswith("/agent"):
        base = base[: -len("/agent")]
    return base.rstrip("/")


class CameraDevice:
    """One RTSP camera. With its own "token" it is its own device on the dashboard (its own connection);
    without one it is ATTACHED to this agent's device (M7c/M7d): it shows in that device's camera picker and its
    signals travel on the agent's connection, labelled with the camera id."""

    def __init__(self, ws_url, insecure, cam):
        self.ws_url = ws_url
        self.insecure = insecure
        self.id = cam["id"]
        self.name = cam.get("name") or cam["id"]
        self.token = cam.get("token") or ""
        self.attached = not self.token
        self._send_attached = None  # set by CameraManager: (viewer, data) -> None, on the agent's connection
        self.source = cam.get("source") or "rtsp"   # "rtsp" (network camera) or "local" (this computer's camera, M9)
        self.rtsp = cam.get("rtsp", "")
        self.input = cam.get("input", "")         # local: "video=<name>" (dshow), "/dev/video0" (v4l2), "<name>:none"
        self.format = cam.get("format", "")
        self.local_options = dict(cam.get("options") or {})
        self.want_audio = bool(cam.get("audio", False)) and self.source == "rtsp"
        # ffmpeg/PyAV options for a reliable pull from most cameras. Kept to widely
        # supported ones: "rtsp_transport" tcp survives Wi-Fi loss better than udp, and
        # "timeout" (microseconds) bounds a dead connection. (The older "stimeout" name
        # is avoided — an unknown option can make the stream fail to open on some ffmpeg.)
        self.options = {
            "rtsp_transport": cam.get("transport", "tcp"),
            "timeout": str(int(cam.get("timeout", 15)) * 1_000_000),
            "max_delay": "500000",
        }
        self._thread = None
        self._loop = None
        self._stop = threading.Event()
        self._pcs = {}     # viewer id -> RTCPeerConnection
        self._player = None  # ONE open camera shared by every viewer (a webcam can only be opened once)
        self._relay = None
        self._ws = None

    # ---- lifecycle -------------------------------------------------------

    def start(self):
        self._thread = threading.Thread(target=self._thread_main, name=f"cam-{self.id}", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._loop:
            self._loop.call_soon_threadsafe(lambda: None)  # wake the loop

    def _thread_main(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._run())
        finally:
            self._loop.close()

    async def _run(self):
        if self.attached:  # no connection of its own: just keep the loop alive for handle_signal()
            while not self._stop.is_set():
                await asyncio.sleep(0.5)
            await self._close_all()
            return
        backoff = 1
        while not self._stop.is_set():
            try:
                await self._connect_and_serve()
                backoff = 1
            except WebSocketError as exc:
                print(f"[camera {self.id}] connection lost: {exc}")
            except Exception as exc:  # noqa: BLE001
                print(f"[camera {self.id}] error: {exc}")
            if self._stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 15)

    async def _connect_and_serve(self):
        loop = self._loop
        ws = WebSocketClient(self.ws_url, insecure=self.insecure)
        await loop.run_in_executor(None, ws.connect)
        self._ws = ws
        hello = {
            "type": "hello",
            "token": self.token,
            "device": {"id": self.id, "name": self.name, "platform": "ipcam"},
            "capabilities": [{"cap": "camera"}],
        }
        ws.send(json.dumps(hello))
        print(f"[camera {self.id}] connected as '{self.name}'")
        try:
            while not self._stop.is_set():
                msg = await loop.run_in_executor(None, ws.recv)
                if msg is None:
                    break
                await self._on_message(ws, msg)
        finally:
            await self._close_all()
            try:
                ws.close()
            except Exception:
                pass
            self._ws = None

    # ---- signaling -------------------------------------------------------

    async def _on_message(self, ws, raw):
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        mtype = msg.get("type")
        if mtype == "hello-ok":
            return
        if mtype != "signal":
            return
        await self.handle_signal(ws, msg.get("from"), msg.get("data") or {})

    async def handle_signal(self, ws, viewer, data):
        """A viewer's signal (request / answer / ice). ws is None for an attached camera."""
        kind = data.get("kind")
        if kind == "request":
            await self._start_stream(ws, viewer)
        elif kind == "answer":
            pc = self._pcs.get(viewer)
            if pc:
                sdp = data.get("sdp") or {}
                await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp["sdp"], type=sdp["type"]))
        elif kind == "ice":
            pc = self._pcs.get(viewer)
            cand = data.get("candidate")
            if pc and cand and cand.get("candidate"):
                ice = candidate_from_sdp(cand["candidate"].split(":", 1)[1])
                ice.sdpMid = cand.get("sdpMid")
                ice.sdpMLineIndex = cand.get("sdpMLineIndex")
                await pc.addIceCandidate(ice)

    def _signal(self, ws, viewer, data):
        if self.attached:
            if self._send_attached:
                self._send_attached(viewer, {**data, "camera": self.id})
            return
        ws.send(json.dumps({"type": "signal", "to": viewer, "data": data}))

    async def _start_stream(self, ws, viewer):
        await self._close_viewer(viewer)  # a fresh request replaces any old connection
        try:
            pc = RTCPeerConnection(RTCConfiguration(iceServers=self._ice_servers()))
            self._pcs[viewer] = pc

            player = await self._open()
            pc.addTrack(self._relay.subscribe(player.video, buffered=False))
            if self.want_audio and player.audio:
                pc.addTrack(self._relay.subscribe(player.audio, buffered=False))

            @pc.on("connectionstatechange")
            async def _on_state():  # noqa: ANN202
                if pc.connectionState in ("failed", "closed", "disconnected"):
                    await self._close_viewer(viewer)

            offer = await pc.createOffer()
            await pc.setLocalDescription(offer)  # aiortc gathers ICE into this SDP
            self._signal(ws, viewer, {
                "kind": "offer",
                "sdp": {"type": pc.localDescription.type, "sdp": pc.localDescription.sdp},
            })
        except Exception as exc:  # noqa: BLE001
            print(f"[camera {self.id}] stream failed: {exc}")
            self._signal(ws, viewer, {"kind": "error", "error": self._friendly(str(exc))})
            await self._close_viewer(viewer)

    def _make_player(self, options):
        if self.source == "local":
            return MediaPlayer(self.input, format=self.format, options=options)
        return MediaPlayer(self.rtsp, format="rtsp", options=self.options)

    async def _open(self):
        """The shared player: opened by the first viewer, closed after the last one leaves."""
        if self._player is None:
            loop = asyncio.get_running_loop()
            try:
                player = await loop.run_in_executor(None, self._make_player, self.local_options)
            except Exception:
                if self.source != "local" or not self.local_options:
                    raise
                # the camera refused the size / frame rate we asked for: let it pick its own
                player = await loop.run_in_executor(None, self._make_player, {})
            if not player.video:
                raise RuntimeError("no video track from the camera")
            self._player, self._relay = player, MediaRelay()
        return self._player

    def _release_if_idle(self):
        if self._pcs or self._player is None:
            return
        try:
            if self._player.video:
                self._player.video.stop()
            if self._player.audio:
                self._player.audio.stop()
        except Exception:
            pass
        self._player = self._relay = None

    @staticmethod
    def _friendly(err):
        low = err.lower()
        if "could not find video device" in low or "no such file" in low or "i/o error" in low or "device or resource busy" in low:
            return "This computer's camera is busy or unplugged — close other apps using it (Teams, Zoom, Camera) and try again."
        if "401" in err or "unauthorized" in low or "authoriz" in low:
            return "Wrong camera username or password."
        if "timed out" in low or "timeout" in low or "etimedout" in low:
            return "Camera did not answer — check it is on and the address is right."
        if "connection refused" in low or "404" in err:
            return "No stream at that address — check the RTSP path."
        return "Could not open the camera stream."

    def _ice_servers(self):
        try:
            with urllib.request.urlopen(_http_base(self.ws_url) + "/api/ice", timeout=8) as r:
                data = json.loads(r.read().decode())
            servers = []
            for s in data.get("iceServers", []):
                urls = s.get("urls")
                servers.append(RTCIceServer(
                    urls=urls if isinstance(urls, list) else [urls],
                    username=s.get("username"),
                    credential=s.get("credential"),
                ))
            if servers:
                return servers
        except Exception as exc:  # noqa: BLE001
            print(f"[camera {self.id}] ICE fetch failed, using public STUN: {exc}")
        return [RTCIceServer(urls=["stun:stun.l.google.com:19302"])]

    # ---- teardown --------------------------------------------------------

    async def _close_viewer(self, viewer):
        pc = self._pcs.pop(viewer, None)
        if pc:
            try:
                await pc.close()
            except Exception:
                pass
        self._release_if_idle()

    async def _close_all(self):
        for viewer in list(self._pcs):
            await self._close_viewer(viewer)


class CameraManager:
    """Starts/stops one CameraDevice per configured RTSP camera, and routes the agent's signals to the attached ones."""

    def __init__(self, ws_url, insecure, cameras):
        self.ws_url, self.insecure = ws_url, insecure
        self.devices = [CameraDevice(ws_url, insecure, c) for c in cameras]
        self.running = False
        self._send = None

    def attached(self):
        return [d for d in self.devices if d.attached]

    def capability(self):
        """The camera picker entries for the agent's own device (attached cameras), or None."""
        cams = self.attached()
        if not cams or not self.running:
            return None
        return {"cap": "camera", "cameras": [{"id": d.id, "name": d.name, "source": d.source} for d in cams]}

    def set_sender(self, send):
        """send(viewer, data): how attached cameras reach viewers (the agent's live connection), or None."""
        self._send = send
        for d in self.attached():
            d._send_attached = send

    def add(self, cam):
        """Adds (or replaces) one attached camera while running (M9: added from the dashboard, or plugged in)."""
        self.remove(cam["id"])
        d = CameraDevice(self.ws_url, self.insecure, cam)
        d._send_attached = self._send
        self.devices.append(d)
        if AIORTC_OK:
            d.start()
            self.running = True
        return d

    def remove(self, cam_id):
        for d in [d for d in self.devices if d.id == cam_id]:
            d.stop()
            self.devices.remove(d)

    def on_signal(self, viewer, data):
        """A signal from the agent's connection. True if it was for an attached camera (handled here)."""
        cam = data.get("camera")
        for d in self.attached():
            if d.id == cam and d._loop is not None:
                asyncio.run_coroutine_threadsafe(d.handle_signal(None, viewer, data), d._loop)
                return True
        return False

    def drop_viewers(self):
        """The agent's connection dropped: close every attached camera's viewers (they will ask again)."""
        for d in self.attached():
            if d._loop is not None:
                asyncio.run_coroutine_threadsafe(d._close_all(), d._loop)

    def start(self):
        if not AIORTC_OK:
            if not self.devices:
                return
            print("[camera] cameras are configured but aiortc is not installed.")
            print(f"[camera] run:  pip install aiortc      (import error: {AIORTC_ERROR})")
            return
        self.running = True
        if not self.devices:
            return
        for d in self.devices:
            d.start()
        print(f"[camera] relaying {len(self.devices)} camera(s): " + ", ".join(d.name for d in self.devices))

    def stop(self):
        for d in self.devices:
            d.stop()


def validate_cameras(cameras):
    """Checks the 'cameras' config; returns a list of human-readable problems."""
    problems = []
    seen = set()
    for i, cam in enumerate(cameras):
        where = f"cameras[{i}]"
        for field in ("id", "rtsp"):  # no "token" = attached to this agent's device
            if not cam.get(field):
                problems.append(f"{where}: missing '{field}'")
        cid = cam.get("id")
        if cid in seen:
            problems.append(f"{where}: duplicate id '{cid}'")
        seen.add(cid)
        rtsp = cam.get("rtsp", "")
        if rtsp and not rtsp.startswith(("rtsp://", "rtsps://")):
            problems.append(f"{where}: rtsp should start with rtsp:// (got '{rtsp[:20]}...')")
    return problems
