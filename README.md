# Raspberry Pi Robot Arm

> [!WARNING]
> This is a physical robotic arm capable of causing injury. Keep people, hair, clothing, and loose objects clear during startup and `HOME`. Provide an accessible servo-power disconnect. Test every new movement range slowly. `Ctrl+C` or a service stop must disable PWM. Software limits do not replace continuous physical supervision.

A Raspberry Pi 5-controlled Hiwonder/LeArm robotic arm with camera-based person detection and tracking. The current build combines a PCA9685 servo controller at I2C address `0x40` and 50 Hz, six PWM servos, a USB camera, OpenCV, a mirrored framebuffer LCD preview, an optional MJPEG browser stream, and systemd startup.

This repository preserves the working controller plus calibration and experimental scripts. Many scripts can move real hardware. Read [SAFETY.md](docs/SAFETY.md) and [SERVO_MAPPING.md](docs/SERVO_MAPPING.md) before running anything.

## Project lineage

**Apofenija/1 → Digital on Stage / KIBLA → controller failure → Circuit Control rebuild → Raspberry Pi / OpenCV experiments → possible ROS or custom interfaces**

This sequence traces several lives of the same machine, from its earlier artistic contexts through failure, repair, and renewed experimentation. The technical rebuild is therefore also part of an artistic and ethical practice centered on reuse, adaptation, and technological afterlives.

Rather than discarding the robot when its controller failed, the project treats its existing body as a platform for new relationships, interfaces, and works. **Keep the body, replace the brain, and see what the machine can become next.**

## Project gallery

<p align="center">
  <img src="docs/images/robot-arm-setup.jpg" alt="Raspberry Pi robot-arm project setup" width="760">
</p>

<p align="center"><em>Physical robot-arm setup with camera and local display. Screen content and bystanders are privacy-redacted.</em></p>

<p align="center">
  <img src="docs/images/tracking-display.jpg" alt="Local tracking display with tracking overlay" width="480">
</p>

<p align="center"><em>Local display showing the tracking overlay. People in the camera image are privacy-redacted.</em></p>

<table>
  <tr>
    <td align="center"><img src="docs/images/electronics-installed.jpg" alt="Electronics installed at the robot-arm base" width="250"><br><sub>Installed electronics</sub></td>
    <td align="center"><img src="docs/images/hiwonder-servos.jpg" alt="Hiwonder servos installed in the arm" width="250"><br><sub>Installed Hiwonder servos</sub></td>
    <td align="center"><img src="docs/images/pca9685-driver-front.jpg" alt="PCA9685 servo driver HAT front" width="250"><br><sub>PCA9685 driver HAT</sub></td>
  </tr>
</table>

## Architecture

```mermaid
flowchart LR
    Camera[USB Camera] --> OpenCV
    OpenCV --> Tracker[Person Tracker]
    Tracker --> Behavior[Behavior State]
    Behavior --> Targets[Servo Targets]
    Targets --> PCA9685
    PCA9685 --> Servos
    OpenCV --> Preview[Rendered Preview]
    Preview --> LCD[Framebuffer LCD]
    Preview --> MJPEG[MJPEG HTTP Stream]
```

## Features and status

| Feature | Status in this snapshot |
| --- | --- |
| `FOLLOW` person tracking | Working in `person_follow_camera.py` |
| `GREET` gesture | Working after sustained centered engagement |
| `HAPPY_DANCE` | Constants/telemetry retained, but no reachable dance sequence is implemented in the checked-in controller |
| Mirrored LCD preview | Working through pygame or direct 16-bit `/dev/fb0` output |
| `HELLO` during greeting | Intended behavior; the current overlay shows tracking/behavior state rather than a literal `HELLO` label |
| MJPEG web stream | Implemented; enabled with `--web-stream` |
| Light-react mode | Separate experimental controller, not part of normal autostart |
| Demo/random motion | Separate experimental scripts, not part of normal autostart |

## Hardware

- Raspberry Pi 5
- Hiwonder/LeArm six-servo robotic arm
- PCA9685 PWM controller (`0x40`, 50 Hz)
- Six PWM servos on channels 0–5
- USB camera
- Framebuffer LCD (optional for headless/browser-only use)
- Separate, correctly rated servo power supply with a readily accessible disconnect

The channel roles are:

| Channel | Role |
| --- | --- |
| 0 | Gripper |
| 1 | Expressive joint |
| 2 | Pointing/helper joint |
| 3 | Upright/support joint |
| 4 | Vertical tracking |
| 5 | Horizontal/base tracking |

See [hardware](docs/HARDWARE.md), [wiring](docs/WIRING.md), and the authoritative [servo mapping and range notes](docs/SERVO_MAPPING.md).

## Quick start

The documented baseline is Raspberry Pi OS Lite on a Raspberry Pi 5 with a project-local Python virtual environment at `.venv`. These commands describe the expected Raspberry Pi setup; they have not been run as part of this documentation update.

```bash
git clone https://github.com/monitronica/raspberry-pi-robot-arm.git
cd raspberry-pi-robot-arm
python3 -m venv .venv --system-site-packages
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install adafruit-blinka adafruit-circuitpython-pca9685 adafruit-circuitpython-servokit
```

OpenCV, NumPy, pygame, and `python3-venv` are documented as Raspberry Pi OS packages in the full [installation guide](docs/INSTALLATION.md), which also maps every non-standard Python import to its package.

Before enabling motion, inspect the wiring, mechanically support the arm, clear its workspace, and keep a hand near the servo power disconnect. The working entry point requires two explicit motion acknowledgements and a `HOME` line on standard input:

```bash
printf 'HOME\n' | .venv/bin/python person_follow_camera.py \
  --track --confirm-motion-ready --preview --web-stream
```

Open `http://<raspberry-pi-hostname>:8080/` for the optional MJPEG view. Do not publish this unauthenticated stream to the internet.

## Project history

The Raspberry Pi rebuild took place during [Circuit Control](https://circuit-control.de/?lang=en) in Dresden, a DIY electronics, soldering, and maker festival. That setting formed part of the context in which the robot's old controller was replaced and the existing machine was given a new control system. The project is an exercise in reuse, repair, and technological afterlife: **Keep the body, replace the brain, and see what the machine can become next.**

The current Raspberry Pi control system is a working stage in that process, not the intended final interface. Future directions could include [ROS or ROS 2](https://www.ros.org/) integration, a custom control interface, browser-based control, OSC or other network interfaces for performance, interfaces designed for performers or workshop participants, and modular control that lets the same physical robot serve different projects. ROS integration does not currently exist; for artistic and performance contexts, a purpose-built interface may ultimately be more appropriate.

See [Project history and future directions](docs/HISTORY.md) for the longer account.

## Documentation

- [Installation](docs/INSTALLATION.md)
- [Usage and modes](docs/USAGE.md)
- [Hardware](docs/HARDWARE.md)
- [Wiring](docs/WIRING.md)
- [Camera stream](docs/CAMERA_STREAM.md)
- [Servo mapping and ranges](docs/SERVO_MAPPING.md)
- [systemd autostart](docs/AUTOSTART.md)
- [Safety](docs/SAFETY.md)
- [Experiments](docs/EXPERIMENTS.md)
- [Project history and future directions](docs/HISTORY.md)

## References

- [Circuit Control](https://circuit-control.de/?lang=en)
- [ROS: Robot Operating System](https://www.ros.org/)


