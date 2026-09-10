"""Set gpiozero pin factory before any gpiozero import (Raspberry Pi / Bookworm)."""

from __future__ import annotations

import os

try:
    from .sbc import BOARD_LUCKFOX, detect_board
except ImportError:
    from sbc import BOARD_LUCKFOX, detect_board  # type: ignore

# lgpio is the preferred backend on Raspberry Pi OS Bookworm+. Skip on
# Luckfox / OpenIPC (sysfs GPIO; gpiozero/lgpio are not used there).
if detect_board() != BOARD_LUCKFOX:
    os.environ.setdefault("GPIOZERO_PIN_FACTORY", "lgpio")
