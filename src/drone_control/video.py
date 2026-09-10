"""
Board camera streaming (GStreamer RTP/UDP on port 5004).

MIPI on Raspberry Pi — hardware H264 via rpicam-vid → gst-launch RTP/UDP::

    rpicam-vid -t 0 --width 1280 --height 720 --framerate 60 --inline --nopreview -o - \\
      | gst-launch-1.0 fdsrc ! h264parse ! rtph264pay config-interval=-1 \\
        ! udpsink host=<client> port=5004

MIPI on Luckfox Pico Pro/Max (OpenIPC) — Majestic already owns the CSI sensor.
Video ON sets Majestic ``outgoing.server`` to ``udp://<client>:5004`` (RTP H264).
Do not run rpicam-vid / rkipc alongside Majestic.

USB — auto-detect UVC (e.g. ``USB2 Video (...usb-...)``), prefer MJPEG::


    gst-launch-1.0 v4l2src device=/dev/video0 \\
      ! image/jpeg,width=1280,height=720,framerate=30/1 \\
      ! rtpjpegpay ! udpsink host=<client> port=5004

    # Fallback when the node has no MJPEG (raw YUY2 → software JPEG)::
    gst-launch-1.0 v4l2src device=/dev/video0 \\
      ! video/x-raw,format=YUY2,width=1280,height=720,framerate=30/1 \\
      ! videoconvert ! jpegenc \\
      ! rtpjpegpay ! udpsink host=<client> port=5004

Mac viewers::

    # MIPI / H264
    gst-launch-1.0 udpsrc port=5004 \\
      caps="application/x-rtp,payload=96,encoding-name=H264" \\
      ! rtph264depay ! h264parse ! avdec_h264 ! videoconvert \\
      ! video/x-raw,format=UYVY ! osxvideosink sync=false

    # USB / JPEG
    gst-launch-1.0 udpsrc port=5004 \\
      caps="application/x-rtp,encoding-name=JPEG,payload=26" \\
      ! rtpjpegdepay ! jpegdec ! videoconvert ! osxvideosink sync=false

Environment (optional):

- ``BOX_CAMERA_WIDTH`` / ``BOX_CAMERA_HEIGHT`` / ``BOX_CAMERA_FRAMERATE`` / ``BOX_CAMERA_BITRATE`` /
  ``BOX_CAMERA_INDEX`` / ``BOX_CAMERA_SKIP_PROBE`` (Pi MIPI; see module constants for USB defaults).
- ``BOX_CAMERA_BACKEND`` — ``libcamera`` (Pi) or ``openipc`` (Majestic). Autodetected.
- ``BOX_MAJESTIC_URL`` — Majestic HTTP base (default ``http://127.0.0.1``).
"""

from __future__ import annotations

import glob
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import time
from typing import List, Literal, Optional, Tuple

# RTP UDP (board → connected HTTP client IP).
STREAM_PORT = 5004

CameraSource = Literal["mipi", "usb"]
UsbPixelFormat = Literal["mjpeg", "yuy2"]

# USB UVC — match ``v4l2-ctl --list-devices`` card name substring, e.g.
# ``USB2 Video: USB2 Video (usb-fe9c0000.xhci-1.3)``. Empty device → auto-pick first match.
USB_CAMERA_NAME = "USB2 Video"
USB_CAMERA_DEVICE = ""  # e.g. "/dev/video0" to pin; "" = detect via USB_CAMERA_NAME
USB_CAMERA_FORMAT: Optional[UsbPixelFormat] = "mjpeg"  # None = probe v4l2 formats
USB_CAMERA_WIDTH = 1280
USB_CAMERA_HEIGHT = 720
USB_CAMERA_FRAMERATE = 30
USB_CAMERA_JPEG_QUALITY = 85  # raw→jpegenc path only

_LOG = logging.getLogger(__name__)

# v4l2-ctl --list-devices header for a USB UVC cam, e.g.
# "USB2 Video: USB2 Video (usb-fe9c0000.xhci-1.3):"
_USB_LIST_DEVICES_RE = re.compile(r"\(usb-[^)]+\)\s*:", re.IGNORECASE)


def normalize_camera_source(source: Optional[str]) -> CameraSource:
    s = (source or "mipi").strip().lower()
    return "usb" if s == "usb" else "mipi"


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


def _v4l2_ctl_binary() -> Optional[str]:
    return _which_camera_tool("v4l2-ctl")


def _normalize_camera_error(raw: str) -> str:
    t = (raw or "").strip()
    low = t.lower()
    if not t:
        return "Camera stream failed (no details from rpicam-vid / GStreamer / Majestic)."
    if "majestic" in low:
        return t[-1200:]
    if "no cameras available" in low or "no camera available" in low:
        return "No camera detected by libcamera."
    if "no usb camera" in low:
        return t
    if "no such file or directory" in low and "video" in low:
        return (
            "USB camera device not found "
            f"(expected name containing {USB_CAMERA_NAME!r}; see v4l2-ctl --list-devices)."
        )
    if "not-negotiated" in low:
        return (
            "USB camera caps not negotiated.\n"
            f"• Check {USB_CAMERA_WIDTH}x{USB_CAMERA_HEIGHT}@{USB_CAMERA_FRAMERATE} / format={USB_CAMERA_FORMAT}\n"
            "• List modes: v4l2-ctl -d /dev/video0 --list-formats-ext"
        )
    if "command not found" in low or "no such file" in low:
        if "gst-launch" in low:
            return "gst-launch-1.0 not found. Install GStreamer on the Pi."
        return "rpicam-vid / libcamera-vid not found. Install Pi OS camera apps."
    if "failed to send" in low and "socket" in low:
        return (
            "Stream socket error (udpsink).\n"
            "• Turn Video ON on the Pi, then start the GStreamer viewer on the Mac\n"
            f"• Viewer: Box tab Play video (RTP UDP port {STREAM_PORT})\n"
            "• If playback stops, toggle Video off/on on the Box tab"
        )
    return t[-1200:]


def _parse_v4l2_list_devices(text: str) -> List[Tuple[str, List[str]]]:
    """Parse ``v4l2-ctl --list-devices`` into ``[(card_name, [/dev/videoN, ...]), ...]``."""
    groups: List[Tuple[str, List[str]]] = []
    name: Optional[str] = None
    nodes: List[str] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        if not line.startswith(("\t", " ")) and line.endswith(":"):
            if name is not None:
                groups.append((name, nodes))
            name = line[:-1].strip()
            nodes = []
            continue
        m = re.search(r"(/dev/video\d+)", line)
        if m and name is not None:
            nodes.append(m.group(1))
    if name is not None:
        groups.append((name, nodes))
    return groups


def _is_usb_v4l_card(name: str) -> bool:
    """True for UVC-style cards, e.g. ``USB2 Video: USB2 Video (usb-fe9c0000.xhci-1.3)``."""
    return bool(_USB_LIST_DEVICES_RE.search(name)) or "usb-" in name.lower()


def _sysfs_usb_video_nodes() -> List[Tuple[str, str]]:
    """Fallback: ``[(/dev/videoN, name), ...]`` whose sysfs path is under a USB device."""
    found: List[Tuple[str, str]] = []
    for sys_path in sorted(glob.glob("/sys/class/video4linux/video*")):
        base = os.path.basename(sys_path)
        dev = f"/dev/{base}"
        try:
            real = os.path.realpath(sys_path)
        except OSError:
            continue
        if "/usb" not in real.lower():
            continue
        card = base
        try:
            with open(os.path.join(sys_path, "name"), encoding="utf-8", errors="replace") as f:
                card = f.read().strip() or base
        except OSError:
            pass
        found.append((dev, card))
    return found


def list_usb_v4l_devices() -> List[Tuple[str, str]]:
    """
    USB capture candidates as ``[(device, card_name), ...]``.

    Prefers the first ``/dev/video*`` under each ``v4l2-ctl --list-devices`` USB card
    (capture node; later nodes are often metadata).
    """
    ctl = _v4l2_ctl_binary()
    if ctl:
        try:
            r = subprocess.run(
                [ctl, "--list-devices"],
                capture_output=True,
                text=True,
                timeout=5.0,
            )
            out = f"{r.stdout or ''}\n{r.stderr or ''}"
            candidates: List[Tuple[str, str]] = []
            for card, nodes in _parse_v4l2_list_devices(out):
                if not _is_usb_v4l_card(card) or not nodes:
                    continue
                candidates.append((nodes[0], card))
            if candidates:
                return candidates
        except (OSError, subprocess.TimeoutExpired):
            pass
    return _sysfs_usb_video_nodes()


def _v4l2_formats_text(device: str, timeout: float = 5.0) -> str:
    ctl = _v4l2_ctl_binary()
    if ctl is None:
        return ""
    try:
        r = subprocess.run(
            [ctl, "-d", device, "--list-formats-ext"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return f"{r.stdout or ''}\n{r.stderr or ''}"
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _detect_usb_pixel_format(device: str) -> UsbPixelFormat:
    """Use ``USB_CAMERA_FORMAT`` when set; else prefer MJPEG from v4l2, else YUY2."""
    if USB_CAMERA_FORMAT in ("mjpeg", "yuy2"):
        return USB_CAMERA_FORMAT
    text = _v4l2_formats_text(device)
    low = text.lower()
    if "mjpg" in low or "motion-jpeg" in low or "'jpeg'" in low:
        return "mjpeg"
    if "yuyv" in low or "yuy2" in low:
        return "yuy2"
    return "mjpeg"


def resolve_usb_camera_device() -> Tuple[str, str]:
    """
    Resolve ``(/dev/videoN, card_name)`` for USB streaming.

    Uses ``USB_CAMERA_DEVICE`` when set; otherwise picks the first USB UVC node whose
    card name contains ``USB_CAMERA_NAME`` (e.g. ``USB2 Video``).
    """
    pinned = (USB_CAMERA_DEVICE or "").strip()
    if pinned:
        if not os.path.exists(pinned):
            raise FileNotFoundError(f"USB camera device not found: {pinned}")
        return pinned, pinned

    name_filter = (USB_CAMERA_NAME or "").strip().lower()
    cams = list_usb_v4l_devices()
    if name_filter:
        cams = [(d, n) for d, n in cams if name_filter in n.lower()]
    if not cams:
        hint = (
            f" matching USB_CAMERA_NAME={USB_CAMERA_NAME!r}"
            if name_filter
            else " (expected a card like 'USB2 Video: USB2 Video (usb-...)')"
        )
        raise FileNotFoundError(f"No USB camera detected{hint}")
    device, card = cams[0]
    if not os.path.exists(device):
        raise FileNotFoundError(f"USB camera device not found: {device} ({card})")
    return device, card


def probe_usb_camera(timeout: float = 5.0) -> Tuple[bool, str]:
    """Return (True, "") if a USB V4L camera is usable, else (False, message)."""
    del timeout  # reserved for parity with probe_cameras
    if os.environ.get("BOX_CAMERA_SKIP_PROBE", "").strip() in ("1", "true", "yes"):
        return True, ""
    try:
        resolve_usb_camera_device()
        return True, ""
    except FileNotFoundError as e:
        return False, str(e)


def gstreamer_viewer_argv(
    port: Optional[int] = None,
    *,
    macos: bool = True,
    source: Optional[str] = "mipi",
) -> List[str]:
    """Ground-station ``gst-launch-1.0`` viewer for the Pi RTP stream (H264 or JPEG)."""
    gst = _gst_launch_binary() or "gst-launch-1.0"
    p = port if port is not None else STREAM_PORT
    src = normalize_camera_source(source)
    if macos:
        sink = ["videoconvert", "!", "video/x-raw,format=UYVY", "!", "osxvideosink", "sync=false"]
        if src == "usb":
            # Match common USB JPEG viewer: videoconvert ! osxvideosink (no UYVY filter).
            sink = ["videoconvert", "!", "osxvideosink", "sync=false"]
    else:
        sink = ["videoconvert", "!", "autovideosink", "sync=false"]
    if src == "usb":
        return [
            gst,
            "udpsrc",
            f"port={p}",
            'caps=application/x-rtp,encoding-name=JPEG,payload=26',
            "!",
            "rtpjpegdepay",
            "!",
            "jpegdec",
            "!",
            *sink,
        ]
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
    Return (True, "") if a MIPI camera appears available, else (False, message).
    OpenIPC: Majestic HTTP / process. Pi: libcamera ``rpicam-hello --list-cameras``.
    Skipped when BOX_CAMERA_SKIP_PROBE=1.
    """
    if os.environ.get("BOX_CAMERA_SKIP_PROBE", "").strip() in ("1", "true", "yes"):
        return True, ""
    try:
        from drone_control.sbc import camera_backend
    except ImportError:
        from sbc import camera_backend  # type: ignore
    if camera_backend() == "openipc":
        try:
            from drone_control.majestic import probe_majestic
        except ImportError:
            from majestic import probe_majestic  # type: ignore
        return probe_majestic(timeout=timeout)
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


def _mipi_camera_stream_argv(client_host: str) -> List[str]:
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


def _usb_camera_stream_argv(client_host: str) -> List[str]:
    """USB V4L2 → RTP/JPEG UDP (MJPEG hardware or YUY2 + jpegenc)."""
    gst = _gst_launch_binary()
    if gst is None:
        raise FileNotFoundError("gst-launch-1.0 not found in PATH")
    host = (client_host or "").strip()
    if not host:
        raise ValueError("no connected client IP for UDP stream")

    device, card = resolve_usb_camera_device()
    pix = _detect_usb_pixel_format(device)
    w = str(USB_CAMERA_WIDTH)
    h = str(USB_CAMERA_HEIGHT)
    fps = str(USB_CAMERA_FRAMERATE)
    _LOG.info("USB camera %s (%s) format=%s %sx%s@%s", device, card, pix, w, h, fps)

    if pix == "mjpeg":
        return [
            gst,
            "v4l2src",
            f"device={device}",
            "!",
            f"image/jpeg,width={w},height={h},framerate={fps}/1",
            "!",
            "rtpjpegpay",
            "!",
            "udpsink",
            f"host={host}",
            f"port={STREAM_PORT}",
        ]

    quality = str(USB_CAMERA_JPEG_QUALITY)
    return [
        gst,
        "v4l2src",
        f"device={device}",
        "!",
        f"video/x-raw,format=YUY2,width={w},height={h},framerate={fps}/1",
        "!",
        "videoconvert",
        "!",
        "jpegenc",
        f"quality={quality}",
        "!",
        "rtpjpegpay",
        "!",
        "udpsink",
        f"host={host}",
        f"port={STREAM_PORT}",
    ]


def _camera_stream_argv(client_host: str, source: CameraSource = "mipi") -> List[str]:
    if source == "usb":
        return _usb_camera_stream_argv(client_host)
    return _mipi_camera_stream_argv(client_host)


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


def _openipc_mipi() -> bool:
    try:
        from drone_control.sbc import camera_backend
    except ImportError:
        from sbc import camera_backend  # type: ignore
    return camera_backend() == "openipc"


class CameraStream:
    """
    Run/stop a camera pipeline.

    Raspberry Pi MIPI: ``rpicam-vid`` | GStreamer RTP/UDP on port 5004.
    Luckfox / OpenIPC MIPI: Majestic ``outgoing`` RTP push to the same port.
    USB: auto-detect UVC → MJPEG (or YUY2+jpegenc) → RTP/JPEG.
    """

    def __init__(self, argv: Optional[List[str]] = None):
        self._argv_override = argv
        self._client_host: Optional[str] = None
        self._source: CameraSource = "mipi"
        self._proc: Optional[subprocess.Popen] = None
        self._majestic_push = False
        self._last_error: Optional[str] = None

    def set_client_host(self, host: str) -> None:
        """UDP destination (ground-station IP from the box HTTP client)."""
        self._client_host = (host or "").strip() or None

    def set_source(self, source: Optional[str]) -> None:
        """Select ``mipi`` (default) or ``usb`` camera pipeline."""
        self._source = normalize_camera_source(source)

    @property
    def source(self) -> CameraSource:
        return self._source

    def _argv(self) -> List[str]:
        if self._argv_override is not None:
            return self._argv_override
        host = self._client_host
        if not host:
            raise ValueError("no connected client IP for UDP stream")
        return _camera_stream_argv(host, self._source)

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    @property
    def is_running(self) -> bool:
        if self._majestic_push:
            try:
                from drone_control.majestic import majestic_running
            except ImportError:
                from majestic import majestic_running  # type: ignore
            if majestic_running():
                return True
            self._majestic_push = False
            if self._last_error is None:
                self._last_error = "Majestic stopped while RTP outgoing was enabled."
            return False
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

    def _start_majestic(self) -> bool:
        try:
            from drone_control.majestic import enable_outgoing_rtp
        except ImportError:
            from majestic import enable_outgoing_rtp  # type: ignore
        host = self._client_host
        if not host:
            self._last_error = "No connected client IP (connect from the ground station first)."
            return False
        try:
            enable_outgoing_rtp(host, STREAM_PORT)
        except (OSError, ValueError, RuntimeError) as e:
            self._last_error = _normalize_camera_error(str(e))
            return False
        self._majestic_push = True
        _LOG.info("OpenIPC Majestic MIPI RTP → %s:%s", host, STREAM_PORT)
        return True

    def start(self, client_host: Optional[str] = None, source: Optional[str] = None) -> bool:
        """Start MIPI/USB streaming. Returns False if the backend fails immediately."""
        if source is not None:
            new_src = normalize_camera_source(source)
            if self.is_running and new_src != self._source:
                self.stop()
            self.set_source(new_src)
        if client_host:
            self.set_client_host(client_host)
        if self.is_running:
            return True
        self._last_error = None
        if self._argv_override is None and not self._client_host:
            self._last_error = "No connected client IP (connect from the ground station first)."
            return False
        if self._source == "mipi":
            ok_probe, probe_err = probe_cameras()
            if not ok_probe:
                self._last_error = probe_err
                return False
            if self._argv_override is None and _openipc_mipi():
                return self._start_majestic()
        elif self._source == "usb":
            ok_probe, probe_err = probe_usb_camera()
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
        """Stop Majestic RTP push and/or terminate the streamer subprocess."""
        if self._majestic_push:
            self._majestic_push = False
            try:
                from drone_control.majestic import disable_outgoing_rtp
            except ImportError:
                from majestic import disable_outgoing_rtp  # type: ignore
            try:
                disable_outgoing_rtp()
            except Exception as e:
                _LOG.warning("Majestic outgoing disable failed: %s", e)
        proc = self._proc
        self._proc = None
        if proc is None:
            self._last_error = None
            return
        _terminate_process_group(proc)
        try:
            if proc.stderr:
                proc.stderr.close()
        except Exception:
            pass
        self._last_error = None
