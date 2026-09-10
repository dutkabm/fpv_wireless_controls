# FPV Wireless Controls

Wireless ELRS/CRSF control: gamepad host, Pi TX bridge, and Pico RX output.

# Overview

This project is a hardware/software setup for flexible, long-range, low-latency controller transmission.
This project is built off the [ExpressLRS](https://www.expresslrs.org/) (ELRS) and [Crossfire](https://www.team-blacksheep.com/media/files/tbs-crossfire-manual.pdf) (CRSF) Wireless control protocols, typically used to control FPV drones.
ELRS & CRSF have widespread adoption in the FPV drone community with an abundance of support, documentation, and budget-friendly hardware availability.
This project is yet another passion project with the goal to utilize and expand the use of these robust control systems.

## Flow

The primary goal is an easily implementable way to wirelessly transmit any input to any output device. The stack is split into two parts, joined by ELRS/CRSF wireless transmission:

- **Input Side (TX)**:
  - Collects input from various devices like joysticks, game controllers, or sensors.
  - Converts this input into CRSF packets for transmission using ELRS.
- **Output Side (RX)**:
  - Receives CRSF packets wirelessly using an ELRS-compatible receiver.
  - Decodes the packets and maps the signals to control servos, motors, or other devices.

## Use Cases

- Standardized control interface for robotics projects. Use this rather than having to rely on WiFi, Bluetooth, IR, Gestures, AI, & Carrier Pigeons to make a robot move.
- Adaptable control inputs for an FPV drone. Another way to control drones rather than the 'traditional' transmitter radio.
- Hybrid control schemes. Utilize both user input and other local sensor data to control a device.
- Fully-autonomous controls. Program your favorite 'AI' to take over a robot.
- Ultra-long-range gamepads. Use your XBox controller to play video games from miles away.

# Implementation

The stack uses Python on a gamepad host and on a Raspberry Pi or Luckfox Pico Pro/Max (OpenIPC) near the transmitter. The **network path** (current `src/` tree) sends RC channels from the PC over UDP to the board, which forwards CRSF to the TX over USB serial or SoC UART. The transmitter wirelessly sends data to the Drone Control Receiver (RX), which outputs CRSF to a Raspberry Pi Pico.
The Pico runs a C++ program which interprets the data, outputting either an emulated Joystick signals to the final output device.

## Requirements

### Hardware

Following the flowchart above, this is a full list of hardware. 

**Note**: This project modular! Want to just control your drone programmatically? Drop the Raspberry Pi Pico and it should all work. Want to control a device with your drone controller? Drop the input processor. 

- **Input Device**
  - This can be an XBox controller, Program, or some other way of taking action.
  - This is required to display as if it were a Gamepad/Joystick/HID Device to the computer running the Python program.
- **Gamepad Input Processor**, aka a computer that can run Python 3.
  - This is required to have a USB or serial port available to communicate with the drone TX.
  - Main project development was done on a laptop running Windows 10, but also tested on a Raspberry Pi 4b.
- **Drone Control Transmitter Module (TX)**.
  - This can be any CRSF serial protocol based device (ELRS, TBS Crossfire, TBS Tracer).
  - Some ELRS TX are able to easily be connected via a USB cable.
  - Other devices may require a FTDI adapter and be connected via the S.PORT pin on the transmitter (similar to as if plugged into a standard drone transmitter)
  - Main project development was done using a Radiomaster Ranger Micro TX using both connection methods.
    - Untested on other devices, but likely to work.
- **Drone Control Receiver Chip (RX)**.
  - This can be basically any CRSF serial protocol based device compatible with your TX (ELRS EP1/EP2, TBS Crossfire Diversity RX, TBS Sixy9, etc.)
  - Main project development was done using a Radiomaster RP1 RX.
    - Untested on other devices, but extremely likely to work.
- **Drone Signal Interpreter**.
  - This is *tentatively* required to be a Raspberry Pi Pico (RP2040) device.
    - The rest of documentation assumes this device.
  - Other Arduino/C++ microcontrollers may be compatible, but not guaranteed.
- **Robot** or **Other End Device**.
  - Any object you want to control that can take either a simulated HID Gamepad input over USB (most computers) OR PWM wire signals.
  - Making the end device compatible is up to end user.

### Software

The input processor device requires most of the software, including device drivers and the ability to run Python.

- `Python 3.10` or higher (3.11+ recommended)
  - **Ground station** (`src/ground_station/`): `customtkinter`, `pygame` — see `src/ground_station/requirements.txt`
  - **SBC drone control / box** (`src/drone_control/`): `pyserial` on Linux (`requirements-linux.txt`); plus GPIO/sensor packages on Raspberry Pi (`requirements-raspberry.txt`)
  - Main project development was done in Python 3.11–3.12; other versions may work but are untested.
- Depending on connection to TX (explained later), these drivers are necesary:
  - [STM32 Virtual COM Port Driver](https://www.st.com/en/development-tools/stsw-stm32102.html). For connecting to TX over USB.
  - [FTDI Virtual Com Port Driver](https://ftdichip.com/drivers/) & [FT_PROG](https://ftdichip.com/utilities/#ft_prog). For connecting to TX using FTDI Adapter and S.PORT Pin.
  - Optional, but also useful: [Zadig](https://zadig.akeo.ie/). Used to fix driver issues per device.

The output Raspberry Pi Pico can be programmed using the Arduino IDE or PlatformIO

- When programming for the HID Gamepad output for the Pico, follow the requirements set by the mikeneiderhauser [CRSFJoystick](https://github.com/mikeneiderhauser/CRSFJoystick) repository.
  - This uses PlatformIO to send data to the Pico.
  - A **Note:** the Pico bootloaded often appears as a USB device when plugged in to a Windows 10 computer, I often had to use the following options in my `platformio.ini` file:

# Wiring

## TX Side Wiring

The transmitter can be wired in two ways:

- Using a USB connection.
  - Easily compatible with ESP-32 based ELRS TX modules.
  - May require external power at high outputs (>100mw).
  - Requires minor modification to TX hardware settings.
- Using a FTDI serial adapter, plugged into the S.PORT pin on a TX module.
  - Compatible with all CRSF TX modules.
  - Likely to need external power, especially >25mw.
  - No changes to hardware settings.

**Note:** Please check out detailed wiring instructions in the Kaack [ELRS Joystick Control](https://github.com/kaack/elrs-joystick-control) repository, that information is basically being parroted here. 

### USB Connection

For this connection method, the [STM32 Virtual COM Port Drivers](https://www.st.com/en/development-tools/) are probably needed to be installed.

ExpressLRS TX Modules have a Web UI that is used to edit settings and other configuration. This is well documented in the [ExpressLRS Quick Start Guide](https://www.expresslrs.org/quick-start/getting-started/).

To make an ExpressLRS compatible over USB, we are changing the CRSF I/O pin from the module's S.BUS pin to the USB port. Go to the Web UI for the TX module, and go to the endpoint `/hardware.html`, by default this would be `10.0.0.1/hardware.html`.

At this endpoint, set the CRSF Serial Pins `RX pin` to `3` and `TX pin` to `1`.

- **Note:** Pins 3 and 1 are for nearly all ESP-32 based ELRS modules. The Kaack tutorial above also mentions to use the pins noted for `Backpack / Logging`, but that may or may not work for your TX. The Radiomaster Ranger Micro uses 3 and 1 for USB, while the Backpack / Logging section lists different pins.
- The CRSF pins will need to be reset to the original default if using device via the S.PORT again in the future.

After saving, the TX module should be ready to work over USB. A good way to check if it is working is the Fourflies [ELRS Buddy](https://github.com/Fourflies/elrsbuddy) website. This runs an ELRS configuration LUA script in a web browser, where you can also change power and transmission settings.

Should the TX Module need external power, it can be powered by the XT-30 plug (if available) or via the JR Bay power pins (shown in diagram in next section).

### FTDI Connection

For this connection method, the [FTDI Virtual Com Port Driver](https://ftdichip.com/drivers/) and the [FT_PROG tool](https://ftdichip.com/utilities/#ft_prog) are probably needed to be installed.

Follow the wiring instructions for your module type (see the Kaack repository above) to hook up to the FTDI adapter. Have a common ground between all parts.

- **Note:** A battery is not required, but is highly recommended because most FTDI adapters can only output about 500ma current through VCC, causing higher-power TX modules to reboot. If you choose to not use a battery, plug in the VBAT on the JR Bay TX to the VCC on the FTDI adapter and connect grounds. **DO NOT CONNECT BOTH VCC OF THE ADAPTER AND VBAT OF THE BATTERY**

Next, plug in the FTDI adapter over USB to your computer. Using the FT_Prog tool set, the setting of the adapter to run Inverted Half-Duplext UART. Both RX and TX signals are inverted and travel over the TX wire. This should make the TX module ready to work using the FTDI adapter.

## RX Side Wiring

The second section of wiring is for the Raspberry Pi Pico device and the RX chip. This simply follows the wiring set out by the mikeneiderhauser [CRSFJoystick](https://github.com/mikeneiderhauser/CRSFJoystick) repository. Wire the CRSF pins to the UART TX and RX of the Pico, and supply power to the chip.

# Code

## TX Side

The Python transmission stack is split across a **PC/laptop client** and a **SBC bridge** (Raspberry Pi, or Luckfox Pico Pro/Max on OpenIPC). The client reads a local gamepad, sends 16 RC channels over the network, and can control an enclosure “box” over HTTP. The board receives those channels, forwards CRSF to the ELRS/Crossfire TX over USB serial or SoC UART, and runs the box HTTP API in the same process as the bridge. Application code lives under `src/` (`common/`, `ground_station/`, `drone_control/`).

### Imports and `PYTHONPATH`

Modules import as `common.*`, `modules.*` (under `ground_station/`), and `drone_control.*`. The SBC sets `PYTHONPATH=src` from the repo root. On the PC, run the client as a script (below); `src/ground_station/main.py` adds its own directory and `src/` to `sys.path` at startup, so no `PYTHONPATH` is needed.

### Ground station client (`src/ground_station/main.py`)

CustomTkinter UI: map a gamepad to 16 RC channels (`src/ground_station/controller_map.txt`), TCP-connect to the SBC bridge on **Connect**, UDP-send channel frames at the configured rate. **Box** tab: status, LED, servo, drone power, camera stream (via `drone_control.box_server` on the board).

```bash
cd fpv-wireless-controls
python3 -m venv .venv && source .venv/bin/activate   # optional
pip install -r src/ground_station/requirements.txt

python src/ground_station/main.py
```

Dependencies: `customtkinter`, `pygame` (see `src/ground_station/requirements.txt`).

### TX bridge (`src/drone_control/main.py`)

Receives UDP channel packets from the client and forwards CRSF to the transmitter over USB serial. Starts `drone_control.box_server` in a background thread and passes the same bearer token in the TCP handshake (`OK <name> <token>`).

Serial / CRSF output mode defaults come from `src/ground_station/controller_map.txt` on the board if present (`--config`); that file is read as data only, not imported as Python. Baud is fixed in code: **420000** for `uart` (SBC→FC CRSF), **400000** for `tx` (USB TX module).

Raspberry Pi OS (Bookworm+) blocks system-wide `pip install` (PEP 668). Use a venv:

```bash
cd ~/Documents/fpv-wireless-controls
sudo apt install -y python3-venv python3-full   # once, if needed
python3 -m venv .venv
source .venv/bin/activate
pip install -r src/drone_control/requirements-raspberry.txt

export PYTHONPATH=src    # SBC only; safe for drone_control.*
python -m drone_control.main
```

Do not run a separate `box_server` while the bridge is running (both use port `50502` by default).

Optional standalone box API (debug or box-only board):

```bash
source .venv/bin/activate
PYTHONPATH=src python -m drone_control.box_server
```

Raspberry Pi dependencies: `pyserial` plus GPIO/sensor stack in `src/drone_control/requirements-raspberry.txt`. Generic Linux / OpenIPC: `src/drone_control/requirements-linux.txt`.

### Luckfox Pico Pro/Max (OpenIPC)

Same `python -m drone_control.main` bridge on a Rockchip RV1106 Luckfox Pico Pro or Max running **OpenIPC**. The board is autodetected (`/etc/majestic.yaml`, device-tree model); override with `BOX_BOARD=luckfox` if needed.

**MIPI camera:** Majestic already owns the CSI sensor. Do not use `rpicam-vid` or stock `rkipc`. Box **Video** on/off sets Majestic `outgoing.server` to `udp://<ground-station>:5004` (RTP H264, same viewer as Pi MIPI). Keep Majestic running. Optional: `BOX_MAJESTIC_URL` (default `http://127.0.0.1`).

**UART (CRSF to FC):** default `/dev/ttyS3` (UART3_M1). Override with `uart_port` in `controller_map.txt` if needed. Enable UART3 in OpenIPC pinmux; do not use UART2 (debug console).

**GPIO / PWM** (Linux numbers, Pico-style header — not Pi BCM):

| Function | Default | Notes |
| --- | --- | --- |
| LED | GPIO 55 (GPIO1_C7, header pin 4) | sysfs |
| Servo | PWM10 (`/sys/class/pwm/pwmchip10`) on GPIO1_C6 | enable PWM10_M1 |
| Drone power | GPIO 52 (GPIO1_C4) | sysfs |
| I2C | `/dev/i2c-3` | I2C3_M1; Adafruit sensor packages are optional |

Overrides: `BOX_LED_PIN`, `BOX_SERVO_PIN`, `BOX_DRONE_POWER_PIN`, `BOX_SERVO_PWMCHIP`, `BOX_I2C_BUS`.

OpenIPC images often omit Python. If `python3` is present (or you add it), install the Linux file — **not** `requirements-raspberry.txt` (RPi.GPIO / lgpio will fail):

```bash
pip3 install -r src/drone_control/requirements-linux.txt
export PYTHONPATH=src
python3 -m drone_control.main
```

### Protocol modules (shared)


| Module                  | Purpose                                         |
| ----------------------- | ----------------------------------------------- |
| `src/common/crsf.py`    | Build CRSF channel packets from PWM values      |
| `src/common/network.py` | Handshake, UDP framing, subnet scan for bridges |
| `src/common/box_api.py` | HTTP route paths and port (`50502`)             |
| `src/common/tx_port.py` | Autodetect CRSF serial device                   |


### **Protocol Note**

Depending on how the channel configuration is set up in the Radio TX hardware, the final output device may display different CRSF values. TBS Crossfire systems typically send a full 16 channels, though ELRS is designed to be more efficient. So it may be set up differently, read [this ELRS documentation](https://www.expresslrs.org/software/switch-config/) for switch configurations.

## RX Side

The output of the Raspberry Pi Pico can be primarily run as an emulated gamepad.

### Emulated Gamepad

Follow the instructions from mikeneiderhauser [CRSFJoystick](https://github.com/mikeneiderhauser/CRSFJoystick) repository!