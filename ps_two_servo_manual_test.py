#!/usr/bin/env python3
"""Conservative two-servo PlayStation controller test for PCA9685 channels 0 and 1."""

from __future__ import annotations

import argparse
import select
import struct
import sys
import time
from dataclasses import dataclass
from datetime import datetime

import board
import busio
from adafruit_pca9685 import PCA9685


PCA9685_ADDRESS = 0x40
FREQUENCY_HZ = 50
EVENT_DEVICE = "/dev/input/event5"

SERVO0_CHANNEL = 0
SERVO1_CHANNEL = 1
CONTROLLED_CHANNELS = (SERVO0_CHANNEL, SERVO1_CHANNEL)

REFERENCE_PULSE_US = 1500
MIN_PULSE_US = 1450
MAX_PULSE_US = 1550
NUDGE_US = 5
RAMP_STEP_US = 1
RAMP_DELAY_SECONDS = 0.25
DEBOUNCE_SECONDS = 0.25

EV_KEY = 0x01
EV_ABS = 0x03
KEY_PRESSED = 1
TRIGGER_AXIS_THRESHOLD = 32

ABS_Z = 2
ABS_RZ = 5
BTN_TL = 310
BTN_TR = 311
BTN_TL2 = 312
BTN_TR2 = 313
BTN_START = 315
BTN_MODE = 316

EVENT_STRUCT = struct.Struct("llHHI")


class EmergencyStop(Exception):
    pass


@dataclass(frozen=True)
class ServoAction:
    channel: int
    direction: int
    label: str


BUTTON_ACTIONS = {
    BTN_TL2: ServoAction(SERVO0_CHANNEL, -1, "SERVO0_DIRECTION_A"),
    BTN_TR2: ServoAction(SERVO0_CHANNEL, 1, "SERVO0_DIRECTION_B"),
    BTN_TL: ServoAction(SERVO1_CHANNEL, -1, "SERVO1_DIRECTION_A"),
    BTN_TR: ServoAction(SERVO1_CHANNEL, 1, "SERVO1_DIRECTION_B"),
}

AXIS_ACTIONS = {
    ABS_Z: BUTTON_ACTIONS[BTN_TL2],
    ABS_RZ: BUTTON_ACTIONS[BTN_TR2],
}


def pulse_us_to_duty_cycle(pulse_us: int) -> int:
    period_us = 1_000_000 / FREQUENCY_HZ
    duty_fraction = pulse_us / period_us
    return max(0, min(0xFFFF, round(duty_fraction * 0xFFFF)))


def clamp_pulse(pulse_us: int) -> int:
    return max(MIN_PULSE_US, min(MAX_PULSE_US, pulse_us))


def disable_pwm(pca: PCA9685) -> None:
    for channel in CONTROLLED_CHANNELS:
        pca.channels[channel].duty_cycle = 0


def set_servo_pulse(pca: PCA9685, channel: int, pulse_us: int) -> int:
    if channel not in CONTROLLED_CHANNELS:
        raise ValueError(f"Refusing to access unexpected channel {channel}.")
    if pulse_us < MIN_PULSE_US or pulse_us > MAX_PULSE_US:
        raise ValueError(f"Refusing pulse outside {MIN_PULSE_US}-{MAX_PULSE_US} us.")

    duty_cycle = pulse_us_to_duty_cycle(pulse_us)
    pca.channels[channel].duty_cycle = duty_cycle
    return duty_cycle


def print_command(channel: int, action: str, pulse_us: int, duty_cycle: int | None = None) -> None:
    timestamp = datetime.now().isoformat(timespec="seconds")
    duty_text = "" if duty_cycle is None else f" | duty_cycle={duty_cycle}"
    print(
        f"{timestamp} | channel={channel} | action={action} | pulse_us={pulse_us}{duty_text}",
        flush=True,
    )


def read_event(event_file):
    data = event_file.read(EVENT_STRUCT.size)
    return EVENT_STRUCT.unpack(data) if len(data) == EVENT_STRUCT.size else None


def decode_action(
    event_type: int,
    code: int,
    value: int,
    axis_active: dict[int, bool],
    trigger_key_seen: dict[int, bool],
) -> ServoAction | str | None:
    if event_type == EV_KEY:
        if code in (BTN_START, BTN_MODE) and value == KEY_PRESSED:
            return "EMERGENCY_STOP"
        if code in trigger_key_seen:
            trigger_key_seen[code] = True
        if value != KEY_PRESSED:
            return None
        return BUTTON_ACTIONS.get(code)

    if event_type != EV_ABS or code not in AXIS_ACTIONS:
        return None

    key_code = BTN_TL2 if code == ABS_Z else BTN_TR2
    if trigger_key_seen[key_code]:
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

        event = read_event(event_file)
        if event is None:
            continue

        _, _, event_type, code, value = event
        action = decode_action(event_type, code, value, axis_active, trigger_key_seen)
        if action == "EMERGENCY_STOP":
            raise EmergencyStop


def sleep_with_emergency_check(
    event_file,
    axis_active: dict[int, bool],
    trigger_key_seen: dict[int, bool],
) -> None:
    deadline = time.monotonic() + RAMP_DELAY_SECONDS
    while time.monotonic() < deadline:
        check_emergency_stop(event_file, axis_active, trigger_key_seen)
        time.sleep(min(0.05, deadline - time.monotonic()))


def ramp_nudge(
    pca: PCA9685,
    event_file,
    current_pulse_us: int,
    action: ServoAction,
    axis_active: dict[int, bool],
    trigger_key_seen: dict[int, bool],
) -> int:
    target_pulse_us = clamp_pulse(current_pulse_us + (action.direction * NUDGE_US))
    if target_pulse_us == current_pulse_us:
        print_command(action.channel, f"{action.label}_CLAMPED", current_pulse_us)
        return current_pulse_us

    step = RAMP_STEP_US if target_pulse_us > current_pulse_us else -RAMP_STEP_US
    pulse_us = current_pulse_us
    while pulse_us != target_pulse_us:
        check_emergency_stop(event_file, axis_active, trigger_key_seen)
        pulse_us += step
        duty_cycle = set_servo_pulse(pca, action.channel, pulse_us)
        print_command(action.channel, action.label, pulse_us, duty_cycle)
        sleep_with_emergency_check(event_file, axis_active, trigger_key_seen)

    return pulse_us


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Conservative two-servo PlayStation controller test."
    )
    parser.add_argument("--event-device", default=EVENT_DEVICE)
    parser.add_argument("--confirm-motion-ready", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.confirm_motion_ready:
        print("Refusing to run without --confirm-motion-ready.", file=sys.stderr)
        return 2

    current_pulse_us = {
        SERVO0_CHANNEL: REFERENCE_PULSE_US,
        SERVO1_CHANNEL: REFERENCE_PULSE_US,
    }
    axis_active = {ABS_Z: False, ABS_RZ: False}
    trigger_key_seen = {BTN_TL2: False, BTN_TR2: False}
    last_press_by_code = {
        BTN_TL: 0.0,
        BTN_TR: 0.0,
        BTN_TL2: 0.0,
        BTN_TR2: 0.0,
        ABS_Z: 0.0,
        ABS_RZ: 0.0,
    }

    i2c = busio.I2C(board.SCL, board.SDA)
    pca = PCA9685(i2c, address=PCA9685_ADDRESS)
    pca.frequency = FREQUENCY_HZ

    try:
        disable_pwm(pca)
        print("READY - PWM DISABLED ON CHANNELS 0 AND 1", flush=True)
        print(
            "L2=servo0 direction A | R2=servo0 direction B | "
            "L1=servo1 direction A | R1=servo1 direction B | "
            "OPTIONS/START or PS/MODE=EMERGENCY_STOP",
            flush=True,
        )

        with open(args.event_device, "rb", buffering=0) as event_file:
            while True:
                readable, _, _ = select.select([event_file], [], [], 0.1)
                if not readable:
                    continue

                event = read_event(event_file)
                if event is None:
                    continue

                _, _, event_type, code, value = event
                action = decode_action(event_type, code, value, axis_active, trigger_key_seen)
                if action is None:
                    continue

                if action == "EMERGENCY_STOP":
                    disable_pwm(pca)
                    print("EMERGENCY STOP - PWM DISABLED ON CHANNELS 0 AND 1", flush=True)
                    return 0

                now = time.monotonic()
                if now - last_press_by_code.get(code, 0.0) < DEBOUNCE_SECONDS:
                    continue
                last_press_by_code[code] = now

                current_pulse_us[action.channel] = ramp_nudge(
                    pca,
                    event_file,
                    current_pulse_us[action.channel],
                    action,
                    axis_active,
                    trigger_key_seen,
                )

    except EmergencyStop:
        disable_pwm(pca)
        print("EMERGENCY STOP - PWM DISABLED ON CHANNELS 0 AND 1", flush=True)
        return 0
    except KeyboardInterrupt:
        print("Ctrl+C received; disabling PWM on channels 0 and 1.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        disable_pwm(pca)
        pca.deinit()
        print("PWM disabled on channels 0 and 1.", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
