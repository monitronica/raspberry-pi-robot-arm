#!/usr/bin/env python3
"""Read-only PS4 Bluetooth event monitor.

This script never accesses PCA9685 and never sends servo commands.
"""

from __future__ import annotations

import re
import select
import signal
import struct
import subprocess
import time
from datetime import datetime
from pathlib import Path

try:
    from evdev import ecodes
except ImportError:  # pragma: no cover - evdev is expected on the Pi.
    ecodes = None


EV_KEY = 0x01
EV_ABS = 0x03
EV_SYN = 0x00
EVENT_STRUCT = struct.Struct("llHHI")

FALLBACK_EVENT_NAMES = {
    EV_SYN: "EV_SYN",
    EV_KEY: "EV_KEY",
    EV_ABS: "EV_ABS",
}

FALLBACK_CODE_NAMES = {
    EV_SYN: {
        0: "SYN_REPORT",
    },
    EV_ABS: {
        0: "ABS_X",
        1: "ABS_Y",
        2: "ABS_Z",
        3: "ABS_RX",
        4: "ABS_RY",
        5: "ABS_RZ",
    },
    EV_KEY: {
        310: "BTN_TL",
        311: "BTN_TR",
        312: "BTN_TL2",
        313: "BTN_TR2",
        315: "BTN_START",
        316: "BTN_MODE",
    },
}

RUNNING = True


def timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def print_line(message: str) -> None:
    print(f"{timestamp()} {message}", flush=True)


def symbolic_event_type(event_type: int) -> str:
    if ecodes is not None:
        name = ecodes.EV.get(event_type)
        if name:
            return name
    return FALLBACK_EVENT_NAMES.get(event_type, "UNKNOWN")


def symbolic_event_code(event_type: int, code: int) -> str:
    if ecodes is not None:
        name = ecodes.bytype.get(event_type, {}).get(code)
        if name:
            if isinstance(name, list):
                return "|".join(name)
            return str(name)
    return FALLBACK_CODE_NAMES.get(event_type, {}).get(code, "UNKNOWN")


def parse_input_devices() -> list[dict[str, str]]:
    devices: list[dict[str, str]] = []
    current: dict[str, str] = {}

    for line in Path("/proc/bus/input/devices").read_text().splitlines():
        if not line.strip():
            if current:
                devices.append(current)
                current = {}
            continue
        if line.startswith("N: Name="):
            current["name"] = line.split("=", 1)[1].strip().strip('"')
        elif line.startswith("H: Handlers="):
            current["handlers"] = line.split("=", 1)[1].strip()
        elif line.startswith("U: Uniq="):
            current["uniq"] = line.split("=", 1)[1].strip()

    if current:
        devices.append(current)
    return devices


def find_controller_event() -> tuple[str, str, str | None]:
    for device in parse_input_devices():
        name = device.get("name", "")
        handlers = device.get("handlers", "")
        if "Wireless Controller" not in name:
            continue
        if "Motion Sensors" in name or "Touchpad" in name:
            continue
        match = re.search(r"\bevent\d+\b", handlers)
        if match:
            return f"/dev/input/{match.group(0)}", name, device.get("uniq") or None
    raise RuntimeError("Could not find PlayStation Wireless Controller event device.")


def read_event(event_file):
    data = event_file.read(EVENT_STRUCT.size)
    return EVENT_STRUCT.unpack(data) if len(data) == EVENT_STRUCT.size else None


def normalize_address(address: str | None) -> str | None:
    if not address:
        return None
    address = address.strip().upper()
    if re.fullmatch(r"([0-9A-F]{2}:){5}[0-9A-F]{2}", address):
        return address
    return None


def bluetoothctl_info(address: str | None) -> dict[str, str]:
    result = {
        "connected": "unavailable",
        "address": address or "unavailable",
        "name": "unavailable",
        "rssi": "unavailable",
        "battery": "unavailable",
    }
    if not address:
        return result

    try:
        completed = subprocess.run(
            ["bluetoothctl", "info", address],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception:
        return result

    for line in completed.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("Name:"):
            result["name"] = stripped.split(":", 1)[1].strip() or "unavailable"
        elif stripped.startswith("Connected:"):
            result["connected"] = stripped.split(":", 1)[1].strip() or "unavailable"
        elif stripped.startswith("RSSI:"):
            result["rssi"] = stripped.split(":", 1)[1].strip() or "unavailable"
        elif stripped.startswith("Battery Percentage:"):
            result["battery"] = stripped.split(":", 1)[1].strip() or "unavailable"
    return result


def sysfs_battery_for_controller(name: str) -> str:
    power_root = Path("/sys/class/power_supply")
    if not power_root.exists():
        return "unavailable"
    for supply in power_root.iterdir():
        try:
            supply_name = (supply / "model_name").read_text().strip()
        except OSError:
            try:
                supply_name = (supply / "manufacturer").read_text().strip()
            except OSError:
                supply_name = supply.name
        if "Wireless Controller" not in supply_name and "sony" not in supply_name.lower():
            continue
        for capacity_file in ("capacity", "charge_now"):
            path = supply / capacity_file
            if path.exists():
                try:
                    return path.read_text().strip()
                except OSError:
                    pass
    return "unavailable"


def print_bluetooth_status(address: str | None, controller_name: str) -> None:
    status = bluetoothctl_info(address)
    sysfs_battery = sysfs_battery_for_controller(controller_name)
    if status["battery"] == "unavailable" and sysfs_battery != "unavailable":
        status["battery"] = sysfs_battery
    print_line(
        "BLUETOOTH connected={connected} address={address} name={name} "
        "rssi={rssi} battery={battery}".format(**status)
    )


def handle_signal(signum, frame) -> None:  # noqa: ARG001
    global RUNNING
    RUNNING = False


def main() -> int:
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    event_device, controller_name, address = find_controller_event()
    address = normalize_address(address)
    print_line(
        f"MONITOR_START device={event_device} name={controller_name} address={address or 'unavailable'}"
    )
    print_line("EVENT STREAM ACTIVE")
    print_bluetooth_status(address, controller_name)

    total_events = 0
    events_this_second = 0
    last_event_monotonic: float | None = None
    next_heartbeat = time.monotonic() + 1.0
    next_bluetooth_status = time.monotonic() + 5.0

    with open(event_device, "rb", buffering=0) as event_file:
        while RUNNING:
            now = time.monotonic()
            timeout = max(0.0, min(next_heartbeat, next_bluetooth_status) - now)
            readable, _, _ = select.select([event_file], [], [], timeout)
            if readable:
                event = read_event(event_file)
                if event is not None:
                    _, _, event_type, code, value = event
                    total_events += 1
                    events_this_second += 1
                    last_event_monotonic = time.monotonic()
                    print_line(
                        "EVENT type={event_type} type_name={type_name} code={code} "
                        "code_name={code_name} value={value}".format(
                            event_type=event_type,
                            type_name=symbolic_event_type(event_type),
                            code=code,
                            code_name=symbolic_event_code(event_type, code),
                            value=value,
                        )
                    )

            now = time.monotonic()
            if now >= next_heartbeat:
                age = None if last_event_monotonic is None else now - last_event_monotonic
                age_text = "none" if age is None else f"{age:.3f}"
                print_line(
                    "HEARTBEAT device={device} total_events={total} events_last_1s={last} "
                    "age_of_last_event={age}".format(
                        device=event_device,
                        total=total_events,
                        last=events_this_second,
                        age=age_text,
                    )
                )
                if age is None or age >= 10.0:
                    silence = "no events yet" if age is None else f"{age:.1f} seconds"
                    print_line(f"NO EVDEV EVENTS FOR {silence}")
                else:
                    print_line("EVENT STREAM ACTIVE")
                events_this_second = 0
                next_heartbeat = now + 1.0

            if now >= next_bluetooth_status:
                print_bluetooth_status(address, controller_name)
                next_bluetooth_status = now + 5.0

    print_line(f"MONITOR_STOP device={event_device} total_events={total_events}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
