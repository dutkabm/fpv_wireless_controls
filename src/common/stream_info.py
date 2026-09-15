"""Playable camera stream advertised by the board, consumed by the ground-station player.

The SBC knows the live encoder (Majestic vs term-cam vs Pi RTP). It puts a
``stream`` object on ``GET /api/status``. The ground station substitutes
``{host}`` with the Target IP and builds GStreamer from ``kind`` + ``codec``.

Different cameras only change this descriptor (path, codec, width/height) —
the player does not hard-code Majestic vs thermal.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

KIND_RTSP = "rtsp"
KIND_RTP = "rtp"

CODEC_H264 = "h264"
CODEC_H265 = "h265"
CODEC_JPEG = "jpeg"


def _env(name: str, default: str) -> str:
    raw = (os.environ.get(name) or "").strip()
    return raw if raw else default


def _env_int(name: str, default: Optional[int]) -> Optional[int]:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw, 10)
    except ValueError:
        return default


def _norm_codec(raw: Optional[str], default: str = CODEC_H264) -> str:
    c = (raw or default).strip().lower().replace("hevc", CODEC_H265).replace("mjpeg", CODEC_JPEG)
    if c in (CODEC_H264, CODEC_H265, CODEC_JPEG):
        return c
    return default


def _norm_path(raw: Optional[str], default: str = "/") -> str:
    p = (raw or default).strip() or default
    if not p.startswith("/"):
        p = "/" + p
    return p


# OpenIPC Majestic HTTP/RTSP login (same as SSH on most images).
DEFAULT_RTSP_USER = "root"
DEFAULT_RTSP_PASSWORD = "12345"


def rtsp_credentials() -> Tuple[str, str]:
    """Majestic RTSP uses the HTTP login. OpenIPC default is root/12345."""
    _y_user, y_password = _creds_from_majestic_yaml()
    user = _env("BOX_RTSP_USER", "") or DEFAULT_RTSP_USER
    password = _env("BOX_RTSP_PASSWORD", "")
    if not password:
        password = y_password if y_password is not None else DEFAULT_RTSP_PASSWORD
    return user, password


def _creds_from_majestic_yaml() -> Tuple[Optional[str], Optional[str]]:
    try:
        text = open("/etc/majestic.yaml", encoding="utf-8", errors="replace").read()
    except OSError:
        return None, None
    login: Optional[str] = None
    password: Optional[str] = None
    in_http = False
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" \t"))
        line = raw.strip()
        if line.startswith("http:"):
            in_http = True
            continue
        if in_http and indent == 0 and line.endswith(":") and not line.startswith("http:"):
            in_http = False
        if not in_http:
            continue
        key, _, val = line.partition(":")
        val = val.strip().strip("'\"")
        if key == "login":
            login = val
        elif key == "password":
            password = val
    return login, password


@dataclass
class StreamInfo:
    """How a ground-station player should open the live camera."""

    kind: str  # rtsp | rtp
    codec: str  # h264 | h265 | jpeg
    port: int
    path: str = "/"
    width: Optional[int] = None
    height: Optional[int] = None
    fps: Optional[int] = None
    source: str = ""
    user: str = ""
    password: str = ""

    def url_template(self) -> str:
        if self.kind == KIND_RTSP:
            return f"rtsp://{{host}}:{int(self.port)}{self.path}"
        return f"rtp://0.0.0.0:{int(self.port)}"

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "kind": self.kind,
            "codec": self.codec,
            "port": int(self.port),
            "path": self.path,
            "url": self.url_template(),
            "source": self.source,
        }
        if self.user:
            d["user"] = self.user
        if self.password != "":
            d["password"] = self.password
        if self.width:
            d["width"] = int(self.width)
        if self.height:
            d["height"] = int(self.height)
        if self.fps:
            d["fps"] = int(self.fps)
        return d


def majestic_stream() -> StreamInfo:
    user, password = rtsp_credentials()
    return StreamInfo(
        kind=KIND_RTSP,
        codec=_norm_codec(_env("BOX_STREAM_MAJESTIC_CODEC", CODEC_H264)),
        port=_env_int("BOX_STREAM_MAJESTIC_PORT", 554) or 554,
        path=_norm_path(_env("BOX_STREAM_MAJESTIC_PATH", "/stream=0")),
        width=_env_int("BOX_STREAM_MAJESTIC_WIDTH", None),
        height=_env_int("BOX_STREAM_MAJESTIC_HEIGHT", None),
        fps=_env_int("BOX_STREAM_MAJESTIC_FPS", None),
        source="majestic",
        user=user,
        password=password,
    )


def term_cam_stream() -> StreamInfo:
    return StreamInfo(
        kind=KIND_RTSP,
        codec=_norm_codec(_env("BOX_STREAM_TERM_CODEC", CODEC_H265), CODEC_H265),
        port=_env_int("BOX_STREAM_TERM_PORT", 554) or 554,
        path=_norm_path(_env("BOX_STREAM_TERM_PATH", "/live/0")),
        width=_env_int("BOX_STREAM_TERM_WIDTH", 640),
        height=_env_int("BOX_STREAM_TERM_HEIGHT", 512),
        fps=_env_int("BOX_STREAM_TERM_FPS", 30),
        source="term-cam",
    )


def rtp_stream(source: str = "mipi") -> StreamInfo:
    src = "usb" if (source or "").strip().lower() == "usb" else "mipi"
    codec = CODEC_JPEG if src == "usb" else CODEC_H264
    return StreamInfo(
        kind=KIND_RTP,
        codec=codec,
        port=_env_int("BOX_STREAM_RTP_PORT", 5004) or 5004,
        source=src,
        width=_env_int("BOX_STREAM_RTP_WIDTH", None),
        height=_env_int("BOX_STREAM_RTP_HEIGHT", None),
        fps=_env_int("BOX_STREAM_RTP_FPS", None),
    )


def format_stream_url(stream: Dict[str, Any], host: str) -> str:
    """Turn a board ``stream`` dict into a playable URL (``{host}`` → Target IP)."""
    h = (host or "").strip()
    url = str(stream.get("url") or "").strip()
    if url:
        return url.replace("{host}", h)
    kind = str(stream.get("kind") or KIND_RTP).lower()
    port = int(stream.get("port") or (554 if kind == KIND_RTSP else 5004))
    if kind == KIND_RTSP:
        path = _norm_path(str(stream.get("path") or "/"))
        return f"rtsp://{h}:{port}{path}"
    return f"rtp://0.0.0.0:{port}"


def stream_fingerprint(stream: Optional[Dict[str, Any]]) -> Tuple[Any, ...]:
    if not stream:
        return ()
    return (
        stream.get("kind"),
        stream.get("codec"),
        stream.get("port"),
        stream.get("path"),
        stream.get("url"),
        stream.get("source"),
    )


def _video_sink(macos: bool) -> List[str]:
    if macos:
        return ["videoconvert", "!", "osxvideosink", "sync=false"]
    return ["videoconvert", "!", "autovideosink", "sync=false"]


def _codec_chain(codec: str) -> List[str]:
    c = _norm_codec(codec)
    if c == CODEC_JPEG:
        return ["rtpjpegdepay", "!", "jpegdec"]
    if c == CODEC_H265:
        return ["rtph265depay", "!", "h265parse", "!", "avdec_h265"]
    return ["rtph264depay", "!", "h264parse", "!", "avdec_h264"]


def gstreamer_play_argv(
    stream: Dict[str, Any],
    *,
    host: str,
    gst_bin: str = "gst-launch-1.0",
    macos: bool = False,
) -> List[str]:
    """``gst-launch-1.0`` argv for the advertised stream (RTSP or RTP)."""
    kind = str(stream.get("kind") or KIND_RTP).strip().lower()
    codec = _norm_codec(stream.get("codec"))
    sink = _video_sink(macos)
    if kind == KIND_RTSP:
        url = format_stream_url(stream, host)
        chain = _codec_chain(codec)
        # Quote location: gst-launch splits on '=' so /stream=0 would be dropped.
        # TCP interleaved: RTSP-UDP (protocols=udp) does SETUP/PLAY then never
        # receives RTP — "Redistribute latency" then a generic connect error.
        proto = _env("BOX_RTSP_PROTOCOLS", "tcp")
        argv = [
            gst_bin,
            "rtspsrc",
            f'location="{url}"',
            "latency=80",
            f"protocols={proto}",
            "drop-on-latency=true",
        ]
        user = str(stream.get("user") or "").strip()
        password = str(stream.get("password") or "")
        if not user and str(stream.get("source") or "") in ("majestic", "mipi", ""):
            user, password = DEFAULT_RTSP_USER, DEFAULT_RTSP_PASSWORD
        if user:
            argv.extend([f"user-id={user}", f"user-pw={password}"])
        argv.extend(["!", "queue", "!", *chain, "!", *sink])
        return argv
    port = int(stream.get("port") or 5004)
    if codec == CODEC_JPEG:
        caps = "application/x-rtp,encoding-name=JPEG,payload=26"
    elif codec == CODEC_H265:
        caps = "application/x-rtp,encoding-name=H265,payload=96"
    else:
        caps = "application/x-rtp,payload=96,encoding-name=H264"
    return [
        gst_bin,
        "udpsrc",
        f"port={port}",
        f"caps={caps}",
        "!",
        *_codec_chain(codec),
        "!",
        *sink,
    ]
