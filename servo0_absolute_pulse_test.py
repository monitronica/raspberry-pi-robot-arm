#!/usr/bin/env python3
"""One-shot absolute pulse diagnostic for PCA9685 channel 0."""

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
ALLOWED_PULSES_US = (1000, 1500, 2000)
HOLD_SECONDS = 1.0


def pulse_us_to_duty_cycle(pulse_us: int) -> int:
    period_us = 1_000_000 / FREQUENCY_HZ
    duty_fraction = pulse_us / period_us
    return max(0, min(0xFFFF, round(duty_fraction * 0xFFFF)))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Command one allowed absolute pulse on PCA9685 channel 0."
    )
    parser.add_argument("--pulse-us", type=int, required=True)
    parser.add_argument("--confirm-motion-ready", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.confirm_motion_ready:
        print("Refusing to run without --confirm-motion-ready.", file=sys.stderr)
        return 2
    if args.pulse_us not in ALLOWED_PULSES_US:
        allowed = ", ".join(str(pulse) for pulse in ALLOWED_PULSES_US)
        print(f"Refusing pulse {args.pulse_us}; allowed pulses are: {allowed}.", file=sys.stderr)
        return 2

    i2c = busio.I2C(board.SCL, board.SDA)
    pca = PCA9685(i2c, address=PCA9685_ADDRESS)
    pca.frequency = FREQUENCY_HZ

    try:
        duty_cycle = pulse_us_to_duty_cycle(args.pulse_us)
        pca.channels[SERVO_CHANNEL].duty_cycle = duty_cycle
        timestamp = datetime.now().isoformat(timespec="seconds")
        print(
            f"{timestamp} | channel={SERVO_CHANNEL} | "
            f"commanded_pulse_us={args.pulse_us} | duty_cycle={duty_cycle}",
            flush=True,
        )
        time.sleep(HOLD_SECONDS)
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
