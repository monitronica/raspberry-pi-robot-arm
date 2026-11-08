#!/usr/bin/env python3
"""Temporary, isolated R2-only test for PCA9685 channel 0."""

from __future__ import annotations

import argparse
import select
import struct
import sys
import time

import board
import busio
from adafruit_pca9685 import PCA9685


PCA9685_ADDRESS = 0x40
FREQUENCY_HZ = 50
SERVO_CHANNEL = 0
REFERENCE_PULSE_US = 1500
TARGET_PULSE_US = 1505
RAMP_STEP_US = 1
RAMP_DELAY_SECONDS = 0.30
DEBOUNCE_SECONDS = 0.25

EV_KEY = 0x01
EV_ABS = 0x03
KEY_PRESSED = 1
ABS_RZ = 5
BTN_SOUTH = 304
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


def print_pulse(label: str, pulse_us: int, duty_cycle: int | None = None) -> None:
    suffix = "" if duty_cycle is None else f" | duty_cycle={duty_cycle}"
    print(f"{label} | channel=0 | pulse_us={pulse_us}{suffix}", flush=True)


def read_event(event_file):
    data = event_file.read(EVENT_STRUCT.size)
    if len(data) != EVENT_STRUCT.size:
        return None
    _, _, event_type, code, value = EVENT_STRUCT.unpack(data)
    return event_type, code, value


def check_stop(event_file, axis_pressed: bool) -> bool:
    while True:
        readable, _, _ = select.select([event_file], [], [], 0.0)
        if not readable:
            return axis_pressed
        event = read_event(event_file)
        if event is None:
            continue
        event_type, code, value = event
        if event_type == EV_KEY and code in (BTN_START, BTN_MODE) and value == KEY_PRESSED:
            raise EmergencyStop
        if event_type == EV_ABS and code == ABS_RZ:
            axis_pressed = value >= TRIGGER_AXIS_THRESHOLD
    

def ramp_r2(event_file, pca: PCA9685, axis_pressed: bool) -> None:
    pulse_us = REFERENCE_PULSE_US
    duty_cycle = set_servo_pulse(pca, pulse_us)
    print_pulse("R2 ONLY TEST", pulse_us, duty_cycle)
    while pulse_us < TARGET_PULSE_US:
        axis_pressed = check_stop(event_file, axis_pressed)
        pulse_us += RAMP_STEP_US
        duty_cycle = set_servo_pulse(pca, pulse_us)
        print_pulse("R2 ONLY TEST", pulse_us, duty_cycle)
        deadline = time.monotonic() + RAMP_DELAY_SECONDS
        while time.monotonic() < deadline:
            axis_pressed = check_stop(event_file, axis_pressed)
            time.sleep(min(0.05, deadline - time.monotonic()))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Temporary isolated R2-only servo test.")
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
    armed = False
    r2_triggered = False

    try:
        pca.channels[SERVO_CHANNEL].duty_cycle = 0
        print("R2-ONLY SERVO TEST ACTIVE", flush=True)
        print("X/CROSS=ARM ONLY | R2=1500 to 1505 us | L2=IGNORED | OPTIONS/START or PS/MODE=STOP", flush=True)

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
                    if code == BTN_SOUTH and value == KEY_PRESSED:
                        if not armed:
                            armed = True
                            print("ARMED", flush=True)
                        continue
                    if code != BTN_TR2 or value != KEY_PRESSED or not armed:
                        continue
                    if r2_triggered:
                        continue
                    r2_triggered = True
                    print("R2 ONLY TEST", flush=True)
                    ramp_r2(event_file, pca, False)
                    continue

                if event_type == EV_ABS and code == ABS_RZ:
                    pressed = value >= TRIGGER_AXIS_THRESHOLD
                    if not pressed:
                        r2_triggered = False
                    elif pressed and armed and not r2_triggered:
                        r2_triggered = True
                        print("R2 ONLY TEST", flush=True)
                        ramp_r2(event_file, pca, True)

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
