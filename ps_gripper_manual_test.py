#!/usr/bin/env python3
"""Manual PlayStation-controller gripper test on PCA9685 channel 0.

Reads Linux input events directly, so python-evdev is not required.
"""

from __future__ import annotations

import argparse
import re
import select
import struct
import sys
import time
from datetime import datetime
from pathlib import Path

import board
import busio
from adafruit_pca9685 import PCA9685


PCA9685_ADDRESS = 0x40
FREQUENCY_HZ = 50
GRIPPER_CHANNEL = 0
MIN_PULSE_US = 1200
MAX_PULSE_US = 1550
START_PULSE_US = 1400
STEP_US = 5
RAMP_STEP_US = 1
RAMP_DELAY_SECONDS = 0.25
DEBOUNCE_SECONDS = 0.25
POST_STEP_HOLD_SECONDS = 1.0
POSITION_FILE = Path("/home/robota/robot-arm/gripper_last_position.txt")

EV_KEY = 0x01
EV_ABS = 0x03
KEY_RELEASED = 0
KEY_PRESSED = 1
TRIGGER_AXIS_THRESHOLD = 32

ABS_Z = 2
ABS_RZ = 5
BTN_TL2 = 312
BTN_TR2 = 313
BTN_START = 315
BTN_MODE = 316

BUTTON_NAMES = {
    BTN_TL2: "BTN_TL2",
    BTN_TR2: "BTN_TR2",
    BTN_START: "BTN_START",
    BTN_MODE: "BTN_MODE",
}

BUTTON_ACTIONS = {
    BTN_TR2: "CLOSE",
    BTN_TL2: "OPEN",
    BTN_START: "EMERGENCY_STOP",
    BTN_MODE: "EMERGENCY_STOP",
}

AXIS_ACTIONS = {
    ABS_RZ: "CLOSE",
    ABS_Z: "OPEN",
}

EVENT_STRUCT = struct.Struct("llHHI")


class EmergencyStop(Exception):
    pass


def pulse_us_to_duty_cycle(pulse_us: int) -> int:
    period_us = 1_000_000 / FREQUENCY_HZ
    duty_fraction = pulse_us / period_us
    return max(0, min(0xFFFF, round(duty_fraction * 0xFFFF)))


def clamp_pulse(pulse_us: int) -> int:
    return max(MIN_PULSE_US, min(MAX_PULSE_US, pulse_us))


def set_gripper_pulse(pca: PCA9685, pulse_us: int) -> int:
    if pulse_us < MIN_PULSE_US or pulse_us > MAX_PULSE_US:
        raise ValueError(f"Refusing pulse outside {MIN_PULSE_US}-{MAX_PULSE_US} us.")
    duty_cycle = pulse_us_to_duty_cycle(pulse_us)
    pca.channels[GRIPPER_CHANNEL].duty_cycle = duty_cycle
    return duty_cycle


def print_status(action: str, pulse_us: int, duty_cycle: int | None = None) -> None:
    timestamp = datetime.now().isoformat(timespec="seconds")
    duty_part = ""
    if duty_cycle is not None:
        duty_part = f" | calculated_duty_cycle={duty_cycle}"
    print(
        f"{timestamp} | channel={GRIPPER_CHANNEL} | action={action} | "
        f"requested_pulse_us={pulse_us}{duty_part}",
        flush=True,
    )


def print_diagnostic(action: str, pulse_us: int, duty_cycle: int, readback: int) -> None:
    timestamp = datetime.now().isoformat(timespec="seconds")
    print(
        f"{timestamp} | channel={GRIPPER_CHANNEL} | action={action} | "
        f"requested_pulse_us={pulse_us} | calculated_duty_cycle={duty_cycle} | "
        f"assigned_channel_duty_cycle={readback}",
        flush=True,
    )


def load_saved_pulse() -> int:
    try:
        pulse_us = int(POSITION_FILE.read_text().strip())
    except FileNotFoundError:
        pulse_us = START_PULSE_US
    except ValueError as exc:
        raise ValueError(f"Invalid saved pulse in {POSITION_FILE}.") from exc

    if pulse_us < MIN_PULSE_US or pulse_us > MAX_PULSE_US:
        raise ValueError(f"Saved pulse must be within {MIN_PULSE_US}-{MAX_PULSE_US} us.")
    return pulse_us


def save_pulse(pulse_us: int) -> None:
    if pulse_us < MIN_PULSE_US or pulse_us > MAX_PULSE_US:
        raise ValueError(f"Refusing to save pulse outside {MIN_PULSE_US}-{MAX_PULSE_US} us.")
    POSITION_FILE.write_text(f"{pulse_us}\n")
    print(f"CURRENT SAVED PULSE = {pulse_us} us", flush=True)


def print_ramp_step(action: str, pulse_us: int, duty_cycle: int, readback: int) -> None:
    timestamp = datetime.now().isoformat(timespec="seconds")
    print(
        f"{timestamp} | channel={GRIPPER_CHANNEL} | action={action} RAMP_STEP | "
        f"requested_pulse_us={pulse_us} | calculated_duty_cycle={duty_cycle} | "
        f"assigned_channel_duty_cycle={readback}",
        flush=True,
    )


def decode_controller_action(
    event_type: int,
    code: int,
    value: int,
    axis_active: dict[int, bool],
    trigger_key_seen: dict[int, bool],
) -> str | None:
    if event_type == EV_KEY:
        if code in trigger_key_seen:
            trigger_key_seen[code] = True
        if value != KEY_PRESSED:
            return None
        return BUTTON_ACTIONS.get(code)

    if event_type != EV_ABS or code not in AXIS_ACTIONS:
        return None

    if (
        code == ABS_Z
        and trigger_key_seen[BTN_TL2]
    ) or (
        code == ABS_RZ
        and trigger_key_seen[BTN_TR2]
    ):
        axis_active[code] = value > 0
        return None

    is_pressed = value >= TRIGGER_AXIS_THRESHOLD
    if axis_active[code] or not is_pressed:
        axis_active[code] = is_pressed
        return None
    axis_active[code] = True
    return AXIS_ACTIONS[code]


def check_emergency_stop(
    event_file,
    axis_active: dict[int, bool],
    trigger_key_seen: dict[int, bool],
) -> None:
    while True:
        readable, _, _ = select.select([event_file], [], [], 0.0)
        if not readable:
            return

        data = event_file.read(EVENT_STRUCT.size)
        if len(data) != EVENT_STRUCT.size:
            continue

        _, _, event_type, code, value = EVENT_STRUCT.unpack(data)
        action = decode_controller_action(event_type, code, value, axis_active, trigger_key_seen)
        if action == "EMERGENCY_STOP":
            raise EmergencyStop


def ramp_gripper_pulse(
    pca: PCA9685,
    event_file,
    start_pulse_us: int,
    target_pulse_us: int,
    axis_active: dict[int, bool],
    trigger_key_seen: dict[int, bool],
) -> tuple[int, int]:
    if target_pulse_us == start_pulse_us:
        duty_cycle = set_gripper_pulse(pca, target_pulse_us)
        return duty_cycle, pca.channels[GRIPPER_CHANNEL].duty_cycle

    direction = RAMP_STEP_US if target_pulse_us > start_pulse_us else -RAMP_STEP_US
    pulse_us = start_pulse_us

    while pulse_us != target_pulse_us:
        check_emergency_stop(event_file, axis_active, trigger_key_seen)
        pulse_us += direction
        duty_cycle = set_gripper_pulse(pca, pulse_us)
        readback = pca.channels[GRIPPER_CHANNEL].duty_cycle
        print_ramp_step("CLOSE" if direction > 0 else "OPEN", pulse_us, duty_cycle, readback)
        check_emergency_stop(event_file, axis_active, trigger_key_seen)
        time.sleep(RAMP_DELAY_SECONDS)

    return duty_cycle, readback


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

    if current:
        devices.append(current)
    return devices


def find_controller_event() -> str:
    for device in parse_input_devices():
        name = device.get("name", "")
        handlers = device.get("handlers", "")
        if "Wireless Controller" not in name:
            continue
        if "Motion Sensors" in name or "Touchpad" in name:
            continue
        match = re.search(r"\bevent\d+\b", handlers)
        if match:
            return f"/dev/input/{match.group(0)}"
    raise RuntimeError("Could not find PlayStation Wireless Controller event device.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Nudge the channel-0 gripper with a PlayStation controller."
    )
    parser.add_argument("--event-device", default=None)
    parser.add_argument("--start-pulse-us", type=int, default=START_PULSE_US)
    parser.add_argument("--confirm-motion-ready", action="store_true")
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    if not args.confirm_motion_ready:
        raise ValueError("Refusing to run without --confirm-motion-ready.")
    if args.start_pulse_us < MIN_PULSE_US or args.start_pulse_us > MAX_PULSE_US:
        raise ValueError(f"--start-pulse-us must be within {MIN_PULSE_US}-{MAX_PULSE_US} us.")


def main() -> int:
    args = parse_args()
    try:
        validate_args(args)
        event_device = args.event_device or find_controller_event()
    except Exception as exc:
        print(f"Safety check failed: {exc}", file=sys.stderr)
        return 2

    current_pulse_us = load_saved_pulse()
    control_enabled = False
    last_press_by_code: dict[int, float] = {}
    axis_active = {ABS_Z: False, ABS_RZ: False}
    trigger_key_seen = {BTN_TL2: False, BTN_TR2: False}

    i2c = busio.I2C(board.SCL, board.SDA)
    pca = PCA9685(i2c, address=PCA9685_ADDRESS)
    pca.frequency = FREQUENCY_HZ

    try:
        pca.channels[GRIPPER_CHANNEL].duty_cycle = 0
        print("GRIPPER MANUALLY SET CLOSED", flush=True)
        print(f"SAVED REFERENCE = {current_pulse_us} us", flush=True)
        print("WAITING FOR CONTROLLER INPUT", flush=True)
        print(
            f"Controller: {event_device} | "
            f"L2/{BUTTON_NAMES[BTN_TL2]}/ABS_Z=OPEN | "
            f"R2/{BUTTON_NAMES[BTN_TR2]}/ABS_RZ=CLOSE | "
            f"OPTIONS/{BUTTON_NAMES[BTN_START]}=EMERGENCY_STOP",
            flush=True,
        )

        with open(event_device, "rb", buffering=0) as event_file:
            while True:
                readable, _, _ = select.select([event_file], [], [], 0.1)
                if not readable:
                    continue

                data = event_file.read(EVENT_STRUCT.size)
                if len(data) != EVENT_STRUCT.size:
                    continue

                _, _, event_type, code, value = EVENT_STRUCT.unpack(data)
                action = decode_controller_action(
                    event_type,
                    code,
                    value,
                    axis_active,
                    trigger_key_seen,
                )
                if action is None:
                    continue

                now = time.monotonic()
                if now - last_press_by_code.get(code, 0.0) < DEBOUNCE_SECONDS:
                    continue
                last_press_by_code[code] = now

                if action == "EMERGENCY_STOP":
                    pca.channels[GRIPPER_CHANNEL].duty_cycle = 0
                    print_status("EMERGENCY STOP", current_pulse_us)
                    return 0

                if not control_enabled:
                    control_enabled = True
                    print_status("CONTROL ENABLED", current_pulse_us)

                target_pulse_us = current_pulse_us
                if action == "CLOSE":
                    print("R2 CLOSE COMMAND", flush=True)
                    target_pulse_us = clamp_pulse(current_pulse_us + STEP_US)
                elif action == "OPEN":
                    print("L2 OPEN COMMAND", flush=True)
                    target_pulse_us = clamp_pulse(current_pulse_us - STEP_US)

                duty_cycle, readback = ramp_gripper_pulse(
                    pca,
                    event_file,
                    current_pulse_us,
                    target_pulse_us,
                    axis_active,
                    trigger_key_seen,
                )
                current_pulse_us = target_pulse_us
                print_diagnostic(action, current_pulse_us, duty_cycle, readback)
                save_pulse(current_pulse_us)
                time.sleep(POST_STEP_HOLD_SECONDS)

    except EmergencyStop:
        pca.channels[GRIPPER_CHANNEL].duty_cycle = 0
        print_status("EMERGENCY STOP", current_pulse_us)
        return 0
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
        print("CONTROLLER STOPPED - PWM DISABLED", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
