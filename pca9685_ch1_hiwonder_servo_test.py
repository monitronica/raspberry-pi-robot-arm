#!/usr/bin/env python3
"""One-shot Hiwonder absolute pulse test for PCA9685 channel 1."""

from __future__ import annotations

import argparse
import sys
import time

import board
import busio
from adafruit_pca9685 import PCA9685


PCA9685_ADDRESS = 0x40
FREQUENCY_HZ = 50
SERVO_CHANNEL = 1
HOLD_SECONDS = 3.0
ALLOWED_PULSE_US = {500, 1500, 2500}


def pulse_us_to_duty_cycle(pulse_us: int) -> int:
    period_us = 1_000_000 / FREQUENCY_HZ
    duty_fraction = pulse_us / period_us
    return max(0, min(0xFFFF, round(duty_fraction * 0xFFFF)))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Command one allowed absolute pulse on PCA9685 channel 1."
    )
    parser.add_argument(
        "pulse_us",
        type=int,
        choices=sorted(ALLOWED_PULSE_US),
        help="Pulse width in microseconds; accepted values: 500, 1500, 2500.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    pca: PCA9685 | None = None

    try:
        i2c = busio.I2C(board.SCL, board.SDA)
        pca = PCA9685(i2c, address=PCA9685_ADDRESS)
        pca.frequency = FREQUENCY_HZ
        pca.channels[SERVO_CHANNEL].duty_cycle = 0

        duty_cycle = pulse_us_to_duty_cycle(args.pulse_us)
        pca.channels[SERVO_CHANNEL].duty_cycle = duty_cycle
        readback = pca.channels[SERVO_CHANNEL].duty_cycle

        print(f"requested pulse_us: {args.pulse_us}", flush=True)
        print(f"calculated duty_cycle: {duty_cycle}", flush=True)
        print(
            f"PCA9685 channel {SERVO_CHANNEL} duty_cycle readback: {readback}",
            flush=True,
        )

        time.sleep(HOLD_SECONDS)
        return 0
    except KeyboardInterrupt:
        print("Ctrl+C received; disabling PCA9685 channel 1 PWM.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        if pca is not None:
            try:
                pca.channels[SERVO_CHANNEL].duty_cycle = 0
            except Exception as exc:  # pragma: no cover - hardware cleanup path
                print(f"Warning: failed to disable PCA9685 channel 1 PWM: {exc}", file=sys.stderr)
            try:
                pca.deinit()
            except Exception as exc:  # pragma: no cover - hardware cleanup path
                print(f"Warning: failed to deinitialize PCA9685: {exc}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
