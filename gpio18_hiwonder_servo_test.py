#!/usr/bin/env python3
"""Drive one Hiwonder servo signal directly from Raspberry Pi GPIO18."""

from __future__ import annotations

import argparse
import sys
import time

import lgpio


GPIO_CHIP = 0
GPIO = 18
FREQUENCY_HZ = 50
HOLD_SECONDS = 3.0
ALLOWED_PULSE_US = {500, 1500, 2500}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Send exactly one Hiwonder servo pulse-width command on GPIO18 "
            "for 2 seconds, then stop PWM and release the GPIO."
        )
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
    handle: int | None = None
    claimed = False

    print(f"requested pulse_us: {args.pulse_us}")
    print(f"GPIO used: GPIO{GPIO} / physical pin 12")
    print(f"frequency: {FREQUENCY_HZ} Hz")
    print(f"hold duration: {HOLD_SECONDS:g} seconds")

    try:
        handle = lgpio.gpiochip_open(GPIO_CHIP)
        lgpio.gpio_claim_output(handle, GPIO, 0)
        claimed = True

        lgpio.tx_servo(handle, GPIO, args.pulse_us, FREQUENCY_HZ)
        time.sleep(HOLD_SECONDS)
        return 0
    except KeyboardInterrupt:
        print("Interrupted; stopping PWM and releasing GPIO18.", file=sys.stderr)
        return 130
    finally:
        if handle is not None:
            if claimed:
                try:
                    lgpio.tx_servo(handle, GPIO, 0)
                except Exception as exc:  # pragma: no cover - hardware cleanup path
                    print(f"Warning: failed to stop PWM: {exc}", file=sys.stderr)
                try:
                    lgpio.gpio_free(handle, GPIO)
                except Exception as exc:  # pragma: no cover - hardware cleanup path
                    print(f"Warning: failed to free GPIO18: {exc}", file=sys.stderr)
            try:
                lgpio.gpiochip_close(handle)
            except Exception as exc:  # pragma: no cover - hardware cleanup path
                print(f"Warning: failed to close GPIO chip: {exc}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
