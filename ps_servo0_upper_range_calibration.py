#!/usr/bin/env python3
"""Upper-range PlayStation calibration for PCA9685 channel 0."""

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
SERVO_CHANNEL = 0
EVENT_DEVICE = "/dev/input/event5"

INITIAL_REFERENCE_US = 2000
MIN_PULSE_US = 1500
MAX_PULSE_US = 2500
TRIGGER_TARGET_STEP_US = 10
INITIAL_RAMP_STEP_US = 5
TRIGGER_RAMP_STEP_US = 1
RAMP_DELAY_SECONDS = 0.15
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
    return max(MIN_PULSE_US, min(MAX_PULSE_US, pulse_us))


def disable_pwm(pca: PCA9685) -> None:
    pca.channels[SERVO_CHANNEL].duty_cycle = 0


def set_servo_pulse(pca: PCA9685, pulse_us: int) -> int:
    if pulse_us < MIN_PULSE_US or pulse_us > MAX_PULSE_US:
        raise ValueError(f"Refusing pulse outside {MIN_PULSE_US}-{MAX_PULSE_US} us.")
    duty_cycle = pulse_us_to_duty_cycle(pulse_us)
    pca.channels[SERVO_CHANNEL].duty_cycle = duty_cycle
    return duty_cycle


def print_pulse(label: str, pulse_us: int, target_us: int | None, duty_cycle: int) -> None:
    timestamp = datetime.now().isoformat(timespec="seconds")
    target_text = "" if target_us is None else f" | target_us={target_us}"
    print(
        f"{timestamp} | channel={SERVO_CHANNEL} | {label} | "
        f"current_us={pulse_us}{target_text} | duty_cycle={duty_cycle}",
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
            return ("X", INITIAL_REFERENCE_US)
        if code == BTN_TL2:
            return ("LOWER", -TRIGGER_TARGET_STEP_US)
        if code == BTN_TR2:
            return ("HIGHER", TRIGGER_TARGET_STEP_US)
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
        return ("LOWER", -TRIGGER_TARGET_STEP_US)
    return ("HIGHER", TRIGGER_TARGET_STEP_US)


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
    current_us: int | None,
    target_us: int,
    ramp_step_us: int,
    label: str,
    axis_active: dict[int, bool],
    trigger_key_seen: dict[int, bool],
) -> int:
    if current_us is None:
        duty_cycle = set_servo_pulse(pca, target_us)
        print_pulse(label, target_us, target_us, duty_cycle)
        print_pulse(f"{label} FINAL", target_us, target_us, duty_cycle)
        return target_us

    if current_us == target_us:
        duty_cycle = pca.channels[SERVO_CHANNEL].duty_cycle
        print_pulse(f"{label} FINAL", current_us, target_us, duty_cycle)
        return current_us

    step = ramp_step_us if target_us > current_us else -ramp_step_us
    while current_us != target_us:
        check_emergency_stop(event_file, axis_active, trigger_key_seen)
        current_us += step
        if step > 0:
            current_us = min(current_us, target_us)
        else:
            current_us = max(current_us, target_us)
        duty_cycle = set_servo_pulse(pca, current_us)
        print_pulse(label, current_us, target_us, duty_cycle)
        sleep_with_emergency_check(event_file, axis_active, trigger_key_seen)

    duty_cycle = pca.channels[SERVO_CHANNEL].duty_cycle
    print_pulse(f"{label} FINAL", current_us, target_us, duty_cycle)
    return current_us


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Upper-range channel-0 calibration controller.")
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
            "X/CROSS=establish 2000 us | L2=LOWER toward 1500 us | "
            "R2=HIGHER toward 2500 us | OPTIONS/START or PS/MODE=EMERGENCY_STOP",
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
                    current_us = ramp_to_target(
                        pca,
                        event_file,
                        current_us,
                        INITIAL_REFERENCE_US,
                        INITIAL_RAMP_STEP_US,
                        label,
                        axis_active,
                        trigger_key_seen,
                    )
                    reference_established = True
                    continue

                if not reference_established or current_us is None:
                    print("IGNORED - PRESS X/CROSS TO ESTABLISH 2000 us FIRST", flush=True)
                    continue

                target_us = clamp_pulse(current_us + target_or_delta)
                current_us = ramp_to_target(
                    pca,
                    event_file,
                    current_us,
                    target_us,
                    TRIGGER_RAMP_STEP_US,
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
        print(f"PWM disabled on channel {SERVO_CHANNEL}.", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
