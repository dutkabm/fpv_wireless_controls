#!/usr/bin/env python3
"""
PC / laptop: read a local joystick (controller_map.txt + on-screen map table), show 16 channel meters,
TCP-handshake with the Pi bridge on Connect, then UDP-send 16-channel frames at the configured rate.
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import threading
import time
from typing import Optional

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
)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from modules.joystick import (
    apply_rc_preset_mappings,
    get_pwm_channels_from_joystick,
    joy_menu_values,
    load_controller_config,
    merge_default_joystick_mappings,
    open_joystick,
    prune_joystick_mappings,
)
from modules.network import (
    scan_tx_bridges,
    send_pwm_datagram,
    tcp_handshake,
    try_open_udp_socket,
    validate_ipv4_netmask,
    validate_ipv4_text,
)
from modules.ui import JoystickRef, NetworkJoystickUI

_LOG = logging.getLogger(__name__)


def main():
    default_config = os.path.join(SCRIPT_DIR, "controller_map.txt")

    ap = argparse.ArgumentParser(description="Joystick → UDP client for Mini Rex network bridge")
    ap.add_argument("--config", default=default_config, help="Mapping INI path (Mini Rex joystick map)")
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
    ap.add_argument(
        "--netmask",
        default="255.255.255.0",
        help="Subnet mask or CIDR for LAN bridge scan (255.255.255.0 or /24); used with local IPv4",
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

    sock, udp_open_err = try_open_udp_socket()
    if sock is None:
        last_err = udp_open_err or "UDP socket failed"
        status_text = "Socket error"
    else:
        last_err = ""
        status_text = "Idle"

    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")

    root = ctk.CTk()
    root.title("Network FPV Controller")
    root.geometry("1320x660")
    root.minsize(1000, 560)
    root.grid_columnconfigure(0, weight=1)

    joy_ref = JoystickRef(joystick, joy_index)
    view = NetworkJoystickUI(
        root,
        args=args,
        joy_ref=joy_ref,
        axis_map=axis_map,
        button_map=button_map,
        hat_map=hat_map,
        open_joystick_fn=open_joystick,
        prune_fn=prune_joystick_mappings,
        preset_fn=apply_rc_preset_mappings,
        merge_fn=merge_default_joystick_mappings,
        joy_menu_values_fn=joy_menu_values,
        logger=_LOG,
    )
    view.apply_joy_choice()

    sending = False
    bridge_connected = False
    handshake_busy = False
    scan_busy = False

    def _status_display() -> str:
        s = f"Status: {status_text}"
        if last_err:
            s += f" — {last_err}"
        return s

    view.status_lbl.configure(text=_status_display())

    def toggle_send():
        nonlocal sending, status_text, last_err, acc
        if not sending and not bridge_connected:
            last_err = "Connect first (TCP handshake)"
            status_text = "Error"
            view.status_lbl.configure(text=_status_display())
            return
        if not sending:
            _ip, ip_err = validate_ipv4_text(view.ip_entry.get())
            if _ip is None:
                last_err = ip_err
                status_text = "Error"
                view.status_lbl.configure(text=_status_display())
                return
        sending = not sending
        if sending:
            view.send_var.set("Stop sending")
            status_text = "Sending"
            last_err = ""
            acc = 0.0
        else:
            view.send_var.set("Start sending")
            status_text = "Stopped"
        view.status_lbl.configure(text=_status_display())

    view.send_btn.configure(command=toggle_send)

    def on_connect():
        nonlocal bridge_connected, handshake_busy, sending, status_text, last_err, acc

        if handshake_busy:
            return
        if bridge_connected:
            bridge_connected = False
            if sending:
                sending = False
                view.send_var.set("Start sending")
                status_text = "Stopped"
                acc = 0.0
            view.connect_var.set("Connect")
            last_err = ""
            view.connect_btn.configure(
                state="normal",
                fg_color=("gray75", "gray25"),
                hover_color=("gray65", "gray35"),
            )
            view.status_lbl.configure(text=_status_display())
            return

        if scan_busy:
            last_err = "Wait for bridge scan to finish"
            status_text = "Error"
            view.status_lbl.configure(text=_status_display())
            return

        host, ip_err = validate_ipv4_text(view.ip_entry.get())
        if host is None:
            last_err = ip_err
            status_text = "Error"
            view.status_lbl.configure(text=_status_display())
            return

        def worker():
            ok, err, bridge_name = tcp_handshake(host, args.handshake_port)

            def apply_result():
                nonlocal bridge_connected, handshake_busy, status_text, last_err, sending, acc
                handshake_busy = False
                view.connect_btn.configure(state="normal")
                if ok:
                    bridge_connected = True
                    view.connect_var.set("Disconnect")
                    last_err = ""
                    view.connect_btn.configure(
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
                        view.send_var.set("Stop sending")
                        acc = 0.0
                        status_text = (
                            f"Sending — {bridge_name}" if bridge_name.strip() else "Sending"
                        )
                        try:
                            pwm0 = get_pwm_channels_from_joystick(
                                joy_ref.joystick, axis_map, button_map, hat_map
                            )
                            n0 = send_pwm_datagram(sock, host, args.target_port, pwm0)
                            _LOG.info(
                                "UDP stream started to %s:%s (%d bytes/frame)",
                                host,
                                args.target_port,
                                n0,
                            )
                        except OSError as e:
                            _LOG.warning("UDP send right after connect failed: %s", e)
                            sending = False
                            view.send_var.set("Start sending")
                            status_text = "UDP send error"
                            last_err = str(e)
                else:
                    bridge_connected = False
                    last_err = err or "Handshake failed"
                    status_text = "Connect failed"
                    view.connect_btn.configure(
                        fg_color=("gray75", "gray25"),
                        hover_color=("gray65", "gray35"),
                    )
                view.status_lbl.configure(text=_status_display())

            root.after(0, apply_result)

        handshake_busy = True
        status_text = "Connecting…"
        last_err = ""
        view.connect_btn.configure(state="disabled")
        view.status_lbl.configure(text=_status_display())
        threading.Thread(target=worker, daemon=True).start()

    view.connect_btn.configure(command=on_connect, fg_color=("gray75", "gray25"), hover_color=("gray65", "gray35"))

    def on_scan():
        nonlocal scan_busy, status_text, last_err

        if scan_busy or handshake_busy:
            return
        nm_raw = view.netmask_entry.get()
        _nm_ok, nm_err = validate_ipv4_netmask(nm_raw)
        if nm_err:
            last_err = nm_err
            status_text = "Error"
            view.reset_scan_menu_to_placeholder()
            view.status_lbl.configure(text=_status_display())
            return

        def worker():
            rows, serr = scan_tx_bridges(args.handshake_port, nm_raw.strip())

            def apply_scan():
                nonlocal scan_busy, status_text, last_err
                scan_busy = False
                view.scan_btn.configure(state="normal")
                if serr:
                    last_err = serr
                    status_text = "Scan failed"
                    view.reset_scan_menu_to_placeholder()
                else:
                    last_err = ""
                    view.set_scan_results(rows)
                    status_text = f"Scan done ({len(rows)} bridge(s))"
                    if rows:
                        _LOG.info("Bridge scan: %s", rows)
                view.status_lbl.configure(text=_status_display())

            root.after(0, apply_scan)

        scan_busy = True
        view.scan_btn.configure(state="disabled")
        status_text = "Scanning LAN…"
        last_err = ""
        view.status_lbl.configure(text=_status_display())
        threading.Thread(target=worker, daemon=True).start()

    view.scan_btn.configure(command=on_scan)

    running = True
    last_raw_print_mono = 0.0

    def tick():
        nonlocal last_tick, acc, status_text, last_err, sending, last_raw_print_mono
        if not running:
            return
        now = time.monotonic()
        dt = now - last_tick
        last_tick = now

        pwm = get_pwm_channels_from_joystick(joy_ref.joystick, axis_map, button_map, hat_map)
        j = joy_ref.joystick
        if j is not None:
            for key, lbl in view.mapping_live_labels.items():
                try:
                    if key.startswith("axis_"):
                        idx = int(key.split("_", 1)[1])
                        v = j.get_axis(idx)
                        lbl.configure(text=f"{v:+.4f}")
                    elif key.startswith("button_"):
                        idx = int(key.split("_", 1)[1])
                        b = j.get_button(idx)
                        lbl.configure(text="on" if b else "off")
                    elif key.startswith("hat_"):
                        parts = key.split("_")
                        hi = int(parts[1])
                        hx, hy = j.get_hat(hi)
                        lbl.configure(text=str(hx) if parts[2] == "x" else str(hy))
                except (ValueError, IndexError, pygame.error):
                    lbl.configure(text="—")
        if args.print_raw and j is not None:
            nowm = time.monotonic()
            interval = 1.0 / max(args.print_raw_hz, 0.25)
            if nowm - last_raw_print_mono >= interval:
                last_raw_print_mono = nowm
                axes = [j.get_axis(i) for i in range(j.get_numaxes())]
                btns = [j.get_button(i) for i in range(j.get_numbuttons())]
                hats = [j.get_hat(i) for i in range(j.get_numhats())]
                print(f"raw joystick axes={axes} buttons={btns} hats={hats}", flush=True)
        for i in range(16):
            v = pwm[i]
            view.channel_bars[i].set((v - 1000) / 1000.0)
            view.channel_value_labels[i].configure(text=str(v))

        if sending and sock is not None:
            acc += dt
            if acc >= send_interval:
                acc = 0.0
                udp_port = args.target_port
                host, ip_err = validate_ipv4_text(view.ip_entry.get())
                if host is None:
                    last_err = ip_err
                    status_text = "Error"
                    sending = False
                    view.send_var.set("Start sending")
                    view.status_lbl.configure(text=_status_display())
                    root.after(16, tick)
                    return
                try:
                    n = send_pwm_datagram(sock, host, udp_port, pwm)
                    _LOG.debug("UDP send %s:%s len=%d", host, udp_port, n)
                    last_err = ""
                    if status_text != "Sending":
                        status_text = "Sending"
                except OSError as e:
                    last_err = str(e)
                    status_text = "Send error"

        view.status_lbl.configure(text=_status_display())
        if sending:
            view.send_btn.configure(fg_color="seagreen", hover_color="darkgreen")
        else:
            view.send_btn.configure(fg_color="gray40", hover_color="gray35")
        root.after(16, tick)

    def on_closing():
        nonlocal running
        running = False
        if sock is not None:
            sock.close()
        if joy_ref.joystick is not None:
            joy_ref.joystick.quit()
        pygame.quit()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_closing)

    tick()

    root.mainloop()


if __name__ == "__main__":
    main()
    sys.exit(0)
