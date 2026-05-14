"""
Raspberry Pi camera video streaming (subprocess ``rpicam-vid`` / libcamera).

No extra pip dependencies; uses the system camera stack on Pi OS Bookworm.

Environment (optional):

- ``BOX_CAMERA_STREAM_CMD`` — full command string (``shlex.split``); replaces the default argv.
- Otherwise: ``BOX_CAMERA_WIDTH``, ``BOX_CAMERA_HEIGHT``, ``BOX_CAMERA_FRAMERATE``,
  ``BOX_CAMERA_STREAM_OUTPUT`` (default ``tcp://0.0.0.0:8888``).
"""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from typing import List, Optional


def _camera_stream_argv_from_env() -> List[str]:
    """Default Pi OS Bookworm stack: ``rpicam-vid`` TCP listener. Override with BOX_CAMERA_STREAM_CMD."""
    cmd = os.environ.get("BOX_CAMERA_STREAM_CMD", "").strip()
    if cmd:
        return shlex.split(cmd)
    w = os.environ.get("BOX_CAMERA_WIDTH", "1280")
    h = os.environ.get("BOX_CAMERA_HEIGHT", "720")
    fps = os.environ.get("BOX_CAMERA_FRAMERATE", "30")
    out = os.environ.get("BOX_CAMERA_STREAM_OUTPUT", "tcp://0.0.0.0:8888")
    return [
        "rpicam-vid",
        "-t",
        "0",
        "--width",
        w,
        "--height",
        h,
        "--framerate",
        fps,
        "--codec",
        "h264",
        "--inline",
        "--listen",
        "-o",
        out,
    ]


def _drain_stderr(proc: subprocess.Popen) -> str:
    if proc.stderr is None:
        return ""
    try:
        raw = proc.stderr.read()
        if isinstance(raw, bytes):
            return raw.decode("utf-8", errors="replace").strip()[-800:]
        return str(raw).strip()[-800:]
    except Exception:
        return ""


class CameraStream:
    """
    Run/stop a Raspberry Pi camera pipeline in a subprocess (no extra Python deps).

    Default command listens for one TCP client (VLC/ffplay etc.) on port 8888 unless
    ``BOX_CAMERA_STREAM_CMD`` or ``BOX_CAMERA_STREAM_OUTPUT`` overrides it.
    """

    def __init__(self, argv: Optional[List[str]] = None):
        self._argv_override = argv
        self._proc: Optional[subprocess.Popen] = None
        self._last_error: Optional[str] = None

    def _argv(self) -> List[str]:
        return self._argv_override if self._argv_override is not None else _camera_stream_argv_from_env()

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    @property
    def is_running(self) -> bool:
        proc = self._proc
        if proc is None:
            return False
        code = proc.poll()
        if code is None:
            return True
        # Process ended; capture stderr once for diagnostics / status.
        if self._last_error is None:
            tail = _drain_stderr(proc)
            self._last_error = tail or f"camera process exited (code {code})"
        try:
            if proc.stderr:
                proc.stderr.close()
        except Exception:
            pass
        self._proc = None
        return False

    def start(self) -> bool:
        """Spawn the streamer. Returns False if spawn fails or the process exits immediately."""
        if self.is_running:
            return True
        self._last_error = None
        argv = self._argv()
        try:
            self._proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except (OSError, ValueError) as e:
            self._last_error = str(e)
            self._proc = None
            return False
        time.sleep(0.2)
        if self._proc is not None and self._proc.poll() is not None:
            tail = _drain_stderr(self._proc)
            code = self._proc.returncode
            self._last_error = tail or f"camera process exited immediately (code {code})"
            try:
                if self._proc.stderr:
                    self._proc.stderr.close()
            except Exception:
                pass
            try:
                self._proc.wait(timeout=1.0)
            except Exception:
                pass
            self._proc = None
            return False
        return True

    def stop(self) -> None:
        """Terminate the streamer subprocess."""
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=4.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=2.0)
                except Exception:
                    pass
        except Exception:
            pass
        try:
            if proc.stderr:
                proc.stderr.close()
        except Exception:
            pass
        self._last_error = None


def camera_stream_tcp_port() -> int:
    """
    TCP port for the default ``rpicam-vid --listen -o tcp://...`` URL.

    Parsed from ``BOX_CAMERA_STREAM_OUTPUT`` (falls back to 8888). If you use a
    custom ``BOX_CAMERA_STREAM_CMD``, set ``BOX_CAMERA_STREAM_OUTPUT`` so clients
    can show the right URL, or ignore this hint.
    """
    out = os.environ.get("BOX_CAMERA_STREAM_OUTPUT", "tcp://0.0.0.0:8888").strip()
    base = out.split("?", 1)[0].split("#", 1)[0]
    if ":" in base:
        tail = base.rsplit(":", 1)[-1]
        tail = tail.split("/")[0]
        try:
            p = int(tail)
            if 1 <= p <= 65535:
                return p
        except ValueError:
            pass
    return 8888
