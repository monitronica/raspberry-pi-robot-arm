#!/usr/bin/env python3
"""Minimal PlayStation controller direction test for PCA9685 channel 0."""

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
SERVO_CHANNEL = 0
MIN_PULSE_US = 1450
MAX_PULSE_US = 1550
ARM_PULSE_US = 1500
PRESS_STEP_US = 2
RAMP_STEP_US = 1
RAMP_DELAY_SECONDS = 0.30
DEBOUNCE_SECONDS = 0.25

EV_KEY = 0x01
EV_ABS = 0x03
KEY_PRESSED = 1
TRIGGER_AXIS_THRESHOLD = 32

ABS_Z = 2
ABS_RZ = 5
BTN_SOUTH = 304
BTN_TL2 = 312
BTN_TR2 = 313
BTN_START = 315
BTN_MODE = 316

EVENT_STRUCT = struct.Struct("llHHI")

BUTTON_ACTIONS = {
    BTN_SOUTH: "ARM",
    BTN_TL2: "DIR_A",
    BTN_TR2: "DIR_B",
    BTN_START: "EMERGENCY_STOP",
    BTN_MODE: "EMERGENCY_STOP",
}

AXIS_ACTIONS = {
    ABS_Z: "DIR_A",
    ABS_RZ: "DIR_B",
}


class EmergencyStop(Exception):
    pass


def pulse_us_to_duty_cycle(pulse_us: int) -> int:
    period_us = 1_000_000 / FREQUENCY_HZ
    duty_fraction = pulse_us / period_us
    return max(0, min(0xFFFF, round(duty_fraction * 0xFFFF)))


def clamp_pulse(pulse_us: int) -> int:
    return max(MIN_PULSE_US, min(MAX_PULSE_US, pulse_us))


def set_servo_pulse(pca: PCA9685, pulse_us: int) -> int:
    if pulse_us < MIN_PULSE_US or pulse_us > MAX_PULSE_US:
        raise ValueError(f"Refusing pulse outside {MIN_PULSE_US}-{MAX_PULSE_US} us.")
    duty_cycle = pulse_us_to_duty_cycle(pulse_us)
    pca.channels[SERVO_CHANNEL].duty_cycle = duty_cycle
    return duty_cycle


def print_command(action: str, pulse_us: int, duty_cycle: int | None = None) -> None:
    timestamp = datetime.now().isoformat(timespec="seconds")
    duty_text = "" if duty_cycle is None else f" | duty_cycle={duty_cycle}"
    print(
        f"{timestamp} | channel={SERVO_CHANNEL} | {action} | "
        f"pulse_us={pulse_us}{duty_text}",
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


def find_controller_event() -> str:
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
    raise RuntimeError("Could not find PlayStation Wireless Controller event device.")


def decode_action(
    event_type: int,
    code: int,
    value: int,
    axis_active: dict[int, bool],
    trigger_key_seen: dict[int, bool],
) -> str | None:
    if event_type == EV_KEY:
        if code in trigger_key_seen:
            trigger_key_seen[code] = True
        if value != KEY_PRESSED:
            return None
        return BUTTON_ACTIONS.get(code)

    if event_type != EV_ABS or code not in AXIS_ACTIONS:
        return None

    if code == ABS_Z and trigger_key_seen[BTN_TL2]:
        axis_active[code] = value > 0
        return None
    if code == ABS_RZ and trigger_key_seen[BTN_TR2]:
        axis_active[code] = value > 0
        return None

    is_pressed = value >= TRIGGER_AXIS_THRESHOLD
    if axis_active[code] or not is_pressed:
        axis_active[code] = is_pressed
        return None
    axis_active[code] = True
    return AXIS_ACTIONS[code]


def check_emergency_stop(
    event_file,
    axis_active: dict[int, bool],
    trigger_key_seen: dict[int, bool],
) -> None:
    while True:
        readable, _, _ = select.select([event_file], [], [], 0.0)
        if not readable:
            return

        data = event_file.read(EVENT_STRUCT.size)
        if len(data) != EVENT_STRUCT.size:
            continue

        _, _, event_type, code, value = EVENT_STRUCT.unpack(data)
        action = decode_action(event_type, code, value, axis_active, trigger_key_seen)
        if action == "EMERGENCY_STOP":
            raise EmergencyStop


def sleep_with_emergency_check(
    seconds: float,
    event_file,
    axis_active: dict[int, bool],
    trigger_key_seen: dict[int, bool],
) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        check_emergency_stop(event_file, axis_active, trigger_key_seen)
        time.sleep(min(0.05, deadline - time.monotonic()))


def ramp_to_target(
    pca: PCA9685,
    event_file,
    current_pulse_us: int,
    target_pulse_us: int,
    axis_active: dict[int, bool],
    trigger_key_seen: dict[int, bool],
) -> int:
    if target_pulse_us == current_pulse_us:
        duty_cycle = set_servo_pulse(pca, current_pulse_us)
        print_command("UNCHANGED", current_pulse_us, duty_cycle)
        return current_pulse_us

    direction = RAMP_STEP_US if target_pulse_us > current_pulse_us else -RAMP_STEP_US
    pulse_us = current_pulse_us

    while pulse_us != target_pulse_us:
        check_emergency_stop(event_file, axis_active, trigger_key_seen)
        pulse_us += direction
        duty_cycle = set_servo_pulse(pca, pulse_us)
        sleep_with_emergency_check(
            RAMP_DELAY_SECONDS,
            event_file,
            axis_active,
            trigger_key_seen,
        )

    print_command("L2 DIR A" if direction < 0 else "R2 DIR B", pulse_us, duty_cycle)
    return pulse_us


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Basic direction test for servo 0 using a PlayStation controller."
    )
    parser.add_argument("--event-device", default=None)
    parser.add_argument("--confirm-motion-ready", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.confirm_motion_ready:
        print("Refusing to run without --confirm-motion-ready.", file=sys.stderr)
        return 2

    try:
        event_device = args.event_device or find_controller_event()
    except Exception as exc:
        print(f"Controller setup failed: {exc}", file=sys.stderr)
        return 2

    axis_active = {ABS_Z: False, ABS_RZ: False}
    trigger_key_seen = {BTN_TL2: False, BTN_TR2: False}
    last_press_by_code: dict[int, float] = {}
    current_pulse_us: int | None = None
    armed = False

    i2c = busio.I2C(board.SCL, board.SDA)
    pca = PCA9685(i2c, address=PCA9685_ADDRESS)
    pca.frequency = FREQUENCY_HZ

    try:
        pca.channels[SERVO_CHANNEL].duty_cycle = 0
        print("READY - SERVO 0 BASIC DIRECTION TEST", flush=True)
        print(
            "X/CROSS=ARM 1500 us | L2=DIR A -2 us | R2=DIR B +2 us | "
            "OPTIONS/START or PS/MODE=EMERGENCY_STOP",
            flush=True,
        )

        with open(event_device, "rb", buffering=0) as event_file:
            while True:
                readable, _, _ = select.select([event_file], [], [], 0.1)
                if not readable:
                    continue

                data = event_file.read(EVENT_STRUCT.size)
                if len(data) != EVENT_STRUCT.size:
                    continue

                _, _, event_type, code, value = EVENT_STRUCT.unpack(data)
                action = decode_action(event_type, code, value, axis_active, trigger_key_seen)
                if action is None:
                    continue

                now = time.monotonic()
                if now - last_press_by_code.get(code, 0.0) < DEBOUNCE_SECONDS:
                    continue
                last_press_by_code[code] = now

                if action == "EMERGENCY_STOP":
                    pca.channels[SERVO_CHANNEL].duty_cycle = 0
                    print_command("EMERGENCY_STOP", current_pulse_us or 0)
                    return 0

                if action == "ARM":
                    current_pulse_us = ARM_PULSE_US
                    duty_cycle = set_servo_pulse(pca, current_pulse_us)
                    armed = True
                    print_command("X/CROSS ARM", current_pulse_us, duty_cycle)
                    continue

                if not armed or current_pulse_us is None:
                    print("IGNORED - PRESS X/CROSS TO ARM FIRST", flush=True)
                    continue

                if action == "DIR_A":
                    target_pulse_us = clamp_pulse(current_pulse_us - PRESS_STEP_US)
                elif action == "DIR_B":
                    target_pulse_us = clamp_pulse(current_pulse_us + PRESS_STEP_US)
                else:
                    continue

                current_pulse_us = ramp_to_target(
                    pca,
                    event_file,
                    current_pulse_us,
                    target_pulse_us,
                    axis_active,
                    trigger_key_seen,
                )

    except EmergencyStop:
        pca.channels[SERVO_CHANNEL].duty_cycle = 0
        print_command("EMERGENCY_STOP", current_pulse_us or 0)
        return 0
    except KeyboardInterrupt:
        print("Ctrl+C received; disabling PWM.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        pca.channels[SERVO_CHANNEL].duty_cycle = 0
        pca.deinit()
        print(f"PWM disabled on channel {SERVO_CHANNEL}.", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
