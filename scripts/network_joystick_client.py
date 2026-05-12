#!/usr/bin/env python3
"""
PC / laptop: read a local joystick (controller_map.txt + on-screen map table), show 16 channel meters,
TCP-handshake with the Pi bridge on Connect, then UDP-send 16-channel frames at the configured rate.
"""

from __future__ import annotations

import argparse
import configparser
import ipaddress
import logging
import os
import socket
import sys
import threading
import time
from typing import Dict, List, Optional, Tuple

import customtkinter as ctk

# macOS: Pygame/SDL installs SDLApplication as NSApplication before Tk inits CustomTkinter,
# which breaks Tk's Aqua backend (crash in TkpGetColor → macOSVersion). Headless video avoids
# that; joystick input still works. Override with SDL_VIDEODRIVER in the environment if needed.
if sys.platform == "darwin" and "SDL_VIDEODRIVER" not in os.environ:
    os.environ["SDL_VIDEODRIVER"] = "dummy"

import pygame

from network_rc_protocol import (
    DEFAULT_HANDSHAKE_TCP_PORT,
    DEFAULT_UDP_CHANNEL_PORT,
    HANDSHAKE_LINE,
    pack_channel_datagram,
)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_LOG = logging.getLogger(__name__)

_DEFAULT_MAP = os.path.join(SCRIPT_DIR, "controller_map.txt")
if not os.path.exists(_DEFAULT_MAP):
    _alt_map = os.path.join(SCRIPT_DIR, "controler_map.txt")
    if os.path.exists(_alt_map):
        _DEFAULT_MAP = _alt_map


def _strip_inline_comment(value: Optional[str]) -> str:
    """ConfigParser does not strip inline '# ...' comments; parse after removing them."""
    if value is None:
        return ""
    s = str(value).strip()
    if "#" in s:
        s = s.split("#", 1)[0].strip()
    return s


def map_axis(value):
    return int(1500 + value * 500)


def map_button(value):
    return 2000 if value else 1000


def get_pwm_channels_from_joystick(
    joystick: Optional[pygame.joystick.Joystick],
    axis_channel_map: Dict,
    button_channel_map: Dict,
    hat_channel_map: Dict,
) -> List[int]:
    if threading.current_thread() is threading.main_thread():
        pygame.event.pump()

    channels = [1500] * 16
    if not joystick:
        return channels

    axis_values = {}
    for i in range(joystick.get_numaxes()):
        axis_values[f"axis_{i}"] = joystick.get_axis(i)

    for axis_key, mapping in axis_channel_map.items():
        channel_num = mapping["channel"]
        invert = mapping.get("invert", False)
        value = map_axis(axis_values.get(axis_key, 0.0))
        if invert:
            value = 3000 - value
        channels[channel_num - 1] = value

    button_values = {}
    for i in range(joystick.get_numbuttons()):
        button_values[f"button_{i}"] = joystick.get_button(i)

    for button_key, mapping in button_channel_map.items():
        channel_num = mapping["channel"]
        invert = mapping.get("invert", False)
        value = map_button(button_values.get(button_key, 0))
        if invert:
            value = 3000 - value
        channels[channel_num - 1] = value

    hat_values = {}
    for i in range(joystick.get_numhats()):
        hat = joystick.get_hat(i)
        hat_values[f"hat_{i}_x"] = hat[0]
        hat_values[f"hat_{i}_y"] = hat[1]

    for hat_key_x, mapping in hat_channel_map["x"].items():
        channel_num = mapping["channel"]
        invert = mapping.get("invert", False)
        value = 1500 + hat_values.get(hat_key_x, 0) * 500
        if invert:
            value = 3000 - value
        channels[channel_num - 1] = value

    for hat_key_y, mapping in hat_channel_map["y"].items():
        channel_num = mapping["channel"]
        invert = mapping.get("invert", False)
        value = 1500 + hat_values.get(hat_key_y, 0) * 500
        if invert:
            value = 3000 - value
        channels[channel_num - 1] = value

    for i in range(16):
        channels[i] = max(1000, min(2000, channels[i]))
    return channels


def load_controller_config(config_path: str) -> Tuple[Optional[int], Dict, Dict, Dict]:
    default_joystick_index: Optional[int] = None
    axis_map: Dict = {}
    button_map: Dict = {}
    hat_map = {"x": {}, "y": {}}

    if not os.path.exists(config_path):
        return default_joystick_index, axis_map, button_map, hat_map

    config = configparser.ConfigParser()
    config.read(config_path)

    if "General" in config:
        general = config["General"]
        _ji = _strip_inline_comment(general.get("joystick_index", fallback="-1")).strip().lower()
        if _ji == "auto":
            pygame.joystick.init()
            default_joystick_index = 0 if pygame.joystick.get_count() > 0 else None
        else:
            try:
                ji = int(_ji)
            except ValueError:
                ji = -1
            default_joystick_index = None if ji < 0 else ji

    if "AxisMappings" in config:
        for axis_key in config["AxisMappings"]:
            mapping_str = _strip_inline_comment(config["AxisMappings"][axis_key])
            parts = [p.strip() for p in mapping_str.split(",")]
            mapping = {}
            for part in parts:
                if ":" in part:
                    k, v = part.split(":", 1)
                    k = k.strip().lower()
                    v = v.strip().lower()
                    if k == "channel":
                        mapping["channel"] = int(v)
                    elif k == "invert":
                        mapping["invert"] = v == "true"
            if "channel" in mapping:
                axis_map[axis_key] = mapping

    if "ButtonMappings" in config:
        for button_key in config["ButtonMappings"]:
            mapping_str = _strip_inline_comment(config["ButtonMappings"][button_key])
            parts = [p.strip() for p in mapping_str.split(",")]
            mapping = {}
            for part in parts:
                if ":" in part:
                    k, v = part.split(":", 1)
                    k = k.strip().lower()
                    v = v.strip().lower()
                    if k == "channel":
                        mapping["channel"] = int(v)
                    elif k == "invert":
                        mapping["invert"] = v == "true"
            if "channel" in mapping:
                button_map[button_key] = mapping

    if "HatMappings" in config:
        for hat_key in config["HatMappings"]:
            mapping_str = _strip_inline_comment(config["HatMappings"][hat_key])
            parts = [p.strip() for p in mapping_str.split(",")]
            mapping = {}
            for part in parts:
                if ":" in part:
                    k, v = part.split(":", 1)
                    k = k.strip().lower()
                    v = v.strip().lower()
                    if k == "channel":
                        mapping["channel"] = int(v)
                    elif k == "invert":
                        mapping["invert"] = v == "true"
            if "channel" in mapping:
                if hat_key.endswith("_x"):
                    hat_map["x"][hat_key] = mapping
                elif hat_key.endswith("_y"):
                    hat_map["y"][hat_key] = mapping

    return default_joystick_index, axis_map, button_map, hat_map


def prune_joystick_mappings(
    joystick: Optional[pygame.joystick.Joystick],
    axis_map: Dict,
    button_map: Dict,
    hat_map: Dict,
) -> None:
    """Drop INI mappings for controls this joystick does not have (prevents ghost axes on CH5+)."""
    if joystick is None:
        return
    n_ax = joystick.get_numaxes()
    n_btn = joystick.get_numbuttons()
    n_hat = joystick.get_numhats()
    removed = 0

    for key in list(axis_map.keys()):
        if not key.startswith("axis_"):
            continue
        tail = key[5:]
        if not tail.isdigit():
            continue
        if int(tail) >= n_ax:
            del axis_map[key]
            removed += 1

    for key in list(button_map.keys()):
        if not key.startswith("button_"):
            continue
        tail = key[7:]
        if not tail.isdigit():
            continue
        if int(tail) >= n_btn:
            del button_map[key]
            removed += 1

    for side in ("x", "y"):
        hm = hat_map[side]
        for key in list(hm.keys()):
            parts = key.split("_")
            if len(parts) != 3 or parts[0] != "hat" or parts[2] != side:
                continue
            if not parts[1].isdigit():
                continue
            if int(parts[1]) >= n_hat:
                del hm[key]
                removed += 1

    if removed:
        _LOG.info(
            "Removed %d controller_map mapping(s) not present on this device (frees CH5+ for real inputs).",
            removed,
        )


def _used_channels(axis_map: Dict, button_map: Dict, hat_map: Dict) -> set[int]:
    u: set[int] = set()
    for m in axis_map.values():
        u.add(m["channel"])
    for m in button_map.values():
        u.add(m["channel"])
    for m in hat_map["x"].values():
        u.add(m["channel"])
    for m in hat_map["y"].values():
        u.add(m["channel"])
    return u


def apply_rc_preset_mappings(
    joystick: Optional[pygame.joystick.Joystick],
    axis_map: Dict,
    button_map: Dict,
    hat_map: Dict,
) -> int:
    """Typical gamepad layout: axis 0–3 → CH1–4 (roll/pitch/yaw/throttle), buttons 0–3 → CH5–8 when free."""
    if joystick is None:
        return 0
    used = _used_channels(axis_map, button_map, hat_map)
    added = 0
    n_ax = joystick.get_numaxes()
    n_btn = joystick.get_numbuttons()
    for i in range(min(4, n_ax)):
        key = f"axis_{i}"
        ch = i + 1
        if key not in axis_map and ch not in used:
            axis_map[key] = {"channel": ch, "invert": False}
            used.add(ch)
            added += 1
    for i in range(min(4, n_btn)):
        key = f"button_{i}"
        ch = 5 + i
        if key not in button_map and ch not in used:
            button_map[key] = {"channel": ch, "invert": False}
            used.add(ch)
            added += 1
    return added


def merge_default_joystick_mappings(
    joystick: Optional[pygame.joystick.Joystick],
    axis_map: Dict,
    button_map: Dict,
    hat_map: Dict,
) -> int:
    """Map any physical control not listed in controller_map to the next free RC channel (1..16).

    Same idea as assigning inputs in ``minirex_pygame_no_config_file.py``: axes, buttons, and hat
    directions that have no INI entry still drive spare outputs so CH5+ are not stuck at 1500.
    Explicit INI mappings are kept; only missing keys get auto channels.
    """
    if joystick is None:
        return 0

    def used_channels() -> set[int]:
        return _used_channels(axis_map, button_map, hat_map)

    used = used_channels()

    def next_free() -> Optional[int]:
        for c in range(1, 17):
            if c not in used:
                return c
        return None

    added = 0

    for i in range(joystick.get_numaxes()):
        key = f"axis_{i}"
        if key not in axis_map:
            c = next_free()
            if c is None:
                break
            axis_map[key] = {"channel": c, "invert": False}
            used.add(c)
            added += 1

    for i in range(joystick.get_numbuttons()):
        key = f"button_{i}"
        if key not in button_map:
            c = next_free()
            if c is None:
                break
            button_map[key] = {"channel": c, "invert": False}
            used.add(c)
            added += 1

    for i in range(joystick.get_numhats()):
        kx = f"hat_{i}_x"
        if kx not in hat_map["x"]:
            c = next_free()
            if c is None:
                return added
            hat_map["x"][kx] = {"channel": c, "invert": False}
            used.add(c)
            added += 1
        ky = f"hat_{i}_y"
        if ky not in hat_map["y"]:
            c = next_free()
            if c is None:
                return added
            hat_map["y"][ky] = {"channel": c, "invert": False}
            used.add(c)
            added += 1

    return added


def open_joystick(
    index: Optional[int],
    previous: Optional[pygame.joystick.Joystick] = None,
) -> Optional[pygame.joystick.Joystick]:
    """Open a joystick by index. Quits ``previous`` only — avoids pygame.joystick.quit()/init() each time (hangs on macOS + Tk)."""
    if previous is not None:
        try:
            previous.quit()
        except Exception:
            pass
    if index is None or index < 0 or index >= pygame.joystick.get_count():
        return None
    if not pygame.joystick.get_init():
        pygame.joystick.init()
    j = pygame.joystick.Joystick(index)
    j.init()
    return j


def _joy_menu_values() -> List[str]:
    n = pygame.joystick.get_count()
    if n == 0:
        return ["(no joystick)"]
    return [f"{i}: {pygame.joystick.Joystick(i).get_name()}" for i in range(n)]


def _validate_ipv4_text(text: str) -> Tuple[Optional[str], str]:
    """Return (normalized_ipv4, "") if valid, else (None, error_message)."""
    t = text.strip()
    if not t:
        return None, "Set target IP"
    try:
        return str(ipaddress.IPv4Address(t)), ""
    except ipaddress.AddressValueError:
        return None, "Invalid IPv4 address"


def tcp_handshake(host: str, tcp_port: int, timeout: float = 5.0) -> Tuple[bool, str]:
    """Connect to the bridge TCP port, send HANDSHAKE_LINE, expect response starting with OK."""
    _LOG.info("TCP handshake: connecting to %s:%s", host, tcp_port)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, tcp_port))
        s.sendall(HANDSHAKE_LINE)
        resp = s.recv(64)
        if not resp.startswith(b"OK"):
            _LOG.warning(
                "TCP handshake: unexpected reply from %s:%s: %r",
                host,
                tcp_port,
                resp,
            )
            return False, "Handshake failed (unexpected reply)"
        _LOG.info("TCP handshake: OK from %s:%s", host, tcp_port)
        return True, ""
    except OSError as e:
        _LOG.warning("TCP handshake: failed %s:%s: %s", host, tcp_port, e)
        return False, str(e)
    finally:
        try:
            s.close()
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser(description="Joystick → UDP client for Mini Rex network bridge")
    ap.add_argument("--config", default=_DEFAULT_MAP, help="Mapping INI path (Mini Rex joystick map)")
    ap.add_argument("--target-ip", default="", help="Default Pi / bridge IP (editable in UI)")
    ap.add_argument(
        "--target-port",
        type=int,
        default=DEFAULT_UDP_CHANNEL_PORT,
        help=f"UDP channel port (fixed in UI; must match bridge, default {DEFAULT_UDP_CHANNEL_PORT})",
    )
    ap.add_argument(
        "--handshake-port",
        type=int,
        default=DEFAULT_HANDSHAKE_TCP_PORT,
        help=f"TCP handshake port (default {DEFAULT_HANDSHAKE_TCP_PORT})",
    )
    ap.add_argument("--hz", type=float, default=50.0, help="Send rate")
    ap.add_argument("--debug", action="store_true", help="Log each UDP send at DEBUG")
    ap.add_argument("--print-raw", action="store_true", help="Print raw axes/buttons/hats to stdout (throttled)")
    ap.add_argument(
        "--print-raw-hz",
        type=float,
        default=5.0,
        help="Max print lines per second with --print-raw (default 5)",
    )
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    pygame.init()
    pygame.joystick.init()

    joy_index, axis_map, button_map, hat_map = load_controller_config(args.config)
    joystick = open_joystick(joy_index, None)
    prune_joystick_mappings(joystick, axis_map, button_map, hat_map)
    n_pre = apply_rc_preset_mappings(joystick, axis_map, button_map, hat_map)
    n_auto = merge_default_joystick_mappings(joystick, axis_map, button_map, hat_map)
    if n_pre or n_auto:
        _LOG.info(
            "Joystick map: preset added %d, auto-filled %d (edit table in UI or --config).",
            n_pre,
            n_auto,
        )

    send_interval = 1.0 / max(args.hz, 1.0)
    acc = 0.0
    last_tick = time.monotonic()

    sock: Optional[socket.socket] = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError as e:
        last_err = str(e)
        status_text = "Socket error"
    else:
        last_err = ""
        status_text = "Idle"

    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")

    root = ctk.CTk()
    root.title("Network FPV Controller")
    root.geometry("1320x620")
    root.minsize(1000, 520)
    root.grid_columnconfigure(0, weight=1)

    title = ctk.CTkLabel(
        root,
        text="Network joystick → Pi bridge (TCP connect · UDP channels)",
        font=ctk.CTkFont(size=18, weight="bold"),
    )
    title.grid(row=0, column=0, padx=16, pady=(16, 8), sticky="w")

    form = ctk.CTkFrame(root, fg_color="transparent")
    form.grid(row=1, column=0, padx=16, pady=4, sticky="ew")
    form.grid_columnconfigure(1, weight=1)

    ctk.CTkLabel(form, text="Target IP").grid(row=0, column=0, padx=(0, 8), pady=4, sticky="w")
    ip_entry = ctk.CTkEntry(form, placeholder_text="IPv4 e.g. 192.168.1.10", width=280)
    ip_entry.grid(row=0, column=1, padx=0, pady=4, sticky="ew")
    ip_entry.insert(0, args.target_ip.strip())

    connect_var = ctk.StringVar(value="Connect")
    connect_btn = ctk.CTkButton(form, textvariable=connect_var, width=120)
    connect_btn.grid(row=0, column=2, padx=(12, 0), pady=4, sticky="e")

    ctk.CTkLabel(form, text="Joystick").grid(row=1, column=0, padx=(0, 8), pady=4, sticky="nw")

    joy_row = ctk.CTkFrame(form, fg_color="transparent")
    joy_row.grid(row=1, column=1, columnspan=2, padx=0, pady=4, sticky="ew")
    joy_row.grid_columnconfigure(0, weight=1)

    joy_var = ctk.StringVar(value="(no joystick)")
    menu_vals = _joy_menu_values()
    if joystick is not None and joy_index is not None:
        cand = f"{joy_index}: {joystick.get_name()}"
        if cand in menu_vals:
            joy_var.set(cand)
        elif menu_vals:
            joy_var.set(menu_vals[0])

    sending = False
    bridge_connected = False
    handshake_busy = False

    content = ctk.CTkFrame(root, fg_color="transparent")
    content.grid(row=2, column=0, sticky="nsew", padx=16, pady=(2, 6))
    root.grid_rowconfigure(2, weight=1)
    content.grid_columnconfigure(1, weight=1)
    content.grid_rowconfigure(0, weight=1)

    channels_row_frame = ctk.CTkFrame(
        content,
        corner_radius=10,
        border_width=1,
        border_color=("gray65", "gray38"),
        fg_color=("gray93", "gray19"),
    )
    channels_row_frame.grid(row=0, column=0, sticky="nw")
    channels_row_frame.grid_columnconfigure(0, weight=1)

    map_scroll = ctk.CTkScrollableFrame(
        content,
        label_text="Joystick Channels (CH1–4 sticks, CH5–8 buttons preset; assign extra axes as switches)",
        width=460,
        height=360,
    )
    map_scroll.grid(row=0, column=1, sticky="nsew", padx=(12, 0))

    elrs_title = ctk.CTkLabel(
        channels_row_frame,
        text="ELRS Channels",
        font=ctk.CTkFont(size=13),
        anchor="center",
        corner_radius=6,
        height=25,
        fg_color=("gray78", "gray23"),    
    )
    elrs_title.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 2))

    channels_inner = ctk.CTkFrame(channels_row_frame, fg_color="transparent")
    channels_inner.grid(row=1, column=0, padx=4, pady=(0, 6))

    channel_bars: List[ctk.CTkProgressBar] = []
    channel_value_labels: List[ctk.CTkLabel] = []
    cell_bg = ("gray90", "gray22")
    cell_border = ("gray65", "gray38")
    bar_w, bar_h = 20, 88
    _mono = "Menlo" if sys.platform == "darwin" else "Consolas" if os.name == "nt" else "DejaVu Sans Mono"
    font_ch = ctk.CTkFont(family=_mono, size=11, weight="bold")
    font_val = ctk.CTkFont(family=_mono, size=11)
    channel_gap = 2

    for i in range(16):
        cell = ctk.CTkFrame(
            channels_inner,
            corner_radius=7,
            border_width=1,
            border_color=cell_border,
            fg_color=cell_bg,
        )
        pad_l = channel_gap if i == 0 else 0
        pad_r = channel_gap if i < 15 else 0
        cell.pack(side="left", padx=(pad_l, pad_r), pady=0)
        ctk.CTkLabel(
            cell,
            text=str(i + 1),
            font=font_ch,
            width=22,
            anchor="center",
        ).pack(pady=(4, 2))
        bar = ctk.CTkProgressBar(
            cell,
            width=bar_w,
            height=bar_h,
            orientation="vertical",
        )
        bar.pack(pady=2)
        bar.set(0.5)
        channel_bars.append(bar)
        vl = ctk.CTkLabel(
            cell,
            text="1500",
            font=font_val,
            text_color=("gray30", "gray75"),
            width=40,
            anchor="center",
        )
        vl.pack(pady=(0, 5))
        channel_value_labels.append(vl)

    mapping_live_labels: Dict[str, ctk.CTkLabel] = {}

    _ch_labels = (
        "Off",
        "1 roll",
        "2 pitch",
        "3 yaw",
        "4 throttle",
        "5",
        "6",
        "7",
        "8",
        "9",
        "10",
        "11",
        "12",
        "13",
        "14",
        "15",
        "16",
    )

    def _channel_menu_value(ch: Optional[int]) -> str:
        if ch is None or ch < 1 or ch > 16:
            return "Off"
        return _ch_labels[ch]

    def _parse_channel_menu(val: str) -> Optional[int]:
        if val == "Off":
            return None
        try:
            return int(val.split()[0])
        except (ValueError, IndexError):
            return None

    def _mapping_row_target(key: str) -> Tuple[Dict, str]:
        if key.startswith("axis_"):
            return axis_map, key
        if key.startswith("button_"):
            return button_map, key
        if key.endswith("_x") and key.startswith("hat_"):
            return hat_map["x"], key
        if key.endswith("_y") and key.startswith("hat_"):
            return hat_map["y"], key
        return axis_map, key

    def _set_mapping_channel(key: str, menu_val: str) -> None:
        d, k = _mapping_row_target(key)
        ch = _parse_channel_menu(menu_val)
        if ch is None:
            d.pop(k, None)
        else:
            inv = d.get(k, {}).get("invert", False) if k in d else False
            d[k] = {"channel": ch, "invert": inv}

    def _set_mapping_invert(key: str, inv: bool) -> None:
        d, k = _mapping_row_target(key)
        if k not in d:
            return
        d[k]["invert"] = bool(inv)

    def rebuild_mapping_ui() -> None:
        mapping_live_labels.clear()
        for w in map_scroll.winfo_children():
            w.destroy()
        hdr = ctk.CTkFrame(map_scroll, fg_color="transparent")
        hdr.pack(fill="x", pady=(0, 4))
        ctk.CTkLabel(hdr, text="Control", width=200, anchor="w", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=0, padx=4
        )
        ctk.CTkLabel(hdr, text="Channel", width=120, anchor="w", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=1, padx=4
        )
        ctk.CTkLabel(hdr, text="Inv", width=36, anchor="center", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=2, padx=4
        )
        ctk.CTkLabel(hdr, text="Raw", width=100, anchor="w", font=ctk.CTkFont(weight="bold")).grid(
            row=0, column=3, padx=4
        )

        if joystick is None:
            ctk.CTkLabel(map_scroll, text="Select a joystick to edit mappings.").pack(anchor="w", padx=4, pady=8)
            return

        axis_hints = ("roll", "pitch", "yaw", "throttle")

        def add_row(key: str, title: str) -> None:
            rowf = ctk.CTkFrame(map_scroll, fg_color="transparent")
            rowf.pack(fill="x", pady=1)
            ctk.CTkLabel(rowf, text=title, width=200, anchor="w").grid(row=0, column=0, padx=4, sticky="w")
            d, k = _mapping_row_target(key)
            cur = d.get(k, {}).get("channel") if k in d else None
            ch_var = ctk.StringVar(value=_channel_menu_value(cur))
            om = ctk.CTkOptionMenu(
                rowf,
                variable=ch_var,
                values=list(_ch_labels),
                width=118,
                command=lambda v, kk=key: _set_mapping_channel(kk, v),
            )
            om.grid(row=0, column=1, padx=4)
            inv0 = bool(d.get(k, {}).get("invert", False)) if k in d else False
            inv_var = ctk.BooleanVar(value=inv0)

            def _inv_cmd(kk=key, iv=inv_var):
                _set_mapping_invert(kk, iv.get())

            inv_cb = ctk.CTkCheckBox(rowf, text="", width=36, variable=inv_var, command=_inv_cmd)
            inv_cb.grid(row=0, column=2, padx=4)
            live = ctk.CTkLabel(rowf, text="—", width=100, anchor="w", font=font_val)
            live.grid(row=0, column=3, padx=4, sticky="w")
            mapping_live_labels[key] = live

        for i in range(joystick.get_numaxes()):
            hint = axis_hints[i] if i < len(axis_hints) else f"axis {i}"
            add_row(f"axis_{i}", f"Axis {i} ({hint})")
        for i in range(joystick.get_numbuttons()):
            add_row(f"button_{i}", f"Button {i}")
        for i in range(joystick.get_numhats()):
            add_row(f"hat_{i}_x", f"Hat {i} X")
            add_row(f"hat_{i}_y", f"Hat {i} Y")

    def apply_joy_choice(choice: Optional[str] = None):
        nonlocal joystick, joy_index
        choice = choice if choice is not None else joy_var.get()
        if choice == "(no joystick)":
            joystick = None
            joy_index = None
            rebuild_mapping_ui()
            return
        try:
            idx = int(choice.split(":", 1)[0].strip())
        except (ValueError, IndexError):
            joystick = None
            joy_index = None
            rebuild_mapping_ui()
            return
        if joy_index == idx and joystick is not None:
            rebuild_mapping_ui()
            return
        prev_j = joystick
        joy_index = idx
        joystick = open_joystick(joy_index, prev_j)
        prune_joystick_mappings(joystick, axis_map, button_map, hat_map)
        n_pre = apply_rc_preset_mappings(joystick, axis_map, button_map, hat_map)
        n_auto = merge_default_joystick_mappings(joystick, axis_map, button_map, hat_map)
        if n_pre or n_auto:
            _LOG.info("After device change: preset +%d, auto +%d mappings.", n_pre, n_auto)
        rebuild_mapping_ui()

    joy_menu = ctk.CTkOptionMenu(
        joy_row,
        variable=joy_var,
        values=menu_vals,
        command=lambda v: apply_joy_choice(v),
        width=400,
    )
    joy_menu.grid(row=0, column=0, padx=(0, 8), sticky="ew")

    def refresh_joysticks():
        nonlocal joystick
        if joystick is not None:
            try:
                joystick.quit()
            except Exception:
                pass
            joystick = None
        try:
            pygame.joystick.quit()
        except Exception:
            pass
        pygame.joystick.init()
        vals = _joy_menu_values()
        joy_menu.configure(values=vals)
        cur = joy_var.get()
        if cur not in vals:
            if vals:
                joy_var.set(vals[0])
            else:
                joy_var.set("(no joystick)")
        apply_joy_choice()

    rescan_btn = ctk.CTkButton(joy_row, text="Rescan", width=90, command=refresh_joysticks)
    rescan_btn.grid(row=0, column=1, sticky="e")

    apply_joy_choice()

    send_var = ctk.StringVar(value="Start sending")

    def toggle_send():
        nonlocal sending, status_text, last_err, acc
        if not sending and not bridge_connected:
            last_err = "Connect first (TCP handshake)"
            status_text = "Error"
            status_lbl.configure(text=_status_display())
            return
        if not sending:
            _ip, ip_err = _validate_ipv4_text(ip_entry.get())
            if _ip is None:
                last_err = ip_err
                status_text = "Error"
                status_lbl.configure(text=_status_display())
                return
        sending = not sending
        if sending:
            send_var.set("Stop sending")
            status_text = "Sending"
            last_err = ""
            acc = 0.0
        else:
            send_var.set("Start sending")
            status_text = "Stopped"
        status_lbl.configure(text=_status_display())

    send_btn = ctk.CTkButton(root, textvariable=send_var, command=toggle_send, height=36, fg_color="gray40", hover_color="gray35")
    send_btn.grid(row=3, column=0, padx=16, pady=8, sticky="w")

    def on_connect():
        nonlocal bridge_connected, handshake_busy, sending, status_text, last_err, acc

        if handshake_busy:
            return
        if bridge_connected:
            bridge_connected = False
            if sending:
                sending = False
                send_var.set("Start sending")
                status_text = "Stopped"
                acc = 0.0
            connect_var.set("Connect")
            last_err = ""
            connect_btn.configure(
                state="normal",
                fg_color=("gray75", "gray25"),
                hover_color=("gray65", "gray35"),
            )
            status_lbl.configure(text=_status_display())
            return

        host, ip_err = _validate_ipv4_text(ip_entry.get())
        if host is None:
            last_err = ip_err
            status_text = "Error"
            status_lbl.configure(text=_status_display())
            return

        def worker():
            ok, err = tcp_handshake(host, args.handshake_port)

            def apply_result():
                nonlocal bridge_connected, handshake_busy, status_text, last_err, sending, acc
                handshake_busy = False
                connect_btn.configure(state="normal")
                if ok:
                    bridge_connected = True
                    connect_var.set("Disconnect")
                    last_err = ""
                    connect_btn.configure(
                        fg_color="seagreen",
                        hover_color="darkgreen",
                    )
                    # Start UDP immediately so the bridge receives frames (and --debug shows them)
                    # without requiring a separate "Start sending" click after Connect.
                    if sock is None:
                        status_text = "Connected"
                        last_err = "UDP socket unavailable"
                        sending = False
                    else:
                        sending = True
                        send_var.set("Stop sending")
                        acc = 0.0
                        status_text = "Sending"
                        try:
                            pwm0 = get_pwm_channels_from_joystick(
                                joystick, axis_map, button_map, hat_map
                            )
                            pkt0 = pack_channel_datagram(pwm0)
                            sock.sendto(pkt0, (host, args.target_port))
                            _LOG.info(
                                "UDP stream started to %s:%s (%d bytes/frame)",
                                host,
                                args.target_port,
                                len(pkt0),
                            )
                        except OSError as e:
                            _LOG.warning("UDP send right after connect failed: %s", e)
                            sending = False
                            send_var.set("Start sending")
                            status_text = "UDP send error"
                            last_err = str(e)
                else:
                    bridge_connected = False
                    last_err = err or "Handshake failed"
                    status_text = "Connect failed"
                    connect_btn.configure(
                        fg_color=("gray75", "gray25"),
                        hover_color=("gray65", "gray35"),
                    )
                status_lbl.configure(text=_status_display())

            root.after(0, apply_result)

        handshake_busy = True
        status_text = "Connecting…"
        last_err = ""
        connect_btn.configure(state="disabled")
        status_lbl.configure(text=_status_display())
        threading.Thread(target=worker, daemon=True).start()

    connect_btn.configure(command=on_connect, fg_color=("gray75", "gray25"), hover_color=("gray65", "gray35"))

    def _status_display() -> str:
        s = f"Status: {status_text}"
        if last_err:
            s += f" — {last_err}"
        return s

    hint = ctk.CTkLabel(
        root,
        text="Connect · Map table assigns axes/buttons/hats to CH1–16 (preset CH1–4 sticks, CH5–8 first buttons) · --hz",
        font=ctk.CTkFont(size=12),
        text_color="gray60",
    )
    hint.grid(row=4, column=0, padx=16, pady=(0, 4), sticky="w")

    status_lbl = ctk.CTkLabel(root, text=_status_display(), font=ctk.CTkFont(size=13), anchor="w")
    status_lbl.grid(row=5, column=0, padx=16, pady=(4, 16), sticky="ew")

    running = True
    last_raw_print_mono = 0.0

    def tick():
        nonlocal last_tick, acc, status_text, last_err, sending, last_raw_print_mono
        if not running:
            return
        now = time.monotonic()
        dt = now - last_tick
        last_tick = now

        pwm = get_pwm_channels_from_joystick(joystick, axis_map, button_map, hat_map)
        if joystick is not None:
            for key, lbl in mapping_live_labels.items():
                try:
                    if key.startswith("axis_"):
                        idx = int(key.split("_", 1)[1])
                        v = joystick.get_axis(idx)
                        lbl.configure(text=f"{v:+.4f}")
                    elif key.startswith("button_"):
                        idx = int(key.split("_", 1)[1])
                        b = joystick.get_button(idx)
                        lbl.configure(text="on" if b else "off")
                    elif key.startswith("hat_"):
                        parts = key.split("_")
                        hi = int(parts[1])
                        hx, hy = joystick.get_hat(hi)
                        lbl.configure(text=str(hx) if parts[2] == "x" else str(hy))
                except (ValueError, IndexError, pygame.error):
                    lbl.configure(text="—")
        if args.print_raw and joystick is not None:
            nowm = time.monotonic()
            interval = 1.0 / max(args.print_raw_hz, 0.25)
            if nowm - last_raw_print_mono >= interval:
                last_raw_print_mono = nowm
                axes = [joystick.get_axis(i) for i in range(joystick.get_numaxes())]
                btns = [joystick.get_button(i) for i in range(joystick.get_numbuttons())]
                hats = [joystick.get_hat(i) for i in range(joystick.get_numhats())]
                print(f"raw joystick axes={axes} buttons={btns} hats={hats}", flush=True)
        for i in range(16):
            v = pwm[i]
            channel_bars[i].set((v - 1000) / 1000.0)
            channel_value_labels[i].configure(text=str(v))

        if sending and sock is not None:
            acc += dt
            if acc >= send_interval:
                acc = 0.0
                udp_port = args.target_port
                host, ip_err = _validate_ipv4_text(ip_entry.get())
                if host is None:
                    last_err = ip_err
                    status_text = "Error"
                    sending = False
                    send_var.set("Start sending")
                    status_lbl.configure(text=_status_display())
                    root.after(16, tick)
                    return
                try:
                    pkt = pack_channel_datagram(pwm)
                    sock.sendto(pkt, (host, udp_port))
                    _LOG.debug("UDP send %s:%s len=%d", host, udp_port, len(pkt))
                    last_err = ""
                    if status_text != "Sending":
                        status_text = "Sending"
                except OSError as e:
                    last_err = str(e)
                    status_text = "Send error"

        status_lbl.configure(text=_status_display())
        if sending:
            send_btn.configure(fg_color="seagreen", hover_color="darkgreen")
        else:
            send_btn.configure(fg_color="gray40", hover_color="gray35")
        root.after(16, tick)

    def on_closing():
        nonlocal running
        running = False
        if sock is not None:
            sock.close()
        if joystick is not None:
            joystick.quit()
        pygame.quit()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_closing)

    tick()

    root.mainloop()


if __name__ == "__main__":
    main()
    sys.exit(0)
