#!/usr/bin/env python3
"""One-shot absolute-position gripper calibration on PCA9685 channel 0.

Manual gripper placement is not treated as a PWM position reference. This test
only explores the absolute pulse interval 1600-1700 us, ramping slowly upward.
"""

from __future__ import annotations

import argparse
import re
import select
import struct
import sys
import time
from datetime import datetime
from pathlib import Path

import board
import busio
from adafruit_pca9685 import PCA9685


PCA9685_ADDRESS = 0x40
FREQUENCY_HZ = 50
GRIPPER_CHANNEL = 0
MIN_PULSE_US = 1600
MAX_PULSE_US = 1700
START_PULSE_US = 1600
TARGET_PULSE_US = 1700
STEP_US = 5
RAMP_DELAY_SECONDS = 0.20
HOLD_SECONDS = 2.0

EV_KEY = 0x01
KEY_PRESSED = 1
BTN_START = 315
BTN_MODE = 316
EVENT_STRUCT = struct.Struct("llHHI")


class EmergencyStop(Exception):
    pass


def pulse_us_to_duty_cycle(pulse_us: int) -> int:
    period_us = 1_000_000 / FREQUENCY_HZ
    duty_fraction = pulse_us / period_us
    return max(0, min(0xFFFF, round(duty_fraction * 0xFFFF)))


def set_gripper_pulse(pca: PCA9685, pulse_us: int) -> int:
    if pulse_us < MIN_PULSE_US or pulse_us > MAX_PULSE_US:
        raise ValueError(f"Refusing pulse outside {MIN_PULSE_US}-{MAX_PULSE_US} us.")
    duty_cycle = pulse_us_to_duty_cycle(pulse_us)
    pca.channels[GRIPPER_CHANNEL].duty_cycle = duty_cycle
    return duty_cycle


def print_command(action: str, pulse_us: int, duty_cycle: int | None = None) -> None:
    timestamp = datetime.now().isoformat(timespec="seconds")
    duty_part = "" if duty_cycle is None else f" | calculated_duty_cycle={duty_cycle}"
    print(
        f"{timestamp} | channel={GRIPPER_CHANNEL} | action={action} | "
        f"requested_pulse_us={pulse_us}{duty_part}",
        flush=True,
    )


def parse_input_devices() -> list[dict[str, str]]:
    devices: list[dict[str, str]] = []
    current: dict[str, str] = {}

    for line in Path("/proc/bus/input/devices").read_text().splitlines():
        if not line.strip():
            if current:
                devices.append(current)
                current = {}
            continue
        if line.startswith("N: Name="):
            current["name"] = line.split("=", 1)[1].strip().strip('"')
        elif line.startswith("H: Handlers="):
            current["handlers"] = line.split("=", 1)[1].strip()

    if current:
        devices.append(current)
    return devices


def find_controller_event() -> str | None:
    for device in parse_input_devices():
        name = device.get("name", "")
        handlers = device.get("handlers", "")
        if "Wireless Controller" not in name:
            continue
        if "Motion Sensors" in name or "Touchpad" in name:
            continue
        match = re.search(r"\bevent\d+\b", handlers)
        if match:
            return f"/dev/input/{match.group(0)}"
    return None


def emergency_pressed(event_file) -> bool:
    if event_file is None:
        return False

    while True:
        readable, _, _ = select.select([event_file], [], [], 0.0)
        if not readable:
            return False

        data = event_file.read(EVENT_STRUCT.size)
        if len(data) != EVENT_STRUCT.size:
            continue

        _, _, event_type, code, value = EVENT_STRUCT.unpack(data)
        if event_type == EV_KEY and value == KEY_PRESSED and code in (BTN_START, BTN_MODE):
            return True


def sleep_with_emergency_check(seconds: float, event_file) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if emergency_pressed(event_file):
            raise EmergencyStop
        time.sleep(min(0.05, deadline - time.monotonic()))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ramp channel-0 gripper from 1600 us to 1700 us once."
    )
    parser.add_argument("--event-device", default=None)
    parser.add_argument("--confirm-motion-ready", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.confirm_motion_ready:
        print("Refusing to run without --confirm-motion-ready.", file=sys.stderr)
        return 2

    event_device = args.event_device or find_controller_event()
    event_file = None

    i2c = busio.I2C(board.SCL, board.SDA)
    pca = PCA9685(i2c, address=PCA9685_ADDRESS)
    pca.frequency = FREQUENCY_HZ

    try:
        pca.channels[GRIPPER_CHANNEL].duty_cycle = 0
        print("READY - PWM disabled before calibration start", flush=True)
        print(
            f"RAMPING channel {GRIPPER_CHANNEL}: "
            f"{START_PULSE_US} -> {TARGET_PULSE_US} us in {STEP_US} us steps",
            flush=True,
        )
        if event_device:
            event_file = open(event_device, "rb", buffering=0)
            print(f"Emergency stop: OPTIONS/START or PS/MODE on {event_device}", flush=True)

        pulse_us = START_PULSE_US
        while pulse_us <= TARGET_PULSE_US:
            if emergency_pressed(event_file):
                raise EmergencyStop
            duty_cycle = set_gripper_pulse(pca, pulse_us)
            print_command("INCREASING", pulse_us, duty_cycle)
            if pulse_us == TARGET_PULSE_US:
                break
            sleep_with_emergency_check(RAMP_DELAY_SECONDS, event_file)
            pulse_us += STEP_US

        print_command("HOLD", TARGET_PULSE_US)
        sleep_with_emergency_check(HOLD_SECONDS, event_file)
        return 0
    except EmergencyStop:
        pca.channels[GRIPPER_CHANNEL].duty_cycle = 0
        print("EMERGENCY STOP - PWM DISABLED", flush=True)
        return 0
    except KeyboardInterrupt:
        print("Ctrl+C received; disabling PWM.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        pca.channels[GRIPPER_CHANNEL].duty_cycle = 0
        if event_file is not None:
            event_file.close()
        pca.deinit()
        print(f"PWM disabled on channel {GRIPPER_CHANNEL}.", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
