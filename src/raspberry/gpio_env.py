"""Set gpiozero pin factory before any gpiozero import (Pi / Bookworm)."""

from __future__ import annotations

import os

# lgpio is the preferred backend on Raspberry Pi OS Bookworm+.
os.environ.setdefault("GPIOZERO_PIN_FACTORY", "lgpio")
