#!/usr/bin/env python3
"""Controlled channel-0 gripper pulse-response test for the PCA9685.

This program commands a standard 3-wire PWM servo through the PCA9685. It
reports commanded pulse widths only; it does not read measured servo position.
"""

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
GRIPPER_CHANNEL = 0
MIN_PULSE_US = 1150
MAX_PULSE_US = 1300
DEFAULT_HOLD_SECONDS = 0.5
# LFD-06 nominal spec is 500-2500 us at a 20 ms period / 50 Hz.
# These calibration limits are intentionally narrower for safety.
# WARNING: Higher pulse values previously pushed against the open mechanical
# limit. The gripper is currently partially closed, so this test continues
# decreasing from the previous endpoint without jumping back high first.
PULSE_SEQUENCE = tuple((pulse_us, "DECREASING") for pulse_us in range(1300, 1149, -10))


def pulse_us_to_duty_cycle(pulse_us: int) -> int:
    """Convert pulse width in microseconds to a PCA9685 16-bit duty cycle."""
    period_us = 1_000_000 / FREQUENCY_HZ
    duty_fraction = pulse_us / period_us
    return max(0, min(0xFFFF, round(duty_fraction * 0xFFFF)))


def commanded_relative_percent(pulse_us: int) -> float:
    """Report relative command within this test range, not confirmed gripper state."""
    span = MAX_PULSE_US - MIN_PULSE_US
    return ((pulse_us - MIN_PULSE_US) / span) * 100.0


def set_gripper_pulse(pca: PCA9685, pulse_us: int) -> None:
    if pulse_us < MIN_PULSE_US or pulse_us > MAX_PULSE_US:
        raise ValueError(f"Refusing pulse outside {MIN_PULSE_US}-{MAX_PULSE_US} us.")
    pca.channels[GRIPPER_CHANNEL].duty_cycle = pulse_us_to_duty_cycle(pulse_us)


def monitor_line(cycle: int, pulse_us: int, direction: str) -> str:
    timestamp = datetime.now().isoformat(timespec="seconds")
    relative_percent = commanded_relative_percent(pulse_us)
    return (
        f"{timestamp} | step={cycle} | channel={GRIPPER_CHANNEL} | "
        f"COMMANDED POSITION pulse_us={pulse_us} | direction={direction} | "
        f"relative_command_percent={relative_percent:.0f}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one channel-0 slow decreasing ramp from 1300 to 1150 us."
    )
    parser.add_argument("--hold-seconds", type=float, default=DEFAULT_HOLD_SECONDS)
    parser.add_argument(
        "--confirm-motion-ready",
        action="store_true",
        help="Required safety acknowledgement before any PWM output is commanded.",
    )
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    if not args.confirm_motion_ready:
        raise ValueError("Refusing to run without --confirm-motion-ready.")
    if args.hold_seconds < 0.5:
        raise ValueError("--hold-seconds must be at least 0.5.")


def main() -> int:
    args = parse_args()

    try:
        validate_args(args)
    except ValueError as exc:
        print(f"Safety check failed: {exc}", file=sys.stderr)
        return 2

    i2c = busio.I2C(board.SCL, board.SDA)
    pca = PCA9685(i2c, address=PCA9685_ADDRESS)
    pca.frequency = FREQUENCY_HZ

    try:
        for step, (pulse_us, direction) in enumerate(PULSE_SEQUENCE, start=1):
            print(monitor_line(step, pulse_us, direction), flush=True)
            set_gripper_pulse(pca, pulse_us)
            time.sleep(args.hold_seconds)
    except KeyboardInterrupt:
        print("Ctrl+C received; disabling PWM and exiting.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        pca.channels[GRIPPER_CHANNEL].duty_cycle = 0
        pca.deinit()
        print(f"PWM disabled on channel {GRIPPER_CHANNEL}.", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
