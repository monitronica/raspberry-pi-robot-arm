#!/usr/bin/env python3
"""Clean PS4 manual controller for Hiwonder servos on PCA9685 channels 0-5."""

from __future__ import annotations

import argparse
import re
import select
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import board
import busio
from adafruit_pca9685 import PCA9685

try:
    from evdev import ecodes
except ImportError:  # pragma: no cover - evdev is expected on the Pi.
    ecodes = None


PCA9685_ADDRESS = 0x40
FREQUENCY_HZ = 50
CONTROLLED_CHANNELS = (0, 1, 2, 3, 4, 5)

MIN_PULSE_US = 500
MAX_PULSE_US = 2500
HOME_PULSE_US = 1500
RAMP_STEP_US = 1
RAMP_STEP_SECONDS = 1.0

JOYSTICK_CENTER = 128
JOYSTICK_DEADZONE = 24

EV_SYN = 0x00
EV_KEY = 0x01
EV_ABS = 0x03

KEY_RELEASED = 0
KEY_PRESSED = 1

ABS_X = 0
ABS_Y = 1
ABS_Z = 2
ABS_RX = 3
ABS_RY = 4
ABS_RZ = 5

BTN_TL = 310
BTN_TR = 311
BTN_TL2 = 312
BTN_TR2 = 313
BTN_START = 315
BTN_MODE = 316

EVENT_STRUCT = struct.Struct("llHHI")

FALLBACK_EVENT_NAMES = {
    EV_SYN: "EV_SYN",
    EV_KEY: "EV_KEY",
    EV_ABS: "EV_ABS",
}

FALLBACK_CODE_NAMES = {
    EV_ABS: {
        ABS_X: "ABS_X",
        ABS_Y: "ABS_Y",
        ABS_Z: "ABS_Z",
        ABS_RX: "ABS_RX",
        ABS_RY: "ABS_RY",
        ABS_RZ: "ABS_RZ",
    },
    EV_KEY: {
        BTN_TL: "BTN_TL",
        BTN_TR: "BTN_TR",
        BTN_TL2: "BTN_TL2",
        BTN_TR2: "BTN_TR2",
        BTN_START: "BTN_START",
        BTN_MODE: "BTN_MODE",
    },
}


class EmergencyStop(Exception):
    pass


@dataclass(frozen=True)
class ControlMapping:
    channel: int
    label: str
    target_pulse_us: int


@dataclass
class ServoState:
    current_pulse_us: int = HOME_PULSE_US
    target_pulse_us: int = HOME_PULSE_US
    moving: bool = False
    next_step_time: float = 0.0


BUTTON_ACTIONS = {
    BTN_TL2: ControlMapping(0, "L2", 1500),
    BTN_TR2: ControlMapping(0, "R2", 2500),
    BTN_TL: ControlMapping(1, "L1", 500),
    BTN_TR: ControlMapping(1, "R1", 2500),
}

STICK_ACTIONS = {
    ABS_RX: {
        -1: ControlMapping(2, "RIGHT STICK LEFT", 500),
        0: ControlMapping(2, "RIGHT STICK CENTER", 1500),
        1: ControlMapping(2, "RIGHT STICK RIGHT", 2500),
    },
    ABS_RY: {
        -1: ControlMapping(3, "RIGHT STICK UP", 500),
        0: ControlMapping(3, "RIGHT STICK CENTER", 1500),
        1: ControlMapping(3, "RIGHT STICK DOWN", 2500),
    },
    ABS_Y: {
        -1: ControlMapping(4, "LEFT STICK UP", 1000),
        0: ControlMapping(4, "LEFT STICK CENTER", 1500),
        1: ControlMapping(4, "LEFT STICK DOWN", 2500),
    },
    ABS_X: {
        -1: ControlMapping(5, "LEFT STICK LEFT", 500),
        0: ControlMapping(5, "LEFT STICK CENTER", 1500),
        1: ControlMapping(5, "LEFT STICK RIGHT", 2500),
    },
}


def pulse_us_to_duty_cycle(pulse_us: int) -> int:
    period_us = 1_000_000 / FREQUENCY_HZ
    duty_fraction = pulse_us / period_us
    return max(0, min(0xFFFF, round(duty_fraction * 0xFFFF)))


def clamp_pulse(pulse_us: int) -> int:
    return max(MIN_PULSE_US, min(MAX_PULSE_US, pulse_us))


def symbolic_event_type(event_type: int) -> str:
    if ecodes is not None:
        name = ecodes.EV.get(event_type)
        if name:
            return str(name)
    return FALLBACK_EVENT_NAMES.get(event_type, "UNKNOWN")


def symbolic_event_code(event_type: int, code: int) -> str:
    if ecodes is not None:
        name = ecodes.bytype.get(event_type, {}).get(code)
        if name:
            if isinstance(name, list):
                return "|".join(name)
            return str(name)
    return FALLBACK_CODE_NAMES.get(event_type, {}).get(code, "UNKNOWN")


def validate_channel(channel: int) -> None:
    if channel not in CONTROLLED_CHANNELS:
        raise ValueError(f"Refusing to access unexpected channel {channel}.")


def set_channel_pulse(pca: PCA9685, channel: int, pulse_us: int) -> tuple[int, int]:
    validate_channel(channel)
    pulse_us = clamp_pulse(pulse_us)
    duty_cycle = pulse_us_to_duty_cycle(pulse_us)
    pca.channels[channel].duty_cycle = duty_cycle
    readback = pca.channels[channel].duty_cycle
    print(
        f"pwm_write channel={channel} pulse_us={pulse_us} duty_cycle={duty_cycle} readback={readback}",
        flush=True,
    )
    return duty_cycle, readback


def disable_all_controlled_pwm(pca: PCA9685) -> None:
    for channel in CONTROLLED_CHANNELS:
        pca.channels[channel].duty_cycle = 0


class ServoMotionEngine:
    def __init__(self, pca: PCA9685) -> None:
        self.pca = pca
        self.states = {channel: ServoState() for channel in CONTROLLED_CHANNELS}
        self.pwm_enabled = {channel: False for channel in CONTROLLED_CHANNELS}

    def command_target(self, action: ControlMapping, force_output: bool = False) -> None:
        channel = action.channel
        validate_channel(channel)
        target_pulse_us = clamp_pulse(action.target_pulse_us)
        state = self.states[channel]
        changed = state.target_pulse_us != target_pulse_us
        needs_output = force_output or not self.pwm_enabled[channel]

        if not changed and not needs_output:
            return

        state.target_pulse_us = target_pulse_us
        if state.current_pulse_us == state.target_pulse_us:
            if needs_output:
                set_channel_pulse(self.pca, channel, state.current_pulse_us)
                self.pwm_enabled[channel] = True
            state.moving = False
            state.next_step_time = 0.0
        else:
            state.moving = True
            state.next_step_time = time.monotonic()

        self.print_input(action)

    def advance_due_steps(self) -> None:
        now = time.monotonic()
        for channel, state in self.states.items():
            if not state.moving or now < state.next_step_time:
                continue
            self.advance_channel(channel, state, now)

    def advance_channel(self, channel: int, state: ServoState, now: float) -> None:
        if state.current_pulse_us == state.target_pulse_us:
            state.moving = False
            state.next_step_time = 0.0
            return

        step = RAMP_STEP_US if state.target_pulse_us > state.current_pulse_us else -RAMP_STEP_US
        next_pulse_us = state.current_pulse_us + step
        if step > 0:
            next_pulse_us = min(next_pulse_us, state.target_pulse_us)
        else:
            next_pulse_us = max(next_pulse_us, state.target_pulse_us)

        set_channel_pulse(self.pca, channel, next_pulse_us)
        self.pwm_enabled[channel] = True
        state.current_pulse_us = next_pulse_us

        if state.current_pulse_us == state.target_pulse_us:
            state.moving = False
            state.next_step_time = 0.0
        else:
            state.next_step_time = now + RAMP_STEP_SECONDS

    def next_due_time(self) -> float | None:
        due_times = [
            state.next_step_time
            for state in self.states.values()
            if state.moving
        ]
        return min(due_times) if due_times else None

    def all_idle(self) -> bool:
        return not any(state.moving for state in self.states.values())

    def print_input(self, action: ControlMapping) -> None:
        state = self.states[action.channel]
        readback = self.pca.channels[action.channel].duty_cycle
        print(
            f"controller_input={action.label} | channel={action.channel} | "
            f"target_pulse_us={state.target_pulse_us} | current_pulse_us={state.current_pulse_us} | "
            f"pwm_duty_readback={readback} | moving={state.moving}",
            flush=True,
        )


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
    raise RuntimeError("Could not find main Wireless Controller event device.")


def read_event(event_file):
    data = event_file.read(EVENT_STRUCT.size)
    if len(data) != EVENT_STRUCT.size:
        return None
    return EVENT_STRUCT.unpack(data)


def joystick_zone(value: int) -> int:
    if value < JOYSTICK_CENTER - JOYSTICK_DEADZONE:
        return -1
    if value > JOYSTICK_CENTER + JOYSTICK_DEADZONE:
        return 1
    return 0


def should_respond(action: ControlMapping, test_channel: int | None) -> bool:
    return test_channel is None or action.channel == test_channel


def action_from_event(
    event_type: int,
    code: int,
    value: int,
    stick_zones: dict[int, int],
    test_channel: int | None,
) -> ControlMapping | None:
    if event_type == EV_KEY:
        if code in (BTN_START, BTN_MODE) and value == KEY_PRESSED:
            raise EmergencyStop
        if value != KEY_PRESSED or code not in BUTTON_ACTIONS:
            return None
        action = BUTTON_ACTIONS[code]
        return action if should_respond(action, test_channel) else None

    if event_type != EV_ABS or code not in STICK_ACTIONS:
        return None

    zone = joystick_zone(value)
    previous_zone = stick_zones.get(code, 0)
    if zone == previous_zone:
        return None
    stick_zones[code] = zone

    if zone == 0 and previous_zone == 0:
        return None
    action = STICK_ACTIONS[code][zone]
    return action if should_respond(action, test_channel) else None


def confirm_home() -> bool:
    print("READY TO HOME ARM", flush=True)
    try:
        return input().strip() == "HOME"
    except EOFError:
        return False


def home_channels(motion: ServoMotionEngine, event_file) -> None:
    for channel in CONTROLLED_CHANNELS:
        action = ControlMapping(channel, "HOME", HOME_PULSE_US)
        print(f"HOMING CHANNEL {channel} -> {HOME_PULSE_US} us", flush=True)
        motion.command_target(action, force_output=True)
        while not motion.all_idle():
            readable, _, _ = select.select([event_file], [], [], 0)
            if readable:
                event = read_event(event_file)
                if event is not None:
                    _, _, event_type, code, value = event
                    if event_type == EV_KEY and code in (BTN_START, BTN_MODE) and value == KEY_PRESSED:
                        raise EmergencyStop
            motion.advance_due_steps()
            next_due = motion.next_due_time()
            if next_due is None:
                break
            time.sleep(min(0.02, max(0.0, next_due - time.monotonic())))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Clean Hiwonder PS4 manual controller on PCA9685 channels 0-5."
    )
    parser.add_argument("--event-device", default=None)
    parser.add_argument("--confirm-motion-ready", action="store_true")
    parser.add_argument(
        "--test-channel",
        type=int,
        choices=CONTROLLED_CHANNELS,
        default=None,
        help="Limit live controller responses to one PCA9685 channel.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.confirm_motion_ready:
        print("Refusing to run without --confirm-motion-ready.", file=sys.stderr)
        return 2

    pca: PCA9685 | None = None
    try:
        event_device = args.event_device or find_controller_event()
        i2c = busio.I2C(board.SCL, board.SDA)
        pca = PCA9685(i2c, address=PCA9685_ADDRESS)
        pca.frequency = FREQUENCY_HZ
        disable_all_controlled_pwm(pca)
        motion = ServoMotionEngine(pca)

        print("PWM disabled on startup for channels 0-5.", flush=True)
        print(f"Controller: {event_device}", flush=True)
        if args.test_channel is not None:
            print(f"TEST CHANNEL MODE: only channel {args.test_channel} will respond.", flush=True)

        if not confirm_home():
            print("Home confirmation failed; servos were not moved.", file=sys.stderr)
            return 2

        with open(event_device, "rb", buffering=0) as event_file:
            home_channels(motion, event_file)
            print("ARM HOMED - CONTROLLER ACTIVE", flush=True)
            print("READY - FULL ARM CONTROLLER", flush=True)
            print("OPTIONS/START or PS/MODE = EMERGENCY STOP", flush=True)

            stick_zones = {
                ABS_X: 0,
                ABS_Y: 0,
                ABS_RX: 0,
                ABS_RY: 0,
            }

            while True:
                motion.advance_due_steps()
                next_due = motion.next_due_time()
                timeout = 0.02
                if next_due is not None:
                    timeout = min(timeout, max(0.0, next_due - time.monotonic()))

                readable, _, _ = select.select([event_file], [], [], timeout)
                if not readable:
                    continue

                while True:
                    event = read_event(event_file)
                    if event is None:
                        break
                    _, _, event_type, code, value = event
                    action = action_from_event(event_type, code, value, stick_zones, args.test_channel)
                    if action is not None:
                        motion.command_target(action)

                    readable_more, _, _ = select.select([event_file], [], [], 0)
                    if not readable_more:
                        break

    except EmergencyStop:
        print("EMERGENCY STOP - disabling PWM on channels 0-5.", file=sys.stderr)
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
                disable_all_controlled_pwm(pca)
                print("PWM disabled on channels 0-5.", flush=True)
            except Exception as exc:
                print(f"Warning: failed to disable PWM on channels 0-5: {exc}", file=sys.stderr)
            try:
                pca.deinit()
            except Exception as exc:
                print(f"Warning: failed to deinitialize PCA9685: {exc}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
