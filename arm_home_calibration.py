#!/usr/bin/env python3
"""Sequential nominal-center calibration for the Hiwonder arm on PCA9685.

This script does not move anything until the operator confirms each servo.
It calibrates one new servo at a time and disables all used outputs on exit.
"""

from __future__ import annotations

import argparse
import sys
import time

import board
import busio
from adafruit_pca9685 import PCA9685


PCA9685_ADDRESS = 0x40
FREQUENCY_HZ = 50

# Full old Hiwonder arm channel map:
# 0 = gripper, 1-5 = sequential arm joints.
USED_SERVO_CHANNELS = (0, 1, 2, 3, 4, 5)

MIN_PULSE_US = 500
MAX_PULSE_US = 2500
NOMINAL_CENTER_US = 1500
RAMP_STEP_US = 5
RAMP_DELAY_SECONDS = 0.10
CENTER_HOLD_SECONDS = 2.0


def pulse_us_to_duty_cycle(pulse_us: int) -> int:
    period_us = 1_000_000 / FREQUENCY_HZ
    duty_fraction = pulse_us / period_us
    return max(0, min(0xFFFF, round(duty_fraction * 0xFFFF)))


def validate_channel(channel: int) -> None:
    if channel not in USED_SERVO_CHANNELS:
        raise ValueError(f"Refusing to access unexpected channel {channel}.")


def validate_pulse(pulse_us: int) -> None:
    if pulse_us < MIN_PULSE_US or pulse_us > MAX_PULSE_US:
        raise ValueError(
            f"Refusing pulse outside documented {MIN_PULSE_US}-{MAX_PULSE_US} us range."
        )


def disable_all_used_pwm(pca: PCA9685) -> None:
    for channel in USED_SERVO_CHANNELS:
        pca.channels[channel].duty_cycle = 0


def set_servo_pulse(pca: PCA9685, channel: int, pulse_us: int) -> int:
    validate_channel(channel)
    validate_pulse(pulse_us)
    duty_cycle = pulse_us_to_duty_cycle(pulse_us)
    pca.channels[channel].duty_cycle = duty_cycle
    return duty_cycle


def confirm_servo(channel: int) -> bool:
    print("", flush=True)
    print(f"SERVO {channel} -> NOMINAL CENTER {NOMINAL_CENTER_US} us", flush=True)
    print("WAITING FOR CONFIRMATION", flush=True)
    response = input("Type MOVE and press Enter to move this servo, or q to exit: ")
    normalized = response.strip().lower()
    if normalized in {"q", "quit", "exit"}:
        return False
    if response.strip() != "MOVE":
        print("Confirmation did not match MOVE; exiting without moving this servo.", flush=True)
        return False
    return True


def ramp_to_nominal_center(pca: PCA9685, channel: int, start_pulse_us: int) -> None:
    validate_channel(channel)
    validate_pulse(start_pulse_us)

    if start_pulse_us == NOMINAL_CENTER_US:
        duty_cycle = set_servo_pulse(pca, channel, NOMINAL_CENTER_US)
        print(
            f"channel={channel} | pulse_us={NOMINAL_CENTER_US} | duty_cycle={duty_cycle}",
            flush=True,
        )
        return

    step = RAMP_STEP_US if NOMINAL_CENTER_US > start_pulse_us else -RAMP_STEP_US
    pulse_us = start_pulse_us

    while pulse_us != NOMINAL_CENTER_US:
        duty_cycle = set_servo_pulse(pca, channel, pulse_us)
        print(f"channel={channel} | pulse_us={pulse_us} | duty_cycle={duty_cycle}", flush=True)
        time.sleep(RAMP_DELAY_SECONDS)

        remaining = NOMINAL_CENTER_US - pulse_us
        if abs(remaining) <= RAMP_STEP_US:
            pulse_us = NOMINAL_CENTER_US
        else:
            pulse_us += step

    duty_cycle = set_servo_pulse(pca, channel, NOMINAL_CENTER_US)
    print(
        f"channel={channel} | pulse_us={NOMINAL_CENTER_US} | duty_cycle={duty_cycle}",
        flush=True,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Sequentially move used Hiwonder arm servos to nominal center. "
            "Requires explicit confirmation before each servo."
        )
    )
    parser.add_argument(
        "--start-pulse-us",
        type=int,
        default=NOMINAL_CENTER_US,
        help=(
            "Commanded pulse used as the ramp starting point for each servo. "
            "Default: 1500 us. Must be within 500-2500 us."
        ),
    )
    parser.add_argument(
        "--confirm-motion-ready",
        action="store_true",
        help="Required safety flag. The script exits without it.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    validate_pulse(args.start_pulse_us)

    if not args.confirm_motion_ready:
        print("Refusing to run without --confirm-motion-ready.", file=sys.stderr)
        return 2

    i2c = busio.I2C(board.SCL, board.SDA)
    pca = PCA9685(i2c, address=PCA9685_ADDRESS)
    pca.frequency = FREQUENCY_HZ
    calibrated_channels: list[int] = []

    try:
        disable_all_used_pwm(pca)
        print(
            "STARTUP: PWM disabled on used channels "
            f"{', '.join(str(channel) for channel in USED_SERVO_CHANNELS)}.",
            flush=True,
        )
        print("No servo will move without per-servo keyboard confirmation.", flush=True)
        print(f"Nominal center target: {NOMINAL_CENTER_US} us, not confirmed safe home.", flush=True)

        for channel in USED_SERVO_CHANNELS:
            if not confirm_servo(channel):
                break

            ramp_to_nominal_center(pca, channel, args.start_pulse_us)
            calibrated_channels.append(channel)
            print(f"SERVO {channel} holding {NOMINAL_CENTER_US} us.", flush=True)
            time.sleep(CENTER_HOLD_SECONDS)

        if calibrated_channels:
            held = ", ".join(str(channel) for channel in calibrated_channels)
            print("", flush=True)
            print(f"Holding calibrated servos at {NOMINAL_CENTER_US} us: {held}", flush=True)
            input("Press Enter to exit and disable PWM on all used channels.")
        else:
            print("No servos were moved.", flush=True)

        return 0
    except KeyboardInterrupt:
        print("Ctrl+C received; disabling PWM on all used channels immediately.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            disable_all_used_pwm(pca)
            print("PWM disabled on all used channels.", flush=True)
        finally:
            pca.deinit()


if __name__ == "__main__":
    raise SystemExit(main())
