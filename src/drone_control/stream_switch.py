"""
OpenIPC: switch MIPI Majestic vs USB term-cam from an RC channel.

RV1106 rockit/VENC is exclusive. Do not keep both processes MPI-initialized,
and do not SIGSTOP the idle one — it still holds the encoder. Both binaries
stay on disk; drone-control fully stops one and starts the other.

Default: low PWM → Majestic, high PWM → term-cam.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from typing import Optional, Sequence, Tuple

_LOG = logging.getLogger(__name__)

SOURCE_MAJESTIC = "majestic"
SOURCE_TERM_CAM = "term-cam"

# RC PWM microseconds. Mid-stick / unused (1500) stays in the hysteresis band.
DEFAULT_CHANNEL = 8
DEFAULT_LOW_US = 1400
DEFAULT_HIGH_US = 1600

_SWITCH_BIN = "/usr/sbin/switch-video"
_MAJESTIC_INIT = "/etc/init.d/S95majestic"
_TERM_INIT = "/etc/init.d/S96term-cam"


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw, 10)
    except ValueError:
        return default


def load_video_rc_from_config(config_path: str) -> Tuple[int, int, int]:
    """Return (channel 1-16 or 0=off, low_us, high_us). Env overrides the INI."""
    channel = DEFAULT_CHANNEL
    low_us = DEFAULT_LOW_US
    high_us = DEFAULT_HIGH_US
    if config_path and os.path.exists(config_path):
        import configparser

        cfg = configparser.ConfigParser()
        cfg.read(config_path)
        if "General" in cfg:
            g = cfg["General"]

            def _get(key: str, fallback: int) -> int:
                raw = g.get(key, fallback=str(fallback))
                if "#" in raw:
                    raw = raw.split("#", 1)[0]
                try:
                    return int(raw.strip(), 10)
                except ValueError:
                    return fallback

            channel = _get("video_rc_channel", channel)
            low_us = _get("video_rc_low_us", low_us)
            high_us = _get("video_rc_high_us", high_us)
    channel = _env_int("BOX_VIDEO_RC_CHANNEL", channel)
    low_us = _env_int("BOX_VIDEO_RC_LOW_US", low_us)
    high_us = _env_int("BOX_VIDEO_RC_HIGH_US", high_us)
    if channel < 0 or channel > 16:
        channel = 0
    if high_us <= low_us:
        high_us = low_us + 200
    return channel, low_us, high_us


def _pid_running(name: str) -> bool:
    try:
        r = subprocess.run(
            ["pidof", name],
            capture_output=True,
            timeout=1.5,
        )
        return r.returncode == 0 and bool((r.stdout or b"").strip())
    except (OSError, subprocess.TimeoutExpired):
        return False


def current_source() -> str:
    if _pid_running("term-cam"):
        return SOURCE_TERM_CAM
    if _pid_running("majestic"):
        return SOURCE_MAJESTIC
    return "none"


def current_stream_info() -> Optional[dict]:
    """Playable stream for the live encoder, or None if neither process is up."""
    try:
        from common.stream_info import majestic_stream, term_cam_stream
    except ImportError:
        return None
    src = current_source()
    if src == SOURCE_TERM_CAM:
        return term_cam_stream().to_dict()
    if src == SOURCE_MAJESTIC:
        return majestic_stream().to_dict()
    return None


def _run(argv: Sequence[str], env: Optional[dict] = None, timeout: float = 20.0) -> bool:
    try:
        r = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        _LOG.warning("video switch %s failed: %s", argv, e)
        return False
    if r.returncode != 0:
        tail = ((r.stderr or "") + "\n" + (r.stdout or "")).strip()[-400:]
        _LOG.warning("video switch %s exited %s: %s", argv, r.returncode, tail)
        return False
    return True


def apply_source(want: str) -> bool:
    """Fully stop the idle encoder process, then start ``want``."""
    want = SOURCE_TERM_CAM if want == SOURCE_TERM_CAM else SOURCE_MAJESTIC
    have = current_source()
    if have == want:
        return True
    _LOG.info("video switch %s → %s", have, want)
    if os.path.isfile(_SWITCH_BIN) and os.access(_SWITCH_BIN, os.X_OK):
        ok = _run([_SWITCH_BIN, want])
    elif want == SOURCE_TERM_CAM:
        env = os.environ.copy()
        env["TERM_CAM_FORCE"] = "1"
        if os.path.isfile(_MAJESTIC_INIT):
            _run([_MAJESTIC_INIT, "stop"], timeout=12.0)
        else:
            subprocess.run(["killall", "majestic"], capture_output=True, timeout=3.0)
        time.sleep(0.6)
        ok = _run([_TERM_INIT, "start"], env=env) if os.path.isfile(_TERM_INIT) else False
    else:
        if os.path.isfile(_TERM_INIT):
            _run([_TERM_INIT, "stop"], timeout=12.0)
        else:
            subprocess.run(["killall", "term-cam"], capture_output=True, timeout=3.0)
        time.sleep(0.6)
        ok = _run([_MAJESTIC_INIT, "start"]) if os.path.isfile(_MAJESTIC_INIT) else False
    got = current_source()
    if got != want:
        _LOG.warning("video switch wanted %s, now %s (ok=%s)", want, got, ok)
        return False
    _sync_box_source(want)
    return True


def _sync_box_source(source: str) -> None:
    try:
        from drone_control import box_server
    except ImportError:
        return
    cam_src = "usb" if source == SOURCE_TERM_CAM else "mipi"
    try:
        with box_server.STATE.lock:
            box = box_server.STATE.box
            if box is not None and getattr(box, "camera_stream", None) is not None:
                box.camera_stream.set_source(cam_src)
    except Exception:
        _LOG.debug("box camera_source sync failed", exc_info=True)


def available() -> bool:
    """True when this board can switch Majestic / term-cam."""
    try:
        from drone_control.sbc import camera_backend
    except ImportError:
        from sbc import camera_backend  # type: ignore
    if camera_backend() != "openipc":
        return False
    return shutil.which("majestic") is not None or os.path.isfile("/usr/bin/majestic")


class RcVideoSwitcher:
    """Watch one RC channel (1-16) and swap encoder processes off the CRSF path."""

    def __init__(self, channel: int, low_us: int = DEFAULT_LOW_US, high_us: int = DEFAULT_HIGH_US):
        self.channel = int(channel)
        self.low_us = int(low_us)
        self.high_us = int(high_us)
        self._lock = threading.Lock()
        self._want = SOURCE_MAJESTIC
        self._active = current_source() if available() else SOURCE_MAJESTIC
        if self._active == "none":
            self._active = SOURCE_MAJESTIC
        self._busy = False
        self._logged_skip = False

    def note_channels(self, channels: Optional[Sequence[int]]) -> None:
        if self.channel < 1 or self.channel > 16 or not channels:
            return
        if len(channels) < self.channel:
            return
        pwm = int(channels[self.channel - 1])
        if pwm >= self.high_us:
            want = SOURCE_TERM_CAM
        elif pwm <= self.low_us:
            want = SOURCE_MAJESTIC
        else:
            return
        self._request(want)

    def _request(self, want: str) -> None:
        with self._lock:
            self._want = want
            if want == self._active and not self._busy:
                return
            if self._busy:
                return
            self._busy = True
        threading.Thread(target=self._worker, name="video-rc-switch", daemon=True).start()

    def _worker(self) -> None:
        if not available():
            if not self._logged_skip:
                _LOG.info("RC video switch skipped (not OpenIPC / no majestic)")
                self._logged_skip = True
            with self._lock:
                self._busy = False
            return
        while True:
            with self._lock:
                want = self._want
                if want == self._active:
                    self._busy = False
                    return
            apply_source(want)
            with self._lock:
                self._active = current_source()
                if self._active == "none":
                    self._active = want
                if self._want == self._active:
                    self._busy = False
                    return
