"""
Raspberry Pi camera video streaming (subprocess ``rpicam-vid`` / libcamera).

No extra pip dependencies; uses the system camera stack on Pi OS Bookworm.

Default stream: UDP MPEG-TS on port 8888 to the ground-station client that enabled video via the box HTTP API.

Viewers: ``ffplay -fflags nobuffer -flags low_delay -framedrop -i udp://@:8888``.

Environment (optional):

- ``BOX_CAMERA_WIDTH``, ``BOX_CAMERA_HEIGHT``, ``BOX_CAMERA_FRAMERATE``,
  ``BOX_CAMERA_BITRATE`` (default ``2500000``), ``BOX_CAMERA_INTRA`` (default ``15``),
  ``BOX_CAMERA_LOW_LATENCY`` (default ``1`` → ``--low-latency``).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from typing import List, Optional, Tuple

# UDP MPEG-TS on port 8888 (Pi → connected HTTP client IP).
STREAM_PORT = 8888


def _stream_udp_output(client_host: str) -> str:
    host = (client_host or "").strip()
    if not host:
        raise ValueError("no connected client IP for UDP stream")
    return f"udp://{host}:{STREAM_PORT}"


def _which_camera_tool(*names: str) -> Optional[str]:
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    return None


def _camera_vid_binary() -> Optional[str]:
    """``rpicam-vid`` (Bookworm) or ``libcamera-vid`` (older libcamera apps)."""
    return _which_camera_tool("rpicam-vid", "libcamera-vid")


def _normalize_camera_error(raw: str) -> str:
    t = (raw or "").strip()
    low = t.lower()
    if not t:
        return "Camera stream failed (no details from rpicam-vid)."
    if "no cameras available" in low or "no camera available" in low:
        return "No camera detected by libcamera."
    if "command not found" in low or "no such file" in low:
        return "rpicam-vid / libcamera-vid not found. Install Pi OS camera apps."
    if "failed to send" in low and "socket" in low:
        return (
            "Stream socket error (rpicam-vid).\n"
            "• Turn Video ON on the Pi, then run ffplay on the ground station\n"
            f"• Viewer: ffplay -i udp://@:{STREAM_PORT} on the ground station (Box tab Try ffplay)\n"
            "• If playback stops, toggle Video off/on on the Box tab"
        )
    return t[-1200:]


def camera_stream_client_url(port: Optional[int] = None) -> str:
    """ffplay input URL (listen on the ground station for Pi unicast)."""
    p = port if port is not None else STREAM_PORT
    return f"udp://@:{p}"


def ffplay_low_latency_argv(input_url: str) -> List[str]:
    """``ffplay`` with minimal buffering (Box tab / manual launch)."""
    return [
        "ffplay",
        "-fflags",
        "nobuffer",
        "-flags",
        "low_delay",
        "-framedrop",
        "-i",
        input_url,
    ]


def _low_latency_enabled() -> bool:
    return os.environ.get("BOX_CAMERA_LOW_LATENCY", "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )


def probe_cameras(timeout: float = 5.0) -> Tuple[bool, str]:
    """
    Return (True, "") if a libcamera camera appears available, else (False, message).
    Skipped when BOX_CAMERA_SKIP_PROBE=1.
    """
    if os.environ.get("BOX_CAMERA_SKIP_PROBE", "").strip() in ("1", "true", "yes"):
        return True, ""
    hello = _which_camera_tool("rpicam-hello", "libcamera-hello")
    if hello is None:
        return True, ""
    try:
        r = subprocess.run(
            [hello, "--list-cameras"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        out = f"{r.stdout or ''}\n{r.stderr or ''}".strip()
        low = out.lower()
        if "no cameras available" in low or "no cameras found" in low:
            return False, _normalize_camera_error(out)
        if re.search(r"^\s*\d+\s*:", out, re.MULTILINE):
            return True, ""
        if r.returncode != 0:
            return False, _normalize_camera_error(out or f"{hello} exited {r.returncode}")
    except subprocess.TimeoutExpired:
        return False, f"{hello} timed out while listing cameras"
    except OSError as e:
        return False, str(e)
    return True, ""


def _camera_stream_argv(client_host: str) -> List[str]:
    """``rpicam-vid`` UDP MPEG-TS to ``client_host``."""
    vid = _camera_vid_binary()
    if vid is None:
        raise FileNotFoundError("rpicam-vid and libcamera-vid not found in PATH")
    w = os.environ.get("BOX_CAMERA_WIDTH", "640")
    h = os.environ.get("BOX_CAMERA_HEIGHT", "480")
    fps = os.environ.get("BOX_CAMERA_FRAMERATE", "25")
    bitrate = os.environ.get("BOX_CAMERA_BITRATE", "2500000")
    intra = os.environ.get("BOX_CAMERA_INTRA", "15")
    argv = [
        vid,
        "-t",
        "0",
        "-n",
        "--width",
        w,
        "--height",
        h,
        "--framerate",
        fps,
        "--bitrate",
        bitrate,
        "--intra",
        intra,
        "--codec",
        "libav",
        "--libav-format",
        "mpegts",
        "-o",
        _stream_udp_output(client_host),
    ]
    if _low_latency_enabled():
        argv.append("--low-latency")
    cam_idx = os.environ.get("BOX_CAMERA_INDEX", "").strip()
    if cam_idx:
        argv[1:1] = ["--camera", cam_idx]
    return argv


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

    Default: UDP MPEG-TS on port 8888.
    """

    def __init__(self, argv: Optional[List[str]] = None):
        self._argv_override = argv
        self._client_host: Optional[str] = None
        self._proc: Optional[subprocess.Popen] = None
        self._last_error: Optional[str] = None

    def set_client_host(self, host: str) -> None:
        """UDP destination (ground-station IP from the box HTTP client)."""
        self._client_host = (host or "").strip() or None

    def _argv(self) -> List[str]:
        if self._argv_override is not None:
            return self._argv_override
        host = self._client_host
        if not host:
            raise ValueError("no connected client IP for UDP stream")
        return _camera_stream_argv(host)

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
        if self._last_error is None:
            tail = _drain_stderr(proc)
            self._last_error = _normalize_camera_error(tail or f"camera process exited (code {code})")
        try:
            if proc.stderr:
                proc.stderr.close()
        except Exception:
            pass
        self._proc = None
        return False

    def start(self, client_host: Optional[str] = None) -> bool:
        """Spawn the streamer. Returns False if spawn fails or the process exits immediately."""
        if client_host:
            self.set_client_host(client_host)
        if self.is_running:
            return True
        self._last_error = None
        if self._argv_override is None and not self._client_host:
            self._last_error = "No connected client IP (connect from the ground station first)."
            return False
        ok_probe, probe_err = probe_cameras()
        if not ok_probe:
            self._last_error = probe_err
            return False
        try:
            argv = self._argv()
        except (OSError, ValueError, FileNotFoundError) as e:
            self._last_error = _normalize_camera_error(str(e))
            return False
        try:
            self._proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except (OSError, ValueError) as e:
            self._last_error = _normalize_camera_error(str(e))
            self._proc = None
            return False
        time.sleep(0.45)
        if self._proc is not None and self._proc.poll() is not None:
            tail = _drain_stderr(self._proc)
            code = self._proc.returncode
            self._last_error = _normalize_camera_error(
                tail or f"camera process exited immediately (code {code})"
            )
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
