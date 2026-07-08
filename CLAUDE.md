# CLAUDE.md

`src/operator/` shadows Python's stdlib `operator` module — on the PC run the client as a script (`python src/operator/main.py`), not with `PYTHONPATH=src` set globally.

Pi bridge and standalone `box_server` both bind port 50502 — don't run both simultaneously on the Pi.
