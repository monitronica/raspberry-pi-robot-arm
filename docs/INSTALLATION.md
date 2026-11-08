# Installation

These instructions use **Raspberry Pi OS Lite** as the baseline operating system. The project does not require the full desktop image; local preview can use the configured framebuffer LCD, and the browser preview is served over MJPEG. Package names and display configuration can vary by OS release.

## 1. Install Raspberry Pi OS Lite

Use Raspberry Pi Imager to write a current Raspberry Pi OS Lite image suitable for the Raspberry Pi 5. Configure only the hostname, user, locale, network, and SSH access needed for your installation. Do not store credentials, Wi-Fi configuration, SSH keys, or private network details in this repository.

After first boot, update the OS using the normal Raspberry Pi OS package-management procedure. Keep servo power disconnected throughout operating-system setup.

## 2. Prepare the Pi interfaces

Keep the external servo PSU disconnected during interface setup.

Install the I2C utilities:

```bash
sudo apt update
sudo apt install i2c-tools
```

Enable I2C using Raspberry Pi OS configuration:

```bash
sudo raspi-config
```

In `raspi-config`, select **Interface Options → I2C → Enable**, exit, and reboot:

```bash
sudo reboot
```

After reconnecting, verify that I2C bus 1 exists and scan it:


```bash
ls -l /dev/i2c-1
i2cdetect -y 1
```

The PCA9685 should appear at address `40` (hexadecimal `0x40`). An I2C scan does not intentionally command servo motion, but the servo PSU must remain disconnected during initial setup. If `0x40` is absent, power down and inspect `SDA`, `SCL`, logic power, address-selection solder bridges, and common ground before continuing.

Do not continue to a motion command if the scan reports the wrong address or multiple unexplained devices. Never run `i2cdetect` continuously while the controller is operating.

### Camera requirements and setup

Use a Linux UVC/V4L2-compatible USB camera that OpenCV can open as `/dev/videoN`. The main controller requests 640×480 by default and should be tested at that resolution before trying larger frames. Person detection needs a stable, reasonably well-lit image with enough distance to see a substantial portion of a person; severe backlighting, darkness, motion blur, or a tightly cropped view will reduce detection reliability.

Install the optional V4L2 inspection utility and connect only one camera during initial setup when practical:

```bash
sudo apt install v4l-utils
v4l2-ctl --list-devices
v4l2-ctl --device=/dev/video0 --list-formats-ext
```

The camera may expose more than one `/dev/videoN` node. Select the node that provides ordinary capture frames, not a metadata-only interface. The controller auto-detects candidates, or accepts a fixed index/path with `--camera 0` or `--camera /dev/video0`.

Only one process may own the selected camera device. Stop `person-follow.service` before running a manual preview, diagnostic, light-react controller, demo, or random-motion camera script. Do not run two camera controllers concurrently.

Camera access alone does not require servo power. After creating `.venv` in step 3, perform the first preview test with the external servo PSU disconnected:

```bash
.venv/bin/python person_follow_camera.py --camera /dev/video0 --preview --frames 100
```

Do not add `--track` or `--confirm-motion-ready` to this camera-only test. Confirm correct orientation, useful field of view, stable capture, and acceptable lighting before enabling any movement. Mount the camera rigidly so servo movement does not loosen the connector or unexpectedly shift the tracking frame.

### Display interfaces

The direct LCD preview requires a Linux framebuffer at `/dev/fb0`. The current controller supports direct framebuffer output only when that device reports **16 bits per pixel** and exposes readable geometry/stride metadata under `/sys/class/graphics/fb0/`. The runtime user must be allowed to open `/dev/fb0` for writing.

Configure the LCD using its vendor-supported Raspberry Pi OS method, then verify:

```bash
ls -l /dev/fb0
cat /sys/class/graphics/fb0/virtual_size
cat /sys/class/graphics/fb0/bits_per_pixel
cat /sys/class/graphics/fb0/stride
```

Expected results are an existing framebuffer, a numeric virtual size, `16` bits per pixel, and a numeric stride. With the default 640×480 camera input, the controller produces a 960×720 preview and centers it in the framebuffer. The framebuffer dimensions must therefore be at least 960×720 for that default path; the current direct writer does not crop an oversized preview to a smaller framebuffer. Verify that the image is not cropped or distorted on the actual LCD.

Do not loosen framebuffer device permissions globally. Grant the service user access using the device group/rules appropriate to the installed Raspberry Pi OS and display driver, then confirm access after reboot. The included systemd unit runs as user `robota`, so that account must have the same framebuffer access as an interactive test user.

After completing the Python environment setup, test the LCD with camera preview only and the external servo PSU disconnected:

```bash
.venv/bin/python person_follow_camera.py --camera /dev/video0 --preview --frames 100
```

Do not include `--track` in this display test. The program reports `preview backend: framebuffer /dev/fb0` when the direct backend opens successfully. If direct framebuffer access is unavailable, pygame may use another configured SDL display backend; without a usable display backend, preview is disabled while headless camera processing or MJPEG can still be used.

Verify the camera separately with:

```bash
ls -l /dev/video*
```

## 3. Clone and create a Python virtual environment

Use a project-local virtual environment at `.venv`. This keeps project packages separate from Raspberry Pi OS Python packages and matches the interpreter path used by `start_person_follow.sh`. Do not install the project dependencies globally with `sudo pip`.

Install the OS support needed to create the environment and use I2C/OpenCV on Raspberry Pi OS Lite:

```bash
sudo apt update
sudo apt install python3-venv python3-opencv python3-numpy python3-pygame i2c-tools
```

```bash
git clone https://github.com/monitronica/raspberry-pi-robot-arm.git
cd raspberry-pi-robot-arm
python3 -m venv .venv --system-site-packages
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install adafruit-blinka adafruit-circuitpython-pca9685 adafruit-circuitpython-servokit
```

If the `venv` module is unavailable, install the Raspberry Pi OS `python3-venv` package, then create `.venv` again. The `--system-site-packages` option permits use of OS-provided packages such as `python3-opencv` when needed on Raspberry Pi OS.

Verify that commands resolve to the project environment:

```bash
which python
python -c "import sys; print(sys.prefix)"
```

The paths should point inside the repository's `.venv`. Activate it in each new interactive shell with `source .venv/bin/activate`; the included launcher calls `.venv/bin/python` directly and does not require shell activation.

### Python packages inferred from the source

| Import used by the project | Install source/package | Used for |
| --- | --- | --- |
| `cv2` | Raspberry Pi OS `python3-opencv` (or pip `opencv-python`) | Camera capture, person detection, overlays, and JPEG encoding |
| `numpy` | Raspberry Pi OS `python3-numpy` (or pip `numpy`) | Frame and image-array processing |
| `board`, `busio` | pip `adafruit-blinka` | Raspberry Pi pin/I2C compatibility layer |
| `adafruit_pca9685` | pip `adafruit-circuitpython-pca9685` | PCA9685 control used by the main controller and motion tools |
| `pygame` | Raspberry Pi OS `python3-pygame` (or pip `pygame`) | Optional local preview/display backend |
| `adafruit_servokit` | pip `adafruit-circuitpython-servokit` | Import/diagnostic tooling; not used by the main follow controller |
| `adafruit_bus_device` | Installed as an Adafruit dependency | Low-level PCA9685 timing diagnostic |
| `lgpio` | Raspberry Pi OS package or pip package appropriate to the OS | Only the separate GPIO18 servo experiment |

Everything else imported by the checked-in Python files—such as `argparse`, `http.server`, `fcntl`, `signal`, and `threading`—comes from Python's standard library on Linux. The `lgpio` experiment is not required for `person_follow_camera.py`; install it only if that specific experimental script is intentionally being used.

Avoid installing both OS and pip builds of OpenCV/NumPy into the same environment unless needed to resolve a verified compatibility issue. The commands above use the Raspberry Pi OS builds through `--system-site-packages`.

On Raspberry Pi OS, OpenCV may be more reliable from the OS package (`python3-opencv`) than from pip. If so, retain `--system-site-packages` when creating the environment.

## 4. Perform software-only checks

Do not run a motion or calibration script as an installation test. Compile the source without executing it:

```bash
python -m compileall -q .
```

The main controller also has a startup-path test which does not open the PCA9685, camera, preview, or write PWM:

```bash
printf 'HOME\n' | .venv/bin/python person_follow_camera.py \
  --track --confirm-motion-ready --dry-start-no-pwm
```

## 5. Review configuration before motion

Read [SAFETY.md](SAFETY.md), [WIRING.md](WIRING.md), and [SERVO_MAPPING.md](SERVO_MAPPING.md). Confirm that channel assignment and neutral geometry match this exact arm. Keep the servo rail disconnected until the inspection is complete.

Autostart is optional and should only be enabled after controlled manual verification. See [AUTOSTART.md](AUTOSTART.md).

