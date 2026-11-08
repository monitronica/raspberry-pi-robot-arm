#!/usr/bin/env python3
"""Read-only PCA9685 timing diagnostic.

This script reads configuration registers and calculates pulse timing. It does
not instantiate the Adafruit PCA9685 driver, set frequency, write PWM values, or
access individual servo output registers.
"""

from __future__ import annotations

import argparse

import board
import busio
from adafruit_bus_device.i2c_device import I2CDevice


PCA9685_ADDRESS = 0x40
ASSUMED_OSCILLATOR_HZ = 25_000_000
FREQUENCY_HZ = 50

MODE1 = 0x00
MODE2 = 0x01
PRE_SCALE = 0xFE


def read_register(device: I2CDevice, register: int) -> int:
    write_buffer = bytearray([register])
    read_buffer = bytearray(1)
    device.write_then_readinto(write_buffer, read_buffer)
    return read_buffer[0]


def requested_duty_cycle(pulse_us: int, requested_frequency_hz: float) -> int:
    requested_period_us = 1_000_000 / requested_frequency_hz
    duty_fraction = pulse_us / requested_period_us
    return max(0, min(0xFFFF, round(duty_fraction * 0xFFFF)))


def actual_frequency_hz(prescale: int, oscillator_hz: float) -> float:
    return oscillator_hz / (4096 * (prescale + 1))


def actual_pulse_us(counts: int, frequency_hz: float) -> float:
    return (counts / 4096) * (1_000_000 / frequency_hz)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only PCA9685 timing diagnostic.")
    parser.add_argument("--address", type=lambda value: int(value, 0), default=PCA9685_ADDRESS)
    parser.add_argument("--oscillator-hz", type=float, default=ASSUMED_OSCILLATOR_HZ)
    parser.add_argument("--frequency-hz", type=float, default=FREQUENCY_HZ)
    parser.add_argument("--pulse-us", type=int, nargs="+", default=[1000, 1500, 2000])
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    i2c = busio.I2C(board.SCL, board.SDA)

    with I2CDevice(i2c, args.address) as device:
        mode1 = read_register(device, MODE1)
        mode2 = read_register(device, MODE2)
        prescale = read_register(device, PRE_SCALE)

    calculated_frequency_hz = actual_frequency_hz(prescale, args.oscillator_hz)

    print(f"address=0x{args.address:02X}")
    print(f"mode1=0x{mode1:02X} ({mode1:08b})")
    print(f"mode2=0x{mode2:02X} ({mode2:08b})")
    print(f"prescale=0x{prescale:02X} ({prescale})")
    print(f"assumed_oscillator_hz={args.oscillator_hz:.0f}")
    print(f"requested_frequency_hz={args.frequency_hz:.6f}")
    print(f"calculated_actual_frequency_hz={calculated_frequency_hz:.6f}")

    for pulse_us in args.pulse_us:
        duty_cycle = requested_duty_cycle(pulse_us, args.frequency_hz)
        counts = duty_cycle >> 4
        calculated_pulse_us = actual_pulse_us(counts, calculated_frequency_hz)
        print(
            "pulse "
            f"requested_pulse_us={pulse_us} "
            f"requested_16bit_duty={duty_cycle} "
            f"requested_pca9685_counts={counts} "
            f"calculated_actual_pulse_us={calculated_pulse_us:.3f}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
