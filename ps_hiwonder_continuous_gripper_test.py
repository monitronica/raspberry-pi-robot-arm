#!/usr/bin/env python3
"""Continuous-PWM Hiwonder gripper test on PCA9685 channel 0."""

from __future__ import annotations

import argparse
import select
import struct
import sys
import time
from datetime import datetime

import board
import busio
from adafruit_pca9685 import PCA9685


PCA9685_ADDRESS = 0x40
FREQUENCY_HZ = 50
GRIPPER_CHANNEL = 0
EVENT_DEVICE = "/dev/input/event5"

OPEN_PULSE_US = 1500
CLOSED_PULSE_US = 500
TRIGGER_TARGET_STEP_US = 50
RAMP_STEP_US = 5
RAMP_DELAY_SECONDS = 0.10
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


class EmergencyStop(Exception):
    pass


def pulse_us_to_duty_cycle(pulse_us: int) -> int:
    period_us = 1_000_000 / FREQUENCY_HZ
    duty_fraction = pulse_us / period_us
    return max(0, min(0xFFFF, round(duty_fraction * 0xFFFF)))


def clamp_pulse(pulse_us: int) -> int:
    return max(CLOSED_PULSE_US, min(OPEN_PULSE_US, pulse_us))


def disable_pwm(pca: PCA9685) -> None:
    pca.channels[GRIPPER_CHANNEL].duty_cycle = 0


def set_gripper_pulse(pca: PCA9685, pulse_us: int) -> int:
    if pulse_us < CLOSED_PULSE_US or pulse_us > OPEN_PULSE_US:
        raise ValueError(f"Refusing pulse outside {CLOSED_PULSE_US}-{OPEN_PULSE_US} us.")
    duty_cycle = pulse_us_to_duty_cycle(pulse_us)
    pca.channels[GRIPPER_CHANNEL].duty_cycle = duty_cycle
    return duty_cycle


def print_pulse(label: str, pulse_us: int, target_us: int, duty_cycle: int) -> None:
    timestamp = datetime.now().isoformat(timespec="seconds")
    print(
        f"{timestamp} | channel={GRIPPER_CHANNEL} | {label} | "
        f"current_us={pulse_us} | target_us={target_us} | duty_cycle={duty_cycle}",
        flush=True,
    )


def read_event(event_file):
    data = event_file.read(EVENT_STRUCT.size)
    return EVENT_STRUCT.unpack(data) if len(data) == EVENT_STRUCT.size else None


def decode_action(
    event_type: int,
    code: int,
    value: int,
    axis_active: dict[int, bool],
    trigger_key_seen: dict[int, bool],
) -> tuple[str, int] | str | None:
    if event_type == EV_KEY:
        if code in (BTN_START, BTN_MODE) and value == KEY_PRESSED:
            return "EMERGENCY_STOP"
        if code in trigger_key_seen:
            trigger_key_seen[code] = True
        if value != KEY_PRESSED:
            return None
        if code == BTN_SOUTH:
            return ("X", OPEN_PULSE_US)
        if code == BTN_TL2:
            return ("CLOSED", -TRIGGER_TARGET_STEP_US)
        if code == BTN_TR2:
            return ("OPEN", TRIGGER_TARGET_STEP_US)
        return None

    if event_type != EV_ABS or code not in (ABS_Z, ABS_RZ):
        return None

    key_code = BTN_TL2 if code == ABS_Z else BTN_TR2
    if trigger_key_seen[key_code]:
        axis_active[code] = value > 0
        return None

    pressed = value >= TRIGGER_AXIS_THRESHOLD
    if axis_active[code] or not pressed:
        axis_active[code] = pressed
        return None
    axis_active[code] = True

    if code == ABS_Z:
        return ("CLOSED", -TRIGGER_TARGET_STEP_US)
    return ("OPEN", TRIGGER_TARGET_STEP_US)


def check_emergency_stop(
    event_file,
    axis_active: dict[int, bool],
    trigger_key_seen: dict[int, bool],
) -> None:
    while True:
        readable, _, _ = select.select([event_file], [], [], 0.0)
        if not readable:
            return

        event = read_event(event_file)
        if event is None:
            continue

        _, _, event_type, code, value = event
        action = decode_action(event_type, code, value, axis_active, trigger_key_seen)
        if action == "EMERGENCY_STOP":
            raise EmergencyStop


def sleep_with_emergency_check(
    event_file,
    axis_active: dict[int, bool],
    trigger_key_seen: dict[int, bool],
) -> None:
    deadline = time.monotonic() + RAMP_DELAY_SECONDS
    while time.monotonic() < deadline:
        check_emergency_stop(event_file, axis_active, trigger_key_seen)
        time.sleep(min(0.05, deadline - time.monotonic()))


def ramp_to_target(
    pca: PCA9685,
    event_file,
    current_us: int,
    target_us: int,
    label: str,
    axis_active: dict[int, bool],
    trigger_key_seen: dict[int, bool],
) -> int:
    if current_us == target_us:
        duty_cycle = pulse_us_to_duty_cycle(current_us)
        print_pulse(f"{label} FINAL", current_us, target_us, duty_cycle)
        return current_us

    step = RAMP_STEP_US if target_us > current_us else -RAMP_STEP_US
    while current_us != target_us:
        check_emergency_stop(event_file, axis_active, trigger_key_seen)
        current_us += step
        if step > 0:
            current_us = min(current_us, target_us)
        else:
            current_us = max(current_us, target_us)
        duty_cycle = set_gripper_pulse(pca, current_us)
        print_pulse(label, current_us, target_us, duty_cycle)
        sleep_with_emergency_check(event_file, axis_active, trigger_key_seen)

    duty_cycle = pulse_us_to_duty_cycle(current_us)
    print_pulse(f"{label} FINAL", current_us, target_us, duty_cycle)
    return current_us


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Continuous-PWM Hiwonder gripper test.")
    parser.add_argument("--event-device", default=EVENT_DEVICE)
    parser.add_argument("--confirm-motion-ready", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.confirm_motion_ready:
        print("Refusing to run without --confirm-motion-ready.", file=sys.stderr)
        return 2

    current_us: int | None = None
    reference_established = False
    axis_active = {ABS_Z: False, ABS_RZ: False}
    trigger_key_seen = {BTN_TL2: False, BTN_TR2: False}
    last_press_by_code = {
        BTN_SOUTH: 0.0,
        BTN_TL2: 0.0,
        BTN_TR2: 0.0,
        ABS_Z: 0.0,
        ABS_RZ: 0.0,
    }

    i2c = busio.I2C(board.SCL, board.SDA)
    pca = PCA9685(i2c, address=PCA9685_ADDRESS)
    pca.frequency = FREQUENCY_HZ

    try:
        disable_pwm(pca)
        print("READY - PWM OFF", flush=True)
        print(
            "X/CROSS=establish 1500 us | L2=CLOSED toward 500 us | "
            "R2=OPEN toward 1500 us | OPTIONS/START or PS/MODE=EMERGENCY_STOP",
            flush=True,
        )

        with open(args.event_device, "rb", buffering=0) as event_file:
            while True:
                readable, _, _ = select.select([event_file], [], [], 0.1)
                if not readable:
                    continue

                event = read_event(event_file)
                if event is None:
                    continue

                _, _, event_type, code, value = event
                action = decode_action(event_type, code, value, axis_active, trigger_key_seen)
                if action is None:
                    continue

                if action == "EMERGENCY_STOP":
                    disable_pwm(pca)
                    print("EMERGENCY STOP - PWM DISABLED", flush=True)
                    return 0

                now = time.monotonic()
                if now - last_press_by_code.get(code, 0.0) < DEBOUNCE_SECONDS:
                    continue
                last_press_by_code[code] = now

                label, target_or_delta = action
                if label == "X":
                    duty_cycle = set_gripper_pulse(pca, OPEN_PULSE_US)
                    current_us = OPEN_PULSE_US
                    reference_established = True
                    print_pulse("X FINAL", current_us, OPEN_PULSE_US, duty_cycle)
                    continue

                if not reference_established or current_us is None:
                    print("IGNORED - PRESS X/CROSS TO ESTABLISH 1500 us FIRST", flush=True)
                    continue

                target_us = clamp_pulse(current_us + target_or_delta)
                current_us = ramp_to_target(
                    pca,
                    event_file,
                    current_us,
                    target_us,
                    label,
                    axis_active,
                    trigger_key_seen,
                )

    except EmergencyStop:
        disable_pwm(pca)
        print("EMERGENCY STOP - PWM DISABLED", flush=True)
        return 0
    except KeyboardInterrupt:
        print("Ctrl+C received; disabling PWM.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        disable_pwm(pca)
        pca.deinit()
        print(f"PWM disabled on channel {GRIPPER_CHANNEL}.", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
