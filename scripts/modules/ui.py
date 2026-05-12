"""CustomTkinter UI for ``network_joystick_client`` (layout, channel meters, mapping table)."""

from __future__ import annotations

import logging
import os
import sys
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

import customtkinter as ctk

if TYPE_CHECKING:
    import pygame


class JoystickRef:
    """Mutable joystick selection (shared with main tick / networking)."""

    def __init__(self, joystick: Optional["pygame.joystick.Joystick"], joy_index: Optional[int]) -> None:
        self.joystick = joystick
        self.joy_index = joy_index


class NetworkJoystickUI:
    """Builds the main window layout and mapping table; keeps joystick menu + map UI in sync."""

    def __init__(
        self,
        root: ctk.CTk,
        *,
        args: Any,
        joy_ref: JoystickRef,
        axis_map: Dict,
        button_map: Dict,
        hat_map: Dict,
        open_joystick_fn: Callable[[Optional[int], Optional["pygame.joystick.Joystick"]], Optional["pygame.joystick.Joystick"]],
        prune_fn: Callable[..., None],
        preset_fn: Callable[..., int],
        merge_fn: Callable[..., int],
        joy_menu_values_fn: Callable[[], List[str]],
        logger: logging.Logger,
    ) -> None:
        self.root = root
        self.args = args
        self.joy_ref = joy_ref
        self.axis_map = axis_map
        self.button_map = button_map
        self.hat_map = hat_map
        self._open_joystick = open_joystick_fn
        self._prune = prune_fn
        self._preset = preset_fn
        self._merge = merge_fn
        self._joy_menu_values = joy_menu_values_fn
        self._log = logger

        self.mapping_live_labels: Dict[str, ctk.CTkLabel] = {}

        self._ch_labels = (
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

        _mono = "Menlo" if sys.platform == "darwin" else "Consolas" if os.name == "nt" else "DejaVu Sans Mono"
        self._font_ch = ctk.CTkFont(family=_mono, size=11, weight="bold")
        self._font_val = ctk.CTkFont(family=_mono, size=11)

        self._build()

    def _channel_menu_value(self, ch: Optional[int]) -> str:
        if ch is None or ch < 1 or ch > 16:
            return "Off"
        return self._ch_labels[ch]

    def _parse_channel_menu(self, val: str) -> Optional[int]:
        if val == "Off":
            return None
        try:
            return int(val.split()[0])
        except (ValueError, IndexError):
            return None

    def _mapping_row_target(self, key: str) -> Tuple[Dict, str]:
        if key.startswith("axis_"):
            return self.axis_map, key
        if key.startswith("button_"):
            return self.button_map, key
        if key.endswith("_x") and key.startswith("hat_"):
            return self.hat_map["x"], key
        if key.endswith("_y") and key.startswith("hat_"):
            return self.hat_map["y"], key
        return self.axis_map, key

    def _set_mapping_channel(self, key: str, menu_val: str) -> None:
        d, k = self._mapping_row_target(key)
        ch = self._parse_channel_menu(menu_val)
        if ch is None:
            d.pop(k, None)
        else:
            inv = d.get(k, {}).get("invert", False) if k in d else False
            d[k] = {"channel": ch, "invert": inv}

    def _set_mapping_invert(self, key: str, inv: bool) -> None:
        d, k = self._mapping_row_target(key)
        if k not in d:
            return
        d[k]["invert"] = bool(inv)

    def rebuild_mapping_ui(self) -> None:
        self.mapping_live_labels.clear()
        for w in self.map_scroll.winfo_children():
            w.destroy()
        hdr = ctk.CTkFrame(self.map_scroll, fg_color="transparent")
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

        j = self.joy_ref.joystick
        if j is None:
            ctk.CTkLabel(self.map_scroll, text="Select a joystick to edit mappings.").pack(anchor="w", padx=4, pady=8)
            return

        axis_hints = ("roll", "pitch", "yaw", "throttle")

        def add_row(key: str, title: str) -> None:
            rowf = ctk.CTkFrame(self.map_scroll, fg_color="transparent")
            rowf.pack(fill="x", pady=1)
            ctk.CTkLabel(rowf, text=title, width=200, anchor="w").grid(row=0, column=0, padx=4, sticky="w")
            d, k = self._mapping_row_target(key)
            cur = d.get(k, {}).get("channel") if k in d else None
            ch_var = ctk.StringVar(value=self._channel_menu_value(cur))
            ctk.CTkOptionMenu(
                rowf,
                variable=ch_var,
                values=list(self._ch_labels),
                width=118,
                command=lambda v, kk=key: self._set_mapping_channel(kk, v),
            ).grid(row=0, column=1, padx=4)
            inv0 = bool(d.get(k, {}).get("invert", False)) if k in d else False
            inv_var = ctk.BooleanVar(value=inv0)

            def _inv_cmd(kk: str = key, iv: ctk.BooleanVar = inv_var) -> None:
                self._set_mapping_invert(kk, iv.get())

            ctk.CTkCheckBox(rowf, text="", width=36, variable=inv_var, command=_inv_cmd).grid(row=0, column=2, padx=4)
            live = ctk.CTkLabel(rowf, text="—", width=100, anchor="w", font=self._font_val)
            live.grid(row=0, column=3, padx=4, sticky="w")
            self.mapping_live_labels[key] = live

        for i in range(j.get_numaxes()):
            hint = axis_hints[i] if i < len(axis_hints) else f"axis {i}"
            add_row(f"axis_{i}", f"Axis {i} ({hint})")
        for i in range(j.get_numbuttons()):
            add_row(f"button_{i}", f"Button {i}")
        for i in range(j.get_numhats()):
            add_row(f"hat_{i}_x", f"Hat {i} X")
            add_row(f"hat_{i}_y", f"Hat {i} Y")

    def apply_joy_choice(self, choice: Optional[str] = None) -> None:
        choice = choice if choice is not None else self.joy_var.get()
        if choice == "(no joystick)":
            self.joy_ref.joystick = None
            self.joy_ref.joy_index = None
            self.rebuild_mapping_ui()
            return
        try:
            idx = int(choice.split(":", 1)[0].strip())
        except (ValueError, IndexError):
            self.joy_ref.joystick = None
            self.joy_ref.joy_index = None
            self.rebuild_mapping_ui()
            return
        if self.joy_ref.joy_index == idx and self.joy_ref.joystick is not None:
            self.rebuild_mapping_ui()
            return
        prev_j = self.joy_ref.joystick
        self.joy_ref.joy_index = idx
        self.joy_ref.joystick = self._open_joystick(self.joy_ref.joy_index, prev_j)
        self._prune(self.joy_ref.joystick, self.axis_map, self.button_map, self.hat_map)
        n_pre = self._preset(self.joy_ref.joystick, self.axis_map, self.button_map, self.hat_map)
        n_auto = self._merge(self.joy_ref.joystick, self.axis_map, self.button_map, self.hat_map)
        if n_pre or n_auto:
            self._log.info("After device change: preset +%d, auto +%d mappings.", n_pre, n_auto)
        self.rebuild_mapping_ui()

    def refresh_joysticks(self) -> None:
        import pygame

        if self.joy_ref.joystick is not None:
            try:
                self.joy_ref.joystick.quit()
            except Exception:
                pass
            self.joy_ref.joystick = None
        try:
            pygame.joystick.quit()
        except Exception:
            pass
        pygame.joystick.init()
        vals = self._joy_menu_values()
        self.joy_menu.configure(values=vals)
        cur = self.joy_var.get()
        if cur not in vals:
            if vals:
                self.joy_var.set(vals[0])
            else:
                self.joy_var.set("(no joystick)")
        self.apply_joy_choice()

    def _build(self) -> None:
        root = self.root
        args = self.args
        joy_ref = self.joy_ref

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
        self.ip_entry = ctk.CTkEntry(form, placeholder_text="IPv4 e.g. 192.168.1.10", width=280)
        self.ip_entry.grid(row=0, column=1, padx=0, pady=4, sticky="ew")
        self.ip_entry.insert(0, args.target_ip.strip())

        self.connect_var = ctk.StringVar(value="Connect")
        self.connect_btn = ctk.CTkButton(form, textvariable=self.connect_var, width=120)
        self.connect_btn.grid(row=0, column=2, padx=(12, 0), pady=4, sticky="e")

        ctk.CTkLabel(form, text="Joystick").grid(row=1, column=0, padx=(0, 8), pady=4, sticky="nw")

        joy_row = ctk.CTkFrame(form, fg_color="transparent")
        joy_row.grid(row=1, column=1, columnspan=2, padx=0, pady=4, sticky="ew")
        joy_row.grid_columnconfigure(0, weight=1)

        self.joy_var = ctk.StringVar(value="(no joystick)")
        menu_vals = self._joy_menu_values()
        if joy_ref.joystick is not None and joy_ref.joy_index is not None:
            cand = f"{joy_ref.joy_index}: {joy_ref.joystick.get_name()}"
            if cand in menu_vals:
                self.joy_var.set(cand)
            elif menu_vals:
                self.joy_var.set(menu_vals[0])

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

        self.map_scroll = ctk.CTkScrollableFrame(
            content,
            label_text="Joystick Channels (CH1–4 sticks, CH5–8 buttons preset; assign extra axes as switches)",
            width=460,
            height=360,
        )
        self.map_scroll.grid(row=0, column=1, sticky="nsew", padx=(12, 0))

        _ref_lbl = getattr(self.map_scroll, "_label", None)
        if _ref_lbl is not None:
            _sp = int(self.map_scroll.cget("corner_radius") or 0) + int(self.map_scroll.cget("border_width") or 0)
            elrs_title = ctk.CTkLabel(
                channels_row_frame,
                text="ELRS Channels",
                font=_ref_lbl.cget("font"),
                text_color=_ref_lbl.cget("text_color"),
                fg_color=_ref_lbl.cget("fg_color"),
                corner_radius=_ref_lbl.cget("corner_radius"),
                anchor="w",
            )
            elrs_title.grid(row=0, column=0, sticky="ew", padx=_sp, pady=(_sp, 2))
        else:
            elrs_title = ctk.CTkLabel(
                channels_row_frame,
                text="ELRS Channels",
                font=ctk.CTkFont(size=13, weight="bold"),
                anchor="w",
            )
            elrs_title.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 2))

        channels_inner = ctk.CTkFrame(channels_row_frame, fg_color="transparent")
        channels_inner.grid(row=1, column=0, padx=4, pady=(0, 6))

        self.channel_bars = []
        self.channel_value_labels = []
        cell_bg = ("gray90", "gray22")
        cell_border = ("gray65", "gray38")
        bar_w, bar_h = 20, 88
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
                font=self._font_ch,
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
            self.channel_bars.append(bar)
            vl = ctk.CTkLabel(
                cell,
                text="1500",
                font=self._font_val,
                text_color=("gray30", "gray75"),
                width=40,
                anchor="center",
            )
            vl.pack(pady=(0, 5))
            self.channel_value_labels.append(vl)

        self.joy_menu = ctk.CTkOptionMenu(
            joy_row,
            variable=self.joy_var,
            values=menu_vals,
            command=lambda v: self.apply_joy_choice(v),
            width=400,
        )
        self.joy_menu.grid(row=0, column=0, padx=(0, 8), sticky="ew")

        rescan_btn = ctk.CTkButton(joy_row, text="Rescan", width=90, command=self.refresh_joysticks)
        rescan_btn.grid(row=0, column=1, sticky="e")

        self.send_var = ctk.StringVar(value="Start sending")
        self.send_btn = ctk.CTkButton(
            root,
            textvariable=self.send_var,
            height=36,
            fg_color="gray40",
            hover_color="gray35",
        )
        self.send_btn.grid(row=3, column=0, padx=16, pady=8, sticky="w")

        self.hint = ctk.CTkLabel(
            root,
            text="Connect · Map table assigns axes/buttons/hats to CH1–16 (preset CH1–4 sticks, CH5–8 first buttons) · --hz",
            font=ctk.CTkFont(size=12),
            text_color="gray60",
        )
        self.hint.grid(row=4, column=0, padx=16, pady=(0, 4), sticky="w")

        self.status_lbl = ctk.CTkLabel(root, text="", font=ctk.CTkFont(size=13), anchor="w")
        self.status_lbl.grid(row=5, column=0, padx=16, pady=(4, 16), sticky="ew")
