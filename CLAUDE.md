# CLAUDE.md

On the PC run the ground station as a script (`python src/ground_station/main.py`), not with `PYTHONPATH=src` set globally.

The SBC package is `drone_control` (Raspberry Pi or Luckfox / OpenIPC). The TX bridge and standalone `box_server` both bind port 50502 — don't run both simultaneously.
