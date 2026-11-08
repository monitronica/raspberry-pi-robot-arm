#!/usr/bin/env python3
"""Channel-0 upward pulse calibration sweep."""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime

import board
import busio
from adafruit_pca9685 import PCA9685


PCA9685_ADDRESS = 0x40
FREQUENCY_HZ = 50
SERVO_CHANNEL = 0

START_PULSE_US = 1500
STOP_PULSE_US = 2000
STEP_US = 10
STEP_HOLD_SECONDS = 0.5


def pulse_us_to_duty_cycle(pulse_us: int) -> int:
    period_us = 1_000_000 / FREQUENCY_HZ
    duty_fraction = pulse_us / period_us
    return max(0, min(0xFFFF, round(duty_fraction * 0xFFFF)))


def set_servo_pulse(pca: PCA9685, pulse_us: int) -> int:
    duty_cycle = pulse_us_to_duty_cycle(pulse_us)
    pca.channels[SERVO_CHANNEL].duty_cycle = duty_cycle
    return duty_cycle


def disable_pwm(pca: PCA9685) -> None:
    pca.channels[SERVO_CHANNEL].duty_cycle = 0


def print_command(pulse_us: int, duty_cycle: int) -> None:
    timestamp = datetime.now().isoformat(timespec="seconds")
    print(
        f"{timestamp} | channel={SERVO_CHANNEL} | pulse_us={pulse_us} | duty_cycle={duty_cycle}",
        flush=True,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sweep channel 0 upward from 1500 to 2000 us.")
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

    try:
        disable_pwm(pca)
        print(f"PWM disabled on startup for channel {SERVO_CHANNEL}.", flush=True)

        for pulse_us in range(START_PULSE_US, STOP_PULSE_US + 1, STEP_US):
            duty_cycle = set_servo_pulse(pca, pulse_us)
            print_command(pulse_us, duty_cycle)
            time.sleep(STEP_HOLD_SECONDS)

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
