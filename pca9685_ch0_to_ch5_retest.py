#!/usr/bin/env python3
"""Sequential PCA9685 retest for channels 0-5 at 1500 us only."""

from __future__ import annotations

import sys
import time

import board
import busio
from adafruit_pca9685 import PCA9685


PCA9685_ADDRESS = 0x40
FREQUENCY_HZ = 50
TEST_CHANNELS = (0, 1, 2, 3, 4, 5)
PULSE_US = 1500
HOLD_SECONDS = 3.0


def pulse_us_to_duty_cycle(pulse_us: int) -> int:
    period_us = 1_000_000 / FREQUENCY_HZ
    duty_fraction = pulse_us / period_us
    return max(0, min(0xFFFF, round(duty_fraction * 0xFFFF)))


def validate_channel(channel: int) -> None:
    if channel not in TEST_CHANNELS:
        raise ValueError(f"Refusing to access unexpected channel {channel}.")


def disable_channel(pca: PCA9685, channel: int) -> None:
    validate_channel(channel)
    pca.channels[channel].duty_cycle = 0


def disable_all_test_channels(pca: PCA9685) -> None:
    for channel in TEST_CHANNELS:
        pca.channels[channel].duty_cycle = 0


def wait_for_move(channel: int) -> bool:
    print("", flush=True)
    print(f"READY FOR CHANNEL {channel}", flush=True)
    print("Type exactly MOVE to continue", flush=True)
    response = input("> ")
    if response == "MOVE":
        return True

    print("Input was not exactly MOVE; no servo was moved.", flush=True)
    return False


def test_channel(pca: PCA9685, channel: int) -> None:
    validate_channel(channel)
    duty_cycle = pulse_us_to_duty_cycle(PULSE_US)
    pca.channels[channel].duty_cycle = duty_cycle
    readback = pca.channels[channel].duty_cycle

    print(f"channel: {channel}", flush=True)
    print(f"requested pulse_us: {PULSE_US}", flush=True)
    print(f"calculated duty_cycle: {duty_cycle}", flush=True)
    print(f"duty_cycle readback: {readback}", flush=True)

    time.sleep(HOLD_SECONDS)
    disable_channel(pca, channel)
    print(f"PWM disabled on channel {channel}.", flush=True)


def main() -> int:
    pca: PCA9685 | None = None

    try:
        i2c = busio.I2C(board.SCL, board.SDA)
        pca = PCA9685(i2c, address=PCA9685_ADDRESS)
        pca.frequency = FREQUENCY_HZ
        disable_all_test_channels(pca)

        print("No automatic movement on startup.", flush=True)
        print("Planned order: 0 -> 1 -> 2 -> 3 -> 4 -> 5", flush=True)

        for channel in TEST_CHANNELS:
            if not wait_for_move(channel):
                return 2
            test_channel(pca, channel)

        return 0
    except KeyboardInterrupt:
        print("Ctrl+C received; disabling PWM on channels 0-5.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        if pca is not None:
            try:
                disable_all_test_channels(pca)
                print("PWM disabled on channels 0-5.", flush=True)
            except Exception as exc:  # pragma: no cover - hardware cleanup path
                print(f"Warning: failed to disable PWM on channels 0-5: {exc}", file=sys.stderr)
            try:
                pca.deinit()
            except Exception as exc:  # pragma: no cover - hardware cleanup path
                print(f"Warning: failed to deinitialize PCA9685: {exc}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
