"""
OpenIPC Majestic MIPI camera: push H264 RTP/UDP to the ground station.

Majestic already owns the CSI sensor on Luckfox Pico Pro/Max OpenIPC images.
Toggling Video on the Box tab sets ``outgoing.server`` to ``udp://<client>:5004``
(same RTP H264 the Pi ``rpicam-vid`` path uses).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import Any, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

_LOG = logging.getLogger(__name__)

_DEFAULT_BASE = "http://127.0.0.1"
_CONFIG_PATH = "/api/v1/config"
_SET_PATH = "/api/v1/set"
_SCHEMA_PATH = "/api/v1/config.json"


def majestic_base_url() -> str:
    raw = (os.environ.get("BOX_MAJESTIC_URL") or _DEFAULT_BASE).strip()
    return raw.rstrip("/") or _DEFAULT_BASE


def majestic_running() -> bool:
    try:
        r = subprocess.run(
            ["pidof", "majestic"],
            capture_output=True,
            timeout=1.5,
        )
        if r.returncode == 0 and (r.stdout or b"").strip():
            return True
    except (OSError, subprocess.TimeoutExpired):
        pass
    return _http_ok(_SCHEMA_PATH, timeout=0.8) or _http_ok(_CONFIG_PATH, timeout=0.8)


def _http_ok(path: str, timeout: float = 2.0) -> bool:
    try:
        req = Request(majestic_base_url() + path, method="GET")
        with urlopen(req, timeout=timeout) as resp:
            return 200 <= int(resp.status) < 300
    except (OSError, URLError, HTTPError, ValueError):
        return False


def probe_majestic(timeout: float = 2.0) -> Tuple[bool, str]:
    """Return (True, "") if Majestic will accept an outgoing RTP push."""
    if os.environ.get("BOX_CAMERA_SKIP_PROBE", "").strip().lower() in ("1", "true", "yes"):
        return True, ""
    if _http_ok(_SCHEMA_PATH, timeout=timeout) or _http_ok(_CONFIG_PATH, timeout=timeout):
        return True, ""
    if majestic_running():
        return True, ""
    yaml_path = "/etc/majestic.yaml"
    if os.path.exists(yaml_path):
        return False, (
            "OpenIPC Majestic config is present but the streamer is not reachable on "
            f"{majestic_base_url()}. Start Majestic (or set BOX_MAJESTIC_URL)."
        )
    return False, (
        "OpenIPC Majestic not found. On Luckfox Pico Pro/Max the MIPI camera is "
        "owned by Majestic — do not use rpicam-vid. Flash OpenIPC and keep Majestic running."
    )


def _request(method: str, path: str, body: Optional[bytes] = None, timeout: float = 4.0) -> bytes:
    headers = {}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = Request(majestic_base_url() + path, data=body, headers=headers, method=method)
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.read() or b""
    except HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")[-400:]
        raise RuntimeError(f"Majestic HTTP {e.code} {path}: {err_body or e.reason}") from e
    except URLError as e:
        raise RuntimeError(f"Majestic HTTP {path} failed: {e.reason}") from e


def _post_config(payload: dict[str, Any]) -> None:
    _request("POST", _CONFIG_PATH, json.dumps(payload).encode("utf-8"))


def _set_query(**kwargs: Any) -> None:
    """Fallback ``/api/v1/set?key=value`` (older Majestic)."""
    q = urlencode({k: str(v) for k, v in kwargs.items()}, quote_via=quote)
    _request("GET", f"{_SET_PATH}?{q}")


def enable_outgoing_rtp(client_host: str, port: int) -> None:
    """Point Majestic ``outgoing`` at ``udp://client_host:port`` (RTP H264)."""
    host = (client_host or "").strip()
    if not host:
        raise ValueError("no connected client IP for Majestic RTP push")
    server = f"udp://{host}:{int(port)}"
    payload = {
        "video0": {"codec": "h264"},
        "outgoing": {
            "enabled": True,
            "server": server,
            "naluSize": 1200,
        },
    }
    try:
        _post_config(payload)
        _LOG.info("Majestic outgoing RTP → %s", server)
        return
    except RuntimeError as e:
        _LOG.warning("Majestic POST /api/v1/config failed (%s); trying /api/v1/set", e)
    try:
        _set_query(**{"video0.codec": "h264"})
        _set_query(**{"outgoing.server": server, "outgoing.enabled": "true", "outgoing.naluSize": "1200"})
        _LOG.info("Majestic outgoing RTP (set API) → %s", server)
    except RuntimeError:
        _set_query(**{"outgoing.server": server})
        _set_query(**{"outgoing.enabled": "true"})
        _LOG.info("Majestic outgoing RTP (set API, minimal) → %s", server)


def disable_outgoing_rtp() -> None:
    try:
        _post_config({"outgoing": {"enabled": False}})
        _LOG.info("Majestic outgoing RTP disabled")
        return
    except RuntimeError as e:
        _LOG.warning("Majestic POST disable failed (%s); trying /api/v1/set", e)
    _set_query(**{"outgoing.enabled": "false"})
    _LOG.info("Majestic outgoing RTP disabled (set API)")
