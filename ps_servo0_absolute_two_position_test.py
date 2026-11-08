#!/usr/bin/env python3
"""Absolute two-position controller test for PCA9685 channel 0."""

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
INITIAL_REFERENCE_US = 1500
L2_TARGET_US = 1200
R2_TARGET_US = 1800
RAMP_STEP_US = 5
RAMP_DELAY_SECONDS = 0.10
DEBOUNCE_SECONDS = 0.25

EV_KEY = 0x01
EV_ABS = 0x03
KEY_PRESSED = 1
ABS_Z = 2
ABS_RZ = 5
BTN_TL2 = 312
BTN_TR2 = 313
BTN_START = 315
BTN_MODE = 316
TRIGGER_AXIS_THRESHOLD = 32
EVENT_STRUCT = struct.Struct("llHHI")


class EmergencyStop(Exception):
    pass


def pulse_us_to_duty_cycle(pulse_us: int) -> int:
    period_us = 1_000_000 / FREQUENCY_HZ
    return round((pulse_us / period_us) * 0xFFFF)


def set_servo_pulse(pca: PCA9685, pulse_us: int) -> int:
    duty_cycle = pulse_us_to_duty_cycle(pulse_us)
    pca.channels[SERVO_CHANNEL].duty_cycle = duty_cycle
    return duty_cycle


def print_pulse(pulse_us: int, duty_cycle: int) -> None:
    timestamp = datetime.now().isoformat(timespec="seconds")
    print(
        f"{timestamp} | channel=0 | pulse_us={pulse_us} | duty_cycle={duty_cycle}",
        flush=True,
    )


def read_event(event_file):
    data = event_file.read(EVENT_STRUCT.size)
    if len(data) != EVENT_STRUCT.size:
        return None
    _, _, event_type, code, value = EVENT_STRUCT.unpack(data)
    return event_type, code, value


def check_emergency_stop(event_file) -> None:
    while True:
        readable, _, _ = select.select([event_file], [], [], 0.0)
        if not readable:
            return
        event = read_event(event_file)
        if event is None:
            continue
        event_type, code, value = event
        if event_type == EV_KEY and code in (BTN_START, BTN_MODE) and value == KEY_PRESSED:
            raise EmergencyStop


def ramp_to_target(event_file, pca: PCA9685, current_us: int, target_us: int) -> int:
    direction = RAMP_STEP_US if target_us > current_us else -RAMP_STEP_US
    while current_us != target_us:
        check_emergency_stop(event_file)
        current_us = min(target_us, current_us + direction) if direction > 0 else max(target_us, current_us + direction)
        duty_cycle = set_servo_pulse(pca, current_us)
        print_pulse(current_us, duty_cycle)
        deadline = time.monotonic() + RAMP_DELAY_SECONDS
        while time.monotonic() < deadline:
            check_emergency_stop(event_file)
            time.sleep(min(0.05, deadline - time.monotonic()))
    return current_us


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Absolute L2/R2 two-position test for servo 0.")
    parser.add_argument("--event-device", required=True)
    parser.add_argument("--confirm-motion-ready", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.confirm_motion_ready:
        print("Refusing to run without --confirm-motion-ready.", file=sys.stderr)
        return 2

    i2c = busio.I2C(board.SCL, board.SDA)
    pca = PCA9685(i2c, address=PCA9685_ADDRESS)
    pca.frequency = FREQUENCY_HZ
    current_us = INITIAL_REFERENCE_US
    trigger_key_seen = {BTN_TL2: False, BTN_TR2: False}
    axis_pressed = {ABS_Z: False, ABS_RZ: False}
    last_action_time = {BTN_TL2: 0.0, BTN_TR2: 0.0}

    try:
        pca.channels[SERVO_CHANNEL].duty_cycle = 0
        print("READY - PWM DISABLED, ABSOLUTE TARGET TEST", flush=True)
        print("L2=1200 us | R2=1800 us | OPTIONS/START or PS/MODE=EMERGENCY STOP", flush=True)

        with open(args.event_device, "rb", buffering=0) as event_file:
            while True:
                readable, _, _ = select.select([event_file], [], [], 0.1)
                if not readable:
                    continue
                event = read_event(event_file)
                if event is None:
                    continue
                event_type, code, value = event

                if event_type == EV_KEY:
                    if code in (BTN_START, BTN_MODE) and value == KEY_PRESSED:
                        raise EmergencyStop
                    if code in trigger_key_seen:
                        trigger_key_seen[code] = True
                        if value != KEY_PRESSED:
                            continue
                        target_us = L2_TARGET_US if code == BTN_TL2 else R2_TARGET_US
                    else:
                        continue
                elif event_type == EV_ABS and code in axis_pressed and not trigger_key_seen[BTN_TL2 if code == ABS_Z else BTN_TR2]:
                    pressed = value >= TRIGGER_AXIS_THRESHOLD
                    if not pressed or axis_pressed[code]:
                        axis_pressed[code] = pressed
                        continue
                    axis_pressed[code] = True
                    target_us = L2_TARGET_US if code == ABS_Z else R2_TARGET_US
                    code = BTN_TL2 if code == ABS_Z else BTN_TR2
                else:
                    continue

                now = time.monotonic()
                if now - last_action_time[code] < DEBOUNCE_SECONDS:
                    continue
                last_action_time[code] = now
                current_us = ramp_to_target(event_file, pca, current_us, target_us)

    except EmergencyStop:
        print("EMERGENCY STOP", flush=True)
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
        print("PWM disabled on channel 0.", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
