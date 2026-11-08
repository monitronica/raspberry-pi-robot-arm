#!/usr/bin/env python3
"""Basic independent test for PCA9685 channel 1."""

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
SERVO_CHANNEL = 1
INITIAL_REFERENCE_US = 1500
L2_TARGET_US = 1450
R2_TARGET_US = 1550
RAMP_DELAY_SECONDS = 0.20
DEBOUNCE_SECONDS = 0.25

EV_KEY = 0x01
EV_ABS = 0x03
KEY_PRESSED = 1
ABS_Z = 2
ABS_RZ = 5
BTN_SOUTH = 304
BTN_TL2 = 312
BTN_TR2 = 313
BTN_START = 315
BTN_MODE = 316
TRIGGER_AXIS_THRESHOLD = 32
EVENT_STRUCT = struct.Struct("llHHI")


class EmergencyStop(Exception):
    pass


def pulse_us_to_duty_cycle(pulse_us: int) -> int:
    return round((pulse_us / (1_000_000 / FREQUENCY_HZ)) * 0xFFFF)


def set_servo_pulse(pca: PCA9685, pulse_us: int) -> int:
    duty_cycle = pulse_us_to_duty_cycle(pulse_us)
    pca.channels[SERVO_CHANNEL].duty_cycle = duty_cycle
    return duty_cycle


def print_pulse(pulse_us: int, duty_cycle: int) -> None:
    timestamp = datetime.now().isoformat(timespec="seconds")
    print(f"{timestamp} | channel=1 | pulse_us={pulse_us} | duty_cycle={duty_cycle}", flush=True)


def read_event(event_file):
    data = event_file.read(EVENT_STRUCT.size)
    return EVENT_STRUCT.unpack(data) if len(data) == EVENT_STRUCT.size else None


def check_stop(event_file) -> None:
    while True:
        readable, _, _ = select.select([event_file], [], [], 0.0)
        if not readable:
            return
        event = read_event(event_file)
        if event is None:
            continue
        _, _, event_type, code, value = event
        if event_type == EV_KEY and code in (BTN_START, BTN_MODE) and value == KEY_PRESSED:
            raise EmergencyStop


def ramp_to_target(event_file, pca: PCA9685, current_us: int, target_us: int) -> int:
    direction = 1 if target_us > current_us else -1
    while current_us != target_us:
        check_stop(event_file)
        current_us += direction
        duty_cycle = set_servo_pulse(pca, current_us)
        print_pulse(current_us, duty_cycle)
        deadline = time.monotonic() + RAMP_DELAY_SECONDS
        while time.monotonic() < deadline:
            check_stop(event_file)
            time.sleep(min(0.05, deadline - time.monotonic()))
    return current_us


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Basic PlayStation test for PCA9685 channel 1.")
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
    armed = False
    key_seen = {BTN_TL2: False, BTN_TR2: False}
    axis_pressed = {ABS_Z: False, ABS_RZ: False}
    last_action = {BTN_TL2: 0.0, BTN_TR2: 0.0}

    try:
        pca.channels[SERVO_CHANNEL].duty_cycle = 0
        print("SERVO 1 BASIC TEST ACTIVE - X ARMS, L2=1450 / R2=1550", flush=True)
        print("PWM DISABLED - press X to arm", flush=True)

        with open(args.event_device, "rb", buffering=0) as event_file:
            while True:
                readable, _, _ = select.select([event_file], [], [], 0.1)
                if not readable:
                    continue
                event = read_event(event_file)
                if event is None:
                    continue
                _, _, event_type, code, value = event

                if event_type == EV_KEY:
                    if code in (BTN_START, BTN_MODE) and value == KEY_PRESSED:
                        raise EmergencyStop
                    if code == BTN_SOUTH and value == KEY_PRESSED:
                        if not armed:
                            armed = True
                            print("ARMED - PWM STILL DISABLED", flush=True)
                        continue
                    if code in key_seen:
                        key_seen[code] = True
                        if value != KEY_PRESSED or not armed:
                            continue
                        target_us = L2_TARGET_US if code == BTN_TL2 else R2_TARGET_US
                    else:
                        continue
                elif event_type == EV_ABS and code in axis_pressed:
                    key_code = BTN_TL2 if code == ABS_Z else BTN_TR2
                    if key_seen[key_code] or not armed:
                        continue
                    pressed = value >= TRIGGER_AXIS_THRESHOLD
                    if not pressed or axis_pressed[code]:
                        axis_pressed[code] = pressed
                        continue
                    axis_pressed[code] = True
                    code = key_code
                    target_us = L2_TARGET_US if code == BTN_TL2 else R2_TARGET_US
                else:
                    continue

                now = time.monotonic()
                if now - last_action[code] < DEBOUNCE_SECONDS:
                    continue
                last_action[code] = now
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
        print("PWM disabled on channel 1.", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
