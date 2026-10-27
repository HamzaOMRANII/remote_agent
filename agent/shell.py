"""
Shell + scripts for the agent.

- scripts: run an allowlisted, admin-defined command (from config) and stream its
  output back, then a final result with the exit code.
- shell:   an interactive pty (bash) per session; output is streamed, input is
  written to the pty. Opt-in per device (config shell.enabled) — the browser
  dashboard additionally gates it behind an admin password re-prompt.

Each streaming op runs on its own thread and sends frames through the (now
thread-safe) WebSocket client. Output is decoded utf-8 with errors='replace' so
terminal control sequences pass through to xterm.js on the dashboard.
"""

import json
import os
import pty
import signal
import struct
import subprocess
import threading

try:
    import fcntl
    import termios
    HAVE_TTY = True
except ImportError:  # non-POSIX
    HAVE_TTY = False


class ShellManager:
    def __init__(self, ws, scripts=None, shell_enabled=False):
        self.ws = ws
        self.scripts = {s["id"]: s for s in (scripts or [])}
        self.shell_enabled = shell_enabled
        self.sessions = {}  # sid -> { "fd", "proc", "reqId" }
        self._lock = threading.Lock()

    def _send(self, obj):
        try:
            self.ws.send(json.dumps(obj))
        except Exception:
            pass

    # ---- scripts ----------------------------------------------------------
    def run_script(self, req_id, script_id):
        s = self.scripts.get(script_id)
        if not s:
            self._send({"type": "result", "reqId": req_id, "ok": False, "error": f"unknown script: {script_id}"})
            return
        threading.Thread(target=self._run_script, args=(req_id, s), daemon=True).start()

    def _run_script(self, req_id, s):
        try:
            proc = subprocess.Popen(
                s["cmd"], shell=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                bufsize=1, universal_newlines=True,
            )
            for line in iter(proc.stdout.readline, ""):
                self._send({"type": "stream", "reqId": req_id, "chunk": line})
            proc.stdout.close()
            code = proc.wait()
            self._send({"type": "result", "reqId": req_id, "ok": code == 0, "data": {"code": code}})
        except Exception as exc:  # noqa: BLE001
            self._send({"type": "result", "reqId": req_id, "ok": False, "error": str(exc)})

    # ---- interactive shell ------------------------------------------------
    def open(self, req_id, sid):
        if not self.shell_enabled:
            self._send({"type": "result", "reqId": req_id, "ok": False, "error": "shell disabled on this device"})
            return
        if not HAVE_TTY:
            self._send({"type": "result", "reqId": req_id, "ok": False, "error": "pty not supported on this platform"})
            return
        try:
            master, slave = pty.openpty()
            env = dict(os.environ, TERM="xterm-256color")
            proc = subprocess.Popen(
                ["/bin/bash"], stdin=slave, stdout=slave, stderr=slave,
                preexec_fn=os.setsid, env=env, close_fds=True,
            )
            os.close(slave)
            with self._lock:
                self.sessions[sid] = {"fd": master, "proc": proc, "reqId": req_id}
            threading.Thread(target=self._pump, args=(sid, master, proc, req_id), daemon=True).start()
        except Exception as exc:  # noqa: BLE001
            self._send({"type": "result", "reqId": req_id, "ok": False, "error": str(exc)})

    def _pump(self, sid, fd, proc, req_id):
        try:
            while True:
                try:
                    data = os.read(fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                self._send({"type": "stream", "reqId": req_id, "chunk": data.decode("utf-8", "replace")})
        finally:
            code = proc.poll()
            if code is None:
                try: os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception: pass
                code = proc.wait()
            with self._lock:
                self.sessions.pop(sid, None)
            try: os.close(fd)
            except Exception: pass
            self._send({"type": "result", "reqId": req_id, "ok": True, "data": {"code": code}})

    def input(self, sid, data):
        s = self.sessions.get(sid)
        if s:
            try: os.write(s["fd"], data.encode("utf-8"))
            except Exception: pass

    def resize(self, sid, cols, rows):
        s = self.sessions.get(sid)
        if s and HAVE_TTY:
            try:
                fcntl.ioctl(s["fd"], termios.TIOCSWINSZ, struct.pack("HHHH", int(rows), int(cols), 0, 0))
            except Exception:
                pass

    def close(self, sid):
        s = self.sessions.get(sid)
        if s:
            try: os.killpg(os.getpgid(s["proc"].pid), signal.SIGKILL)
            except Exception: pass

    def shutdown(self):
        for sid in list(self.sessions):
            self.close(sid)
