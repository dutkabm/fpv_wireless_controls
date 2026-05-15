"""
Raspberry Pi camera video streaming (subprocess ``rpicam-vid`` / libcamera).

No extra pip dependencies; uses the system camera stack on Pi OS Bookworm.

Environment (optional):

- ``BOX_CAMERA_STREAM_CMD`` — full command string (``shlex.split``); replaces the default argv.
- ``BOX_CAMERA_STREAM_FORMAT`` — ``mpegts`` (default, VLC-friendly) or ``h264`` (raw; VLC: ``tcp/h264://``).
- Otherwise: ``BOX_CAMERA_WIDTH``, ``BOX_CAMERA_HEIGHT``, ``BOX_CAMERA_FRAMERATE``,
  ``BOX_CAMERA_STREAM_OUTPUT`` (default ``tcp://0.0.0.0:8888``; mpegts adds ``?listen=1``).
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import time
from typing import List, Optional, Tuple


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
        return (
            "No camera detected by libcamera.\n"
            "• USB webcam: set BOX_CAMERA_STREAM_CMD to an ffmpeg/v4l2 command"
        )
    if "command not found" in low or "no such file" in low:
        return (
            "rpicam-vid / libcamera-vid not found.\n"
            "Install Pi OS camera apps or set BOX_CAMERA_STREAM_CMD to your stream command."
        )
    if "failed to send" in low and "socket" in low:
        return (
            "Stream socket error (rpicam-vid).\n"
            "• Turn Video ON on the Pi first, then open VLC within a few seconds\n"
            "• Default (mpegts): Media → Open Network Stream → tcp://<pi-ip>:8888\n"
            "• Raw H.264 (BOX_CAMERA_STREAM_FORMAT=h264): use tcp/h264://<pi-ip>:8888\n"
            "• If VLC disconnects, toggle Video off/on on the Box tab (stream stops on disconnect)"
        )
    return t[-1200:]


def camera_stream_format() -> str:
    """``mpegts`` (default) or ``h264`` — must match how the Pi streams and how VLC opens the URL."""
    return os.environ.get("BOX_CAMERA_STREAM_FORMAT", "mpegts").strip().lower()


def camera_stream_client_url(host: str, port: Optional[int] = None) -> str:
    """VLC / ffplay URL for the current ``BOX_CAMERA_STREAM_FORMAT``."""
    p = port if port is not None else camera_stream_tcp_port()
    h = host.strip()
    if camera_stream_format() == "h264":
        return f"tcp/h264://{h}:{p}"
    return f"tcp://{h}:{p}"


def probe_cameras(timeout: float = 5.0) -> Tuple[bool, str]:
    """
    Return (True, "") if a libcamera camera appears available, else (False, message).
    Skipped when BOX_CAMERA_SKIP_PROBE=1 or BOX_CAMERA_STREAM_CMD is set.
    """
    if os.environ.get("BOX_CAMERA_SKIP_PROBE", "").strip() in ("1", "true", "yes"):
        return True, ""
    if os.environ.get("BOX_CAMERA_STREAM_CMD", "").strip():
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
        # libcamera lists lines like "0 : imx219 [...]"
        if re.search(r"^\s*\d+\s*:", out, re.MULTILINE):
            return True, ""
        if r.returncode != 0:
            return False, _normalize_camera_error(out or f"{hello} exited {r.returncode}")
    except subprocess.TimeoutExpired:
        return False, f"{hello} timed out while listing cameras"
    except OSError as e:
        return False, str(e)
    return True, ""


def _camera_stream_argv_from_env() -> List[str]:
    """Default Pi OS Bookworm stack: ``rpicam-vid`` TCP listener. Override with BOX_CAMERA_STREAM_CMD."""
    cmd = os.environ.get("BOX_CAMERA_STREAM_CMD", "").strip()
    if cmd:
        return shlex.split(cmd)
    vid = _camera_vid_binary()
    if vid is None:
        raise FileNotFoundError("rpicam-vid and libcamera-vid not found in PATH")
    w = os.environ.get("BOX_CAMERA_WIDTH", "1280")
    h = os.environ.get("BOX_CAMERA_HEIGHT", "720")
    fps = os.environ.get("BOX_CAMERA_FRAMERATE", "30")
    out = os.environ.get("BOX_CAMERA_STREAM_OUTPUT", "tcp://0.0.0.0:8888").strip()
    fmt = camera_stream_format()
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
    ]
    if fmt == "h264":
        argv.extend(["--codec", "h264", "--inline", "--listen", "-o", out])
    else:
        base = out.split("?", 1)[0]
        listen_out = out if "listen" in out.lower() else f"{base}?listen=1"
        argv.extend(["--codec", "libav", "--libav-format", "mpegts", "-o", listen_out])
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
            self._last_error = _normalize_camera_error(tail or f"camera process exited (code {code})")
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
