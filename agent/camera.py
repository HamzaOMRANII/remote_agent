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
    from aiortc.contrib.media import MediaPlayer
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
    """One RTSP camera, presented to the dashboard as its own device."""

    def __init__(self, ws_url, insecure, cam):
        self.ws_url = ws_url
        self.insecure = insecure
        self.id = cam["id"]
        self.name = cam.get("name") or cam["id"]
        self.token = cam["token"]
        self.rtsp = cam["rtsp"]
        self.want_audio = bool(cam.get("audio", False))
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
        self._players = {}  # viewer id -> MediaPlayer
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
        viewer = msg.get("from")
        data = msg.get("data") or {}
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
        ws.send(json.dumps({"type": "signal", "to": viewer, "data": data}))

    async def _start_stream(self, ws, viewer):
        await self._close_viewer(viewer)  # a fresh request replaces any old connection
        try:
            pc = RTCPeerConnection(RTCConfiguration(iceServers=self._ice_servers()))
            self._pcs[viewer] = pc

            player = MediaPlayer(self.rtsp, format="rtsp", options=self.options)
            self._players[viewer] = player
            if player.video:
                pc.addTrack(player.video)
            if self.want_audio and player.audio:
                pc.addTrack(player.audio)
            if not player.video:
                raise RuntimeError("no video track from the camera")

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

    @staticmethod
    def _friendly(err):
        low = err.lower()
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
        player = self._players.pop(viewer, None)
        if player:
            try:
                if player.video:
                    player.video.stop()
                if player.audio:
                    player.audio.stop()
            except Exception:
                pass
        if pc:
            try:
                await pc.close()
            except Exception:
                pass

    async def _close_all(self):
        for viewer in list(self._pcs):
            await self._close_viewer(viewer)


class CameraManager:
    """Starts/stops one CameraDevice per configured RTSP camera."""

    def __init__(self, ws_url, insecure, cameras):
        self.devices = [CameraDevice(ws_url, insecure, c) for c in cameras]

    def start(self):
        if not self.devices:
            return
        if not AIORTC_OK:
            print("[camera] cameras are configured but aiortc is not installed.")
            print(f"[camera] run:  pip install aiortc      (import error: {AIORTC_ERROR})")
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
        for field in ("id", "token", "rtsp"):
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
