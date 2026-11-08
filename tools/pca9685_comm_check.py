#!/usr/bin/env python3
"""Non-motion PCA9685 communication diagnostics.

This script only talks to the I2C controller registers. It does not configure
servo pulse widths or start any motion sequence.
"""

from __future__ import annotations

import fcntl
import os
from dataclasses import dataclass


I2C_SLAVE = 0x0703
BUS = "/dev/i2c-1"
PCA9685_ADDR = 0x40

MODE1 = 0x00
MODE2 = 0x01
PRE_SCALE = 0xFE


@dataclass
class I2CDevice:
    bus: str
    address: int

    def __enter__(self) -> "I2CDevice":
        self.fd = os.open(self.bus, os.O_RDWR)
        fcntl.ioctl(self.fd, I2C_SLAVE, self.address)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        os.close(self.fd)

    def read_register(self, register: int) -> int:
        os.write(self.fd, bytes([register]))
        return os.read(self.fd, 1)[0]

    def write_register(self, register: int, value: int) -> None:
        os.write(self.fd, bytes([register, value & 0xFF]))


def probe_address(address: int) -> bool:
    try:
        with I2CDevice(BUS, address) as dev:
            dev.read_register(MODE1)
        return True
    except OSError:
        return False


def format_i2cdetect_style(found: set[int]) -> str:
    lines = ["     " + " ".join(f"{i:x}" for i in range(16))]
    for row in range(0x00, 0x80, 0x10):
        cells = []
        for low in range(16):
            address = row + low
            if address < 0x03 or address > 0x77:
                cells.append("  ")
            elif address in found:
                cells.append(f"{address:02x}")
            else:
                cells.append("--")
        lines.append(f"{row:02x}: " + " ".join(cells))
    return "\n".join(lines)


def main() -> int:
    found = {address for address in range(0x03, 0x78) if probe_address(address)}

    print("I2C scan, bus 1:")
    print(format_i2cdetect_style(found))
    print()
    print("Detected addresses:", " ".join(f"0x{x:02x}" for x in sorted(found)) or "none")

    if PCA9685_ADDR not in found:
        print("PCA9685 at 0x40: NOT FOUND")
        return 2

    with I2CDevice(BUS, PCA9685_ADDR) as dev:
        mode1_before = dev.read_register(MODE1)
        mode2_before = dev.read_register(MODE2)
        prescale_before = dev.read_register(PRE_SCALE)

        # Safe write check: write the exact current MODE1 value back unchanged.
        dev.write_register(MODE1, mode1_before)
        mode1_after = dev.read_register(MODE1)

    print("PCA9685 at 0x40: FOUND")
    print(f"MODE1 before: 0x{mode1_before:02x}")
    print(f"MODE1 after same-value write: 0x{mode1_after:02x}")
    print(f"MODE2: 0x{mode2_before:02x}")
    print(f"PRE_SCALE: 0x{prescale_before:02x}")

    if mode1_after != mode1_before:
        print("Communication check: FAILED - MODE1 changed after same-value write")
        return 3

    print("Communication check: PASSED - reads and same-value write are normal")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
