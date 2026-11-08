#!/usr/bin/env python3
"""Tiny-step PlayStation-controller gripper calibration on PCA9685 channel 0.

This script does not infer physical gripper position from manual placement and
does not command any PWM value at startup. A reference pulse must be selected
with the D-pad before X/CROSS arms servo control.
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
MIN_PULSE_US = 1350
MAX_PULSE_US = 1500
REFERENCE_STEP_US = 10
TARGET_STEP_US = 2
RAMP_STEP_US = 1
RAMP_DELAY_SECONDS = 0.30
DEBOUNCE_SECONDS = 0.25

EV_KEY = 0x01
EV_ABS = 0x03
KEY_PRESSED = 1
TRIGGER_AXIS_THRESHOLD = 32

ABS_Z = 2
ABS_RZ = 5
ABS_HAT0X = 16
BTN_SOUTH = 304
BTN_TL2 = 312
BTN_TR2 = 313
BTN_START = 315
BTN_MODE = 316

BUTTON_ACTIONS = {
    BTN_SOUTH: "ARM",
    BTN_TL2: "DECREASE",
    BTN_TR2: "INCREASE",
    BTN_START: "EMERGENCY_STOP",
    BTN_MODE: "EMERGENCY_STOP",
}

AXIS_ACTIONS = {
    ABS_Z: "DECREASE",
    ABS_RZ: "INCREASE",
}

EVENT_STRUCT = struct.Struct("llHHI")


class EmergencyStop(Exception):
    pass


def pulse_us_to_duty_cycle(pulse_us: int) -> int:
    period_us = 1_000_000 / FREQUENCY_HZ
    duty_fraction = pulse_us / period_us
    return max(0, min(0xFFFF, round(duty_fraction * 0xFFFF)))


def clamp_pulse(pulse_us: int) -> int:
    return max(MIN_PULSE_US, min(MAX_PULSE_US, pulse_us))


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


def decode_controller_action(
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

    if event_type == EV_ABS and code == ABS_HAT0X:
        if value < 0:
            return "SELECT_DECREASE"
        if value > 0:
            return "SELECT_INCREASE"
        return None

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
        action = decode_controller_action(event_type, code, value, axis_active, trigger_key_seen)
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
        print_command("UNCHANGED", current_pulse_us)
        return current_pulse_us

    direction = RAMP_STEP_US if target_pulse_us > current_pulse_us else -RAMP_STEP_US
    action = "INCREASE" if direction > 0 else "DECREASE"
    pulse_us = current_pulse_us

    while pulse_us != target_pulse_us:
        check_emergency_stop(event_file, axis_active, trigger_key_seen)
        pulse_us += direction
        duty_cycle = set_gripper_pulse(pca, pulse_us)
        print_command(action, pulse_us, duty_cycle)
        sleep_with_emergency_check(
            RAMP_DELAY_SECONDS,
            event_file,
            axis_active,
            trigger_key_seen,
        )

    return pulse_us


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Tiny-step manual calibration for the channel-0 gripper."
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

    selected_reference_us: int | None = None
    current_pulse_us: int | None = None
    control_armed = False
    last_press_by_code: dict[int, float] = {}
    axis_active = {ABS_Z: False, ABS_RZ: False}
    trigger_key_seen = {BTN_TL2: False, BTN_TR2: False}

    i2c = busio.I2C(board.SCL, board.SDA)
    pca = PCA9685(i2c, address=PCA9685_ADDRESS)
    pca.frequency = FREQUENCY_HZ

    try:
        pca.channels[GRIPPER_CHANNEL].duty_cycle = 0
        print("READY - SELECT INITIAL REFERENCE", flush=True)
        print(
            f"D-pad LEFT/RIGHT selects reference in {REFERENCE_STEP_US} us steps "
            f"within {MIN_PULSE_US}-{MAX_PULSE_US} us; selection does not command PWM",
            flush=True,
        )
        print(
            f"X/CROSS=ARM | L2=DECREASE {TARGET_STEP_US} us | "
            f"R2=INCREASE {TARGET_STEP_US} us | OPTIONS/START or PS/MODE=EMERGENCY_STOP",
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
                action = decode_controller_action(
                    event_type,
                    code,
                    value,
                    axis_active,
                    trigger_key_seen,
                )
                if action is None:
                    continue

                now = time.monotonic()
                if now - last_press_by_code.get(code, 0.0) < DEBOUNCE_SECONDS:
                    continue
                last_press_by_code[code] = now

                if action == "EMERGENCY_STOP":
                    pca.channels[GRIPPER_CHANNEL].duty_cycle = 0
                    print_command("EMERGENCY_STOP", current_pulse_us or 0)
                    return 0

                if action == "SELECT_DECREASE":
                    if selected_reference_us is None:
                        selected_reference_us = MAX_PULSE_US
                    else:
                        selected_reference_us = clamp_pulse(
                            selected_reference_us - REFERENCE_STEP_US
                        )
                    print(f"SELECTED REFERENCE = {selected_reference_us} us", flush=True)
                    continue

                if action == "SELECT_INCREASE":
                    if selected_reference_us is None:
                        selected_reference_us = MIN_PULSE_US
                    else:
                        selected_reference_us = clamp_pulse(
                            selected_reference_us + REFERENCE_STEP_US
                        )
                    print(f"SELECTED REFERENCE = {selected_reference_us} us", flush=True)
                    continue

                if action == "ARM":
                    if selected_reference_us is None:
                        print("SELECT REFERENCE WITH D-pad BEFORE ARMING", flush=True)
                        continue
                    print(f"SELECTED REFERENCE BEFORE ARMING = {selected_reference_us} us", flush=True)
                    duty_cycle = set_gripper_pulse(pca, selected_reference_us)
                    current_pulse_us = selected_reference_us
                    control_armed = True
                    print_command("ARMED", current_pulse_us, duty_cycle)
                    continue

                if not control_armed or current_pulse_us is None:
                    print(f"IGNORED {action} - CONTROL NOT ARMED", flush=True)
                    continue

                if action == "DECREASE":
                    target_pulse_us = clamp_pulse(current_pulse_us - TARGET_STEP_US)
                elif action == "INCREASE":
                    target_pulse_us = clamp_pulse(current_pulse_us + TARGET_STEP_US)
                else:
                    continue

                print_command(f"{action}_REQUEST", target_pulse_us)
                current_pulse_us = ramp_to_target(
                    pca,
                    event_file,
                    current_pulse_us,
                    target_pulse_us,
                    axis_active,
                    trigger_key_seen,
                )

    except EmergencyStop:
        pca.channels[GRIPPER_CHANNEL].duty_cycle = 0
        print_command("EMERGENCY_STOP", current_pulse_us or 0)
        return 0
    except KeyboardInterrupt:
        print("Ctrl+C received; disabling PWM.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        pca.channels[GRIPPER_CHANNEL].duty_cycle = 0
        pca.deinit()
        print(f"PWM disabled on channel {GRIPPER_CHANNEL}.", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
