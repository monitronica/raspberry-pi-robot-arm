#!/usr/bin/env python3
"""Input-only controller event probe. Does not touch PCA9685 or GPIO."""

from __future__ import annotations

import select
import struct
from datetime import datetime


EVENT_DEVICE = "/dev/input/event1"
EVENT_STRUCT = struct.Struct("llHHI")

EV_KEY = 0x01
EV_ABS = 0x03

KEY_NAMES = {
    304: "BTN_SOUTH",
    305: "BTN_EAST",
    307: "BTN_NORTH",
    308: "BTN_WEST",
    310: "BTN_TL",
    311: "BTN_TR",
    312: "BTN_TL2",
    313: "BTN_TR2",
    314: "BTN_SELECT",
    315: "BTN_START",
    316: "BTN_MODE",
    317: "BTN_THUMBL",
    318: "BTN_THUMBR",
}

ABS_NAMES = {
    2: "ABS_Z",
    5: "ABS_RZ",
}


def main() -> int:
    print("Input-only probe on /dev/input/event1", flush=True)
    print("Press L2, R2, and OPTIONS/START now. Ctrl+C exits.", flush=True)

    with open(EVENT_DEVICE, "rb", buffering=0) as event_file:
        while True:
            readable, _, _ = select.select([event_file], [], [], 0.5)
            if not readable:
                continue

            data = event_file.read(EVENT_STRUCT.size)
            if len(data) != EVENT_STRUCT.size:
                continue

            _, _, event_type, code, value = EVENT_STRUCT.unpack(data)
            if event_type == EV_KEY:
                name = KEY_NAMES.get(code, f"KEY_{code}")
            elif event_type == EV_ABS and code in ABS_NAMES:
                name = ABS_NAMES[code]
            else:
                continue

            timestamp = datetime.now().isoformat(timespec="seconds")
            print(
                f"{timestamp} | type={event_type} | code={code} | name={name} | value={value}",
                flush=True,
            )


if __name__ == "__main__":
    raise SystemExit(main())
