#!/usr/bin/env python3
"""Small, limited PCA9685 single-servo motion test.

Do not run this until:
- the servo is connected to the configured PCA9685 channel,
- the servo has suitable external power,
- the arm joint is mechanically safe to move,
- and the operator has explicitly approved motion testing.
"""

from __future__ import annotations

import argparse
import sys
import time

import board
import busio
from adafruit_pca9685 import PCA9685


FREQUENCY_HZ = 50
DEFAULT_CHANNEL = 0
DEFAULT_ADDRESS = 0x40
DEFAULT_NEUTRAL_US = 1500
DEFAULT_MIN_US = 1450
DEFAULT_MAX_US = 1550
DEFAULT_WAIT_SECONDS = 2.0


def pulse_us_to_duty_cycle(pulse_us: int, frequency_hz: int) -> int:
    """Convert a pulse width in microseconds to PCA9685 16-bit duty cycle."""
    period_us = 1_000_000 / frequency_hz
    duty_fraction = pulse_us / period_us
    return max(0, min(0xFFFF, round(duty_fraction * 0xFFFF)))


def set_pulse_us(pca: PCA9685, channel: int, pulse_us: int) -> None:
    pca.channels[channel].duty_cycle = pulse_us_to_duty_cycle(pulse_us, FREQUENCY_HZ)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Limited single-servo PCA9685 test. Never sweeps full range."
    )
    parser.add_argument("--channel", type=int, default=DEFAULT_CHANNEL)
    parser.add_argument("--address", type=lambda value: int(value, 0), default=DEFAULT_ADDRESS)
    parser.add_argument("--neutral-us", type=int, default=DEFAULT_NEUTRAL_US)
    parser.add_argument("--min-us", type=int, default=DEFAULT_MIN_US)
    parser.add_argument("--max-us", type=int, default=DEFAULT_MAX_US)
    parser.add_argument("--wait-seconds", type=float, default=DEFAULT_WAIT_SECONDS)
    parser.add_argument(
        "--pulse-us",
        type=int,
        action="append",
        help="Explicit pulse sequence. May be repeated. Still constrained to min/max.",
    )
    parser.add_argument(
        "--hold-seconds",
        type=float,
        action="append",
        help="Hold time for each --pulse-us value. May be repeated.",
    )
    parser.add_argument(
        "--confirm-motion-ready",
        action="store_true",
        help="Required safety acknowledgement before any PWM output is commanded.",
    )
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    if not args.confirm_motion_ready:
        raise ValueError("Refusing to run without --confirm-motion-ready.")
    if not 0 <= args.channel <= 15:
        raise ValueError("--channel must be between 0 and 15.")
    if args.min_us < 1400 or args.max_us > 1600:
        raise ValueError("Initial test range must stay within 1400-1600 us.")
    if args.max_us - args.min_us > 100:
        raise ValueError("Initial test range must be no wider than 100 us.")
    if not args.min_us <= args.neutral_us <= args.max_us:
        raise ValueError("--neutral-us must be between --min-us and --max-us.")
    if args.wait_seconds < 1.0:
        raise ValueError("--wait-seconds must be at least 1.0.")
    if args.pulse_us:
        for pulse_us in args.pulse_us:
            if pulse_us < args.min_us or pulse_us > args.max_us:
                raise ValueError("--pulse-us values must stay within --min-us and --max-us.")
    if args.hold_seconds:
        if not args.pulse_us:
            raise ValueError("--hold-seconds requires --pulse-us.")
        if len(args.hold_seconds) != len(args.pulse_us):
            raise ValueError("--hold-seconds count must match --pulse-us count.")
        for hold_seconds in args.hold_seconds:
            if hold_seconds < 1.0:
                raise ValueError("--hold-seconds values must be at least 1.0.")


def main() -> int:
    args = parse_args()

    try:
        validate_args(args)
    except ValueError as exc:
        print(f"Safety check failed: {exc}", file=sys.stderr)
        return 2

    i2c = busio.I2C(board.SCL, board.SDA)
    pca = PCA9685(i2c, address=args.address)
    pca.frequency = FREQUENCY_HZ

    if args.pulse_us:
        pulses = args.pulse_us
        holds = args.hold_seconds or [args.wait_seconds] * len(pulses)
    else:
        # Keep this first test deliberately tiny: neutral, slightly low, neutral,
        # slightly high, neutral. No full-range sweep.
        pulses = [
            args.neutral_us,
            args.min_us,
            args.neutral_us,
            args.max_us,
            args.neutral_us,
        ]
        holds = [args.wait_seconds] * len(pulses)

    try:
        for pulse_us, hold_seconds in zip(pulses, holds):
            print(f"Channel {args.channel}: {pulse_us} us")
            set_pulse_us(pca, args.channel, pulse_us)
            time.sleep(hold_seconds)
    finally:
        print(f"Disabling PWM output on channel {args.channel}")
        pca.channels[args.channel].duty_cycle = 0
        pca.deinit()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
