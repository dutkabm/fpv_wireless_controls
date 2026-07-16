"""
Raspberry Pi camera video streaming (hardware H264 + GStreamer RTP).

Pi pipeline (rpicam-vid stdout → gst-launch RTP/UDP)::

    rpicam-vid -t 0 --width 1280 --height 720 --framerate 60 --inline --nopreview -o - \\
      | gst-launch-1.0 fdsrc ! h264parse ! rtph264pay config-interval=-1 \\
        ! udpsink host=<client> port=5004

Mac viewer::

    gst-launch-1.0 udpsrc port=5004 \\
      caps="application/x-rtp,payload=96,encoding-name=H264" \\
      ! rtph264depay ! h264parse ! avdec_h264 ! videoconvert \\
      ! video/x-raw,format=UYVY ! osxvideosink sync=false

Environment (optional):

- ``BOX_CAMERA_WIDTH`` (default ``1280``), ``BOX_CAMERA_HEIGHT`` (default ``720``),
  ``BOX_CAMERA_FRAMERATE`` (default ``60``), ``BOX_CAMERA_BITRATE``,
  ``BOX_CAMERA_INDEX``, ``BOX_CAMERA_SKIP_PROBE``.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import signal
import subprocess
import time
from typing import List, Optional, Tuple

# RTP/H264 UDP (Pi → connected HTTP client IP).
STREAM_PORT = 5004


def _which_camera_tool(*names: str) -> Optional[str]:
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    return None


def _camera_vid_binary() -> Optional[str]:
    """``rpicam-vid`` (Bookworm) or ``libcamera-vid`` (older libcamera apps)."""
    return _which_camera_tool("rpicam-vid", "libcamera-vid")


def _gst_launch_binary() -> Optional[str]:
    return _which_camera_tool("gst-launch-1.0")


def _normalize_camera_error(raw: str) -> str:
    t = (raw or "").strip()
    low = t.lower()
    if not t:
        return "Camera stream failed (no details from rpicam-vid / gstreamer)."
    if "no cameras available" in low or "no camera available" in low:
        return "No camera detected by libcamera."
    if "command not found" in low or "no such file" in low:
        if "gst-launch" in low:
            return "gst-launch-1.0 not found. Install GStreamer on the Pi."
        return "rpicam-vid / libcamera-vid not found. Install Pi OS camera apps."
    if "failed to send" in low and "socket" in low:
        return (
            "Stream socket error (udpsink).\n"
            "• Turn Video ON on the Pi, then start the GStreamer viewer on the Mac\n"
            f"• Viewer: Box tab Play video (RTP/H264 UDP port {STREAM_PORT})\n"
            "• If playback stops, toggle Video off/on on the Box tab"
        )
    return t[-1200:]


def gstreamer_viewer_argv(port: Optional[int] = None, *, macos: bool = True) -> List[str]:
    """Ground-station ``gst-launch-1.0`` viewer for the Pi RTP/H264 stream."""
    gst = _gst_launch_binary() or "gst-launch-1.0"
    p = port if port is not None else STREAM_PORT
    sink = (
        ["videoconvert", "!", "video/x-raw,format=UYVY", "!", "osxvideosink", "sync=false"]
        if macos
        else ["videoconvert", "!", "autovideosink", "sync=false"]
    )
    return [
        gst,
        "udpsrc",
        f"port={p}",
        'caps=application/x-rtp,payload=96,encoding-name=H264',
        "!",
        "rtph264depay",
        "!",
        "h264parse",
        "!",
        "avdec_h264",
        "!",
        *sink,
    ]


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
    """
    Shell pipeline: hardware H264 from ``rpicam-vid`` → GStreamer RTP/UDP to ``client_host``.
    """
    vid = _camera_vid_binary()
    if vid is None:
        raise FileNotFoundError("rpicam-vid and libcamera-vid not found in PATH")
    gst = _gst_launch_binary()
    if gst is None:
        raise FileNotFoundError("gst-launch-1.0 not found in PATH")
    host = (client_host or "").strip()
    if not host:
        raise ValueError("no connected client IP for UDP stream")

    w = os.environ.get("BOX_CAMERA_WIDTH", "1280")
    h = os.environ.get("BOX_CAMERA_HEIGHT", "720")
    fps = os.environ.get("BOX_CAMERA_FRAMERATE", "60")
    bitrate = os.environ.get("BOX_CAMERA_BITRATE", "").strip()
    cam_idx = os.environ.get("BOX_CAMERA_INDEX", "").strip()

    vid_parts = [
        shlex.quote(vid),
        "-t",
        "0",
        "--width",
        shlex.quote(w),
        "--height",
        shlex.quote(h),
        "--framerate",
        shlex.quote(fps),
        "--inline",
        "--nopreview",
    ]
    if cam_idx:
        vid_parts.extend(["--camera", shlex.quote(cam_idx)])
    if bitrate:
        vid_parts.extend(["--bitrate", shlex.quote(bitrate)])
    vid_parts.extend(["-o", "-"])

    gst_parts = [
        shlex.quote(gst),
        "fdsrc",
        "!",
        "h264parse",
        "!",
        "rtph264pay",
        "config-interval=-1",
        "!",
        "udpsink",
        f"host={shlex.quote(host)}",
        f"port={STREAM_PORT}",
    ]
    pipeline = f"{' '.join(vid_parts)} | {' '.join(gst_parts)}"
    return ["bash", "-o", "pipefail", "-c", pipeline]


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


def _terminate_process_group(proc: subprocess.Popen) -> None:
    """Stop the shell pipeline (rpicam-vid | gst-launch) as a process group."""
    if proc.pid is None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        try:
            proc.terminate()
        except Exception:
            pass
    try:
        proc.wait(timeout=4.0)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc.wait(timeout=2.0)
    except Exception:
        pass


class CameraStream:
    """
    Run/stop a Raspberry Pi camera pipeline in a subprocess (no extra Python deps).

    Default: hardware H264 → GStreamer RTP/UDP on port 5004.
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
        """Terminate the streamer subprocess (and pipeline children)."""
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        _terminate_process_group(proc)
        try:
            if proc.stderr:
                proc.stderr.close()
        except Exception:
            pass
        self._last_error = None
