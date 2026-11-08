#!/usr/bin/env python3
"""One-channel isolation test for PCA9685 channels 0 and 1."""

from __future__ import annotations

import argparse
import sys
import time

import board
import busio
from adafruit_pca9685 import PCA9685


PCA9685_ADDRESS = 0x40
FREQUENCY_HZ = 50
ALLOWED_CHANNELS = (0, 1)
ALLOWED_CHANNEL_PULSES = {
    0: (1500,),
    1: (500, 1500),
}
HOLD_SECONDS = 3.0


def pulse_us_to_duty_cycle(pulse_us: int) -> int:
    period_us = 1_000_000 / FREQUENCY_HZ
    duty_fraction = pulse_us / period_us
    return max(0, min(0xFFFF, round(duty_fraction * 0xFFFF)))


def disable_channel(pca: PCA9685, channel: int) -> None:
    if channel not in ALLOWED_CHANNELS:
        raise ValueError(f"Refusing to access unexpected channel {channel}.")
    pca.channels[channel].duty_cycle = 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Command one accepted isolation-test pulse on PCA9685 channel 0 or 1."
    )
    parser.add_argument(
        "channel",
        type=int,
        choices=ALLOWED_CHANNELS,
        help="PCA9685 channel to test; accepted values: 0, 1.",
    )
    parser.add_argument(
        "pulse_us",
        type=int,
        help="Pulse width in microseconds. Accepted pairs: channel 0 at 1500; channel 1 at 500 or 1500.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.pulse_us not in ALLOWED_CHANNEL_PULSES[args.channel]:
        allowed = ", ".join(str(pulse) for pulse in ALLOWED_CHANNEL_PULSES[args.channel])
        print(
            f"Refusing channel {args.channel} pulse {args.pulse_us}; "
            f"allowed pulses for this channel: {allowed}.",
            file=sys.stderr,
        )
        return 2

    pca: PCA9685 | None = None

    try:
        i2c = busio.I2C(board.SCL, board.SDA)
        pca = PCA9685(i2c, address=PCA9685_ADDRESS)
        pca.frequency = FREQUENCY_HZ

        duty_cycle = pulse_us_to_duty_cycle(args.pulse_us)
        pca.channels[args.channel].duty_cycle = duty_cycle
        readback = pca.channels[args.channel].duty_cycle

        print(f"channel: {args.channel}", flush=True)
        print(f"requested pulse_us: {args.pulse_us}", flush=True)
        print(f"calculated duty_cycle: {duty_cycle}", flush=True)
        print(f"duty_cycle readback: {readback}", flush=True)

        time.sleep(HOLD_SECONDS)
        disable_channel(pca, args.channel)
        print(f"PWM disabled on tested channel {args.channel}.", flush=True)
        return 0
    except KeyboardInterrupt:
        print(
            f"Ctrl+C received; disabling PCA9685 channel {args.channel} PWM.",
            file=sys.stderr,
        )
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        if pca is not None:
            try:
                disable_channel(pca, args.channel)
                print(f"PWM disabled on channel {args.channel}.", flush=True)
            except Exception as exc:  # pragma: no cover - hardware cleanup path
                print(
                    f"Warning: failed to disable PCA9685 channel {args.channel} PWM: {exc}",
                    file=sys.stderr,
                )
            try:
                pca.deinit()
            except Exception as exc:  # pragma: no cover - hardware cleanup path
                print(f"Warning: failed to deinitialize PCA9685: {exc}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
