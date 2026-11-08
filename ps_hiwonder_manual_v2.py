#!/usr/bin/env python3
"""Clean manual PS4 controller for the Hiwonder arm on PCA9685 channels 0-5."""

from __future__ import annotations

import argparse
import os
import re
import select
import struct
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
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
START_PULSE_US = 1500
STEP_US = 5
UPDATE_SECONDS = 0.10
RAMP_STEP_US = 5
RAMP_STEP_SECONDS = 0.96
JOYSTICK_CENTER = 128
JOYSTICK_DEADZONE = 24
TRIGGER_AXIS_THRESHOLD = 32
DEBUG_LOG_PATH = Path.home() / "robot-arm" / "ps4_controller_debug.log"

# Reverse individual joints here after physical direction testing.
CHANNEL_DIRECTION_SIGN = {
    0: 1,
    1: 1,
    2: 1,
    3: 1,
    4: 1,
    5: 1,
}

EV_KEY = 0x01
EV_ABS = 0x03
EV_SYN = 0x00
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
    EV_SYN: {
        0: "SYN_REPORT",
    },
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


class ControllerDiagnostics:
    def __init__(self, log_path: Path) -> None:
        self.log_path = log_path
        self.lock = threading.Lock()
        self.running = False
        self.event_device = "unknown"
        self.controller_name = "unknown"
        self.controller_active = False
        self.move_active = False
        self.move_channel: int | None = None
        self.move_target_us: int | None = None
        self.channel_states: dict[int, tuple[int, int, bool]] = {
            channel: (START_PULSE_US, START_PULSE_US, False)
            for channel in CONTROLLED_CHANNELS
        }
        self.last_event_monotonic: float | None = None
        self.previous_event_monotonic: float | None = None
        self.total_events = 0
        self.events_this_second = 0

    def start_heartbeat(self) -> None:
        self.running = True
        thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        thread.start()

    def stop(self) -> None:
        self.running = False

    def log(self, category: str, message: str) -> None:
        wall = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        monotonic = time.monotonic()
        line = f"{wall} monotonic={monotonic:.3f} category={category} {message}\n"
        with self.lock:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as log_file:
                log_file.write(line)

    def log_event(self, event_type: int, code: int, value: int) -> None:
        now = time.monotonic()
        previous = self.previous_event_monotonic
        delta_ms = 0.0 if previous is None else (now - previous) * 1000.0
        self.previous_event_monotonic = now
        self.last_event_monotonic = now
        self.total_events += 1
        self.events_this_second += 1
        self.log(
            "EVENT",
            "type={event_type} type_name={type_name} code={code} "
            "code_name={code_name} value={value} ms_since_previous={delta_ms:.1f}".format(
                event_type=event_type,
                type_name=symbolic_event_type(event_type),
                code=code,
                code_name=symbolic_event_code(event_type, code),
                value=value,
                delta_ms=delta_ms,
            ),
        )

    def controller_mode(self) -> str:
        if self.move_active:
            return "MANUAL"
        return "IDLE"

    def update_channel_states(self, states: dict[int, "ServoState"]) -> None:
        with self.lock:
            self.channel_states = {
                channel: (state.current_pulse, state.target_pulse, state.moving)
                for channel, state in states.items()
            }

    def _heartbeat_loop(self) -> None:
        while self.running:
            time.sleep(1.0)
            now = time.monotonic()
            with self.lock:
                events_previous_second = self.events_this_second
                self.events_this_second = 0
                last_event = self.last_event_monotonic
                total_events = self.total_events
                event_device = self.event_device
                mode = self.controller_mode()
                move_active = self.move_active
                move_channel = self.move_channel
                move_target = self.move_target_us
                channel_states = dict(self.channel_states)
            age = "none" if last_event is None else f"{now - last_event:.3f}"
            channel_text = " ".join(
                "ch{channel}_current={current} ch{channel}_target={target} ch{channel}_moving={moving}".format(
                    channel=channel,
                    current=current,
                    target=target,
                    moving="yes" if moving else "no",
                )
                for channel, (current, target, moving) in sorted(channel_states.items())
            )
            self.log(
                "HEARTBEAT",
                "process_alive=yes event_loop_alive=yes event_device={event_device} seconds_since_last_event={age} "
                "total_events={total_events} events_previous_1s={events_previous_second} "
                "mode={mode} servo_motion_active={move_active} move_channel={move_channel} "
                "current_target_pulse={move_target} {channel_text}".format(
                    event_device=event_device,
                    age=age,
                    total_events=total_events,
                    events_previous_second=events_previous_second,
                    mode=mode,
                    move_active="yes" if move_active else "no",
                    move_channel="none" if move_channel is None else move_channel,
                    move_target="none" if move_target is None else move_target,
                    channel_text=channel_text,
                ),
            )


DIAGNOSTICS = ControllerDiagnostics(DEBUG_LOG_PATH)


@dataclass(frozen=True)
class ControlMapping:
    channel: int
    direction: int
    label: str
    absolute_pulse_us: int | None = None


@dataclass
class ServoState:
    current_pulse: int
    target_pulse: int
    moving: bool = False
    next_step_time: float = 0.0
    movement_started: float | None = None


BUTTON_ACTIONS = {
    BTN_TL2: ControlMapping(0, -1, "L2 OPEN CLAW", 1500),
    BTN_TR2: ControlMapping(0, 1, "R2 CLOSE CLAW", 2500),
    BTN_TL: ControlMapping(1, -1, "L1", 500),
    BTN_TR: ControlMapping(1, 1, "R1", 2500),
}

TRIGGER_AXIS_ACTIONS = {
    ABS_Z: BUTTON_ACTIONS[BTN_TL2],
    ABS_RZ: BUTTON_ACTIONS[BTN_TR2],
}

TRIGGER_AXIS_TO_BUTTON = {
    ABS_Z: BTN_TL2,
    ABS_RZ: BTN_TR2,
}

TRIGGER_BUTTON_TO_AXIS = {
    BTN_TL2: ABS_Z,
    BTN_TR2: ABS_RZ,
}

STICK_AXIS_ACTIONS = {
    ABS_RX: (ControlMapping(2, -1, "RIGHT STICK LEFT", 500), ControlMapping(2, 1, "RIGHT STICK RIGHT", 2500)),
    ABS_RY: (ControlMapping(3, -1, "RIGHT STICK UP", 500), ControlMapping(3, 1, "RIGHT STICK DOWN", 2500)),
    ABS_Y: (ControlMapping(4, -1, "LEFT STICK UP", 1000), ControlMapping(4, 1, "LEFT STICK DOWN", 2500)),
    ABS_X: (ControlMapping(5, -1, "LEFT STICK LEFT", 500), ControlMapping(5, 1, "LEFT STICK RIGHT", 2500)),
}

STICK_CENTER_ACTIONS = {
    ABS_RX: ControlMapping(2, 0, "RIGHT STICK CENTER", 1500),
    ABS_RY: ControlMapping(3, 0, "RIGHT STICK CENTER", 1500),
    ABS_Y: ControlMapping(4, 0, "LEFT STICK CENTER", 1500),
    ABS_X: ControlMapping(5, 0, "LEFT STICK CENTER", 1500),
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


def validate_channel(channel: int) -> None:
    if channel not in CONTROLLED_CHANNELS:
        raise ValueError(f"Refusing to access unexpected channel {channel}.")


def disable_channel(pca: PCA9685, channel: int) -> None:
    validate_channel(channel)
    pca.channels[channel].duty_cycle = 0


def disable_all_controlled_pwm(pca: PCA9685) -> None:
    for channel in CONTROLLED_CHANNELS:
        pca.channels[channel].duty_cycle = 0


def set_channel_pulse(pca: PCA9685, channel: int, pulse_us: int) -> int:
    validate_channel(channel)
    pulse_us = clamp_pulse(pulse_us)
    duty_cycle = pulse_us_to_duty_cycle(pulse_us)
    pca.channels[channel].duty_cycle = duty_cycle
    readback = pca.channels[channel].duty_cycle
    DIAGNOSTICS.log(
        "PWM_WRITE",
        f"channel={channel} pulse_us={pulse_us} duty_cycle={duty_cycle} readback={readback}",
    )
    print(
        f"pwm_write channel={channel} pulse_us={pulse_us} duty_cycle={duty_cycle} readback={readback}",
        flush=True,
    )
    return duty_cycle


class ServoMotionEngine:
    def __init__(self, pca: PCA9685, channels: tuple[int, ...]) -> None:
        self.pca = pca
        self.states = {
            channel: ServoState(START_PULSE_US, START_PULSE_US)
            for channel in channels
        }
        self.pwm_enabled = {channel: False for channel in channels}
        DIAGNOSTICS.update_channel_states(self.states)

    def command_target(self, channel: int, target_us: int, control_name: str, force_output: bool = False) -> bool:
        validate_channel(channel)
        target_us = clamp_pulse(target_us)
        state = self.states[channel]
        previous_target = state.target_pulse
        force_output = force_output or not self.pwm_enabled[channel]
        if previous_target == target_us and not force_output:
            return False
        state.target_pulse = target_us

        if state.current_pulse == state.target_pulse:
            duty_cycle = None
            readback = None
            if force_output or state.moving:
                duty_cycle = set_channel_pulse(self.pca, channel, state.target_pulse)
                readback = self.pca.channels[channel].duty_cycle
                self.pwm_enabled[channel] = True
            state.moving = False
            state.next_step_time = 0.0
            state.movement_started = None
            self._update_diagnostic_motion_state()
            if duty_cycle is not None:
                DIAGNOSTICS.log(
                    "MOVE_END",
                    "channel={channel} current_pulse={current} target_pulse={target} "
                    "elapsed_movement_seconds=0.000 duty_cycle={duty} readback={readback} control={control}".format(
                        channel=channel,
                        current=state.current_pulse,
                        target=state.target_pulse,
                        duty=duty_cycle,
                        readback=readback,
                        control=control_name.replace(" ", "_"),
                    ),
                )
            return previous_target != target_us

        now = time.monotonic()
        was_moving = state.moving
        state.moving = True
        state.next_step_time = now
        if not was_moving:
            state.movement_started = now
            DIAGNOSTICS.log(
                "MOVE_START",
                "channel={channel} current_pulse={current} target_pulse={target} control={control}".format(
                    channel=channel,
                    current=state.current_pulse,
                    target=state.target_pulse,
                    control=control_name.replace(" ", "_"),
                ),
            )
        else:
            DIAGNOSTICS.log(
                "MOVE_TARGET_UPDATE",
                "channel={channel} current_pulse={current} previous_target_pulse={previous} "
                "target_pulse={target} control={control}".format(
                    channel=channel,
                    current=state.current_pulse,
                    previous=previous_target,
                    target=state.target_pulse,
                    control=control_name.replace(" ", "_"),
                ),
            )
        self._update_diagnostic_motion_state()
        return True

    def cancel_all_motion(self) -> None:
        for channel, state in self.states.items():
            if state.moving:
                DIAGNOSTICS.log(
                    "MOVE_CANCEL",
                    f"channel={channel} current_pulse={state.current_pulse} target_pulse={state.target_pulse}",
                )
            state.target_pulse = state.current_pulse
            state.moving = False
            state.next_step_time = 0.0
            state.movement_started = None
        self._update_diagnostic_motion_state()

    def advance_due_steps(self) -> None:
        now = time.monotonic()
        for channel, state in self.states.items():
            if not state.moving or now < state.next_step_time:
                continue
            self._advance_channel(channel, state, now)
        self._update_diagnostic_motion_state()

    def next_due_time(self) -> float | None:
        due_times = [
            state.next_step_time
            for state in self.states.values()
            if state.moving
        ]
        return min(due_times) if due_times else None

    def all_idle(self) -> bool:
        return not any(state.moving for state in self.states.values())

    def _advance_channel(self, channel: int, state: ServoState, now: float) -> None:
        if state.current_pulse == state.target_pulse:
            state.moving = False
            state.next_step_time = 0.0
            state.movement_started = None
            return

        step = RAMP_STEP_US if state.target_pulse > state.current_pulse else -RAMP_STEP_US
        next_us = state.current_pulse + step
        if step > 0:
            next_us = min(next_us, state.target_pulse)
        else:
            next_us = max(next_us, state.target_pulse)

        duty_cycle = set_channel_pulse(self.pca, channel, next_us)
        readback = self.pca.channels[channel].duty_cycle
        self.pwm_enabled[channel] = True
        state.current_pulse = next_us
        elapsed = 0.0 if state.movement_started is None else now - state.movement_started
        DIAGNOSTICS.log(
            "MOVE_STEP",
            "channel={channel} current_pulse={current} target_pulse={target} "
            "elapsed_movement_seconds={elapsed:.3f} duty_cycle={duty} readback={readback}".format(
                channel=channel,
                current=state.current_pulse,
                target=state.target_pulse,
                elapsed=elapsed,
                duty=duty_cycle,
                readback=readback,
            ),
        )

        if state.current_pulse == state.target_pulse:
            state.moving = False
            state.next_step_time = 0.0
            DIAGNOSTICS.log(
                "MOVE_END",
                "channel={channel} current_pulse={current} target_pulse={target} "
                "elapsed_movement_seconds={elapsed:.3f}".format(
                    channel=channel,
                    current=state.current_pulse,
                    target=state.target_pulse,
                    elapsed=elapsed,
                ),
            )
            state.movement_started = None
        else:
            state.next_step_time = now + RAMP_STEP_SECONDS

    def _update_diagnostic_motion_state(self) -> None:
        moving_channels = [
            channel for channel, state in self.states.items()
            if state.moving
        ]
        DIAGNOSTICS.move_active = bool(moving_channels)
        DIAGNOSTICS.move_channel = moving_channels[0] if len(moving_channels) == 1 else None
        if len(moving_channels) == 1:
            DIAGNOSTICS.move_target_us = self.states[moving_channels[0]].target_pulse
        else:
            DIAGNOSTICS.move_target_us = None
        DIAGNOSTICS.update_channel_states(self.states)

    def mark_all_pwm_disabled(self) -> None:
        for channel in self.pwm_enabled:
            self.pwm_enabled[channel] = False


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


def controller_name_for_event(event_device: str) -> str:
    event_name = Path(event_device).name
    for device in parse_input_devices():
        handlers = device.get("handlers", "")
        if re.search(rf"\b{re.escape(event_name)}\b", handlers):
            return device.get("name", "unknown")
    return "unknown"


def read_event(event_file):
    data = event_file.read(EVENT_STRUCT.size)
    if len(data) != EVENT_STRUCT.size:
        return None
    event = EVENT_STRUCT.unpack(data)
    _, _, event_type, code, value = event
    DIAGNOSTICS.log_event(event_type, code, value)
    return event


def print_l2_r2_event(event_type: int, code: int, value: int) -> None:
    if event_type == EV_KEY and code not in TRIGGER_BUTTON_TO_AXIS:
        return
    if event_type == EV_ABS and code not in TRIGGER_AXIS_TO_BUTTON:
        return
    if event_type not in (EV_KEY, EV_ABS):
        return
    print(
        "l2_r2_event type={event_type} type_name={type_name} code={code} "
        "code_name={code_name} value={value}".format(
            event_type=event_type,
            type_name=symbolic_event_type(event_type),
            code=code,
            code_name=symbolic_event_code(event_type, code),
            value=value,
        ),
        flush=True,
    )


def stick_zone(value: int) -> int:
    if value < JOYSTICK_CENTER - JOYSTICK_DEADZONE:
        return -1
    if value > JOYSTICK_CENTER + JOYSTICK_DEADZONE:
        return 1
    return 0


def stick_action(code: int, value: int) -> ControlMapping | None:
    if code not in STICK_AXIS_ACTIONS:
        return None
    negative_action, positive_action = STICK_AXIS_ACTIONS[code]
    zone = stick_zone(value)
    if zone < 0:
        return negative_action
    if zone > 0:
        return positive_action
    return None


def active_mapping(
    pressed_buttons: set[int],
    trigger_values: dict[int, int],
    stick_values: dict[int, int],
    previous_stick_zones: dict[int, int],
    test_channel: int | None,
) -> ControlMapping | None:
    for button in (BTN_TL2, BTN_TR2, BTN_TL, BTN_TR):
        if button in pressed_buttons:
            action = BUTTON_ACTIONS[button]
            return action if test_channel is None or action.channel == test_channel else None

    for axis in (ABS_Z, ABS_RZ):
        if trigger_values.get(axis, 0) >= TRIGGER_AXIS_THRESHOLD:
            action = TRIGGER_AXIS_ACTIONS[axis]
            return action if test_channel is None or action.channel == test_channel else None

    for axis in (ABS_RX, ABS_RY, ABS_Y, ABS_X):
        value = stick_values.get(axis, JOYSTICK_CENTER)
        zone = stick_zone(value)
        action = stick_action(axis, stick_values.get(axis, JOYSTICK_CENTER))
        if axis in STICK_CENTER_ACTIONS:
            previous_zone = previous_stick_zones.get(axis, 0)
            previous_stick_zones[axis] = zone
            if action is None and previous_zone != 0 and zone == 0:
                center_action = STICK_CENTER_ACTIONS[axis]
                return center_action if test_channel is None or center_action.channel == test_channel else None
        if action is not None:
            return action if test_channel is None or action.channel == test_channel else None

    return None


def apply_action(
    motion: ServoMotionEngine,
    action: ControlMapping,
) -> None:
    channel = action.channel
    validate_channel(channel)
    state = motion.states[channel]
    if action.absolute_pulse_us is None:
        signed_step = STEP_US * action.direction * CHANNEL_DIRECTION_SIGN[channel]
        target_pulse_us = clamp_pulse(state.target_pulse + signed_step)
    else:
        target_pulse_us = clamp_pulse(action.absolute_pulse_us)

    DIAGNOSTICS.log(
        "ACTION",
        "control={control} channel={channel} requested_target_pulse={target} "
        "current_servo_pulse={current}".format(
            control=action.label.replace(" ", "_"),
            channel=channel,
            target=target_pulse_us,
            current=state.current_pulse,
        ),
    )
    target_changed = motion.command_target(channel, target_pulse_us, action.label)
    if not target_changed:
        return
    diagnostic = (
        f"{action.label} -> channel {channel} -> {target_pulse_us} us"
        if action.absolute_pulse_us is not None
        else action.label
    )
    print(
        f"controller_input={diagnostic} | channel={channel} | "
        f"direction={action.direction} | target_pulse_us={target_pulse_us} | "
        f"current_pulse_us={state.current_pulse} | "
        f"pwm_duty_readback={motion.pca.channels[channel].duty_cycle} | moving={state.moving}",
        flush=True,
    )


def confirm_home() -> bool:
    print("READY TO HOME ARM", flush=True)
    try:
        return input().strip() == "HOME"
    except EOFError:
        return False


def home_channels(
    motion: ServoMotionEngine,
    channels: tuple[int, ...],
    event_file=None,
) -> None:
    for channel in channels:
        print(f"HOMING CHANNEL {channel} -> {START_PULSE_US} us", flush=True)
        motion.command_target(channel, START_PULSE_US, "HOME", force_output=True)
        while not motion.all_idle():
            if event_file is not None:
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
        description="Full-arm PS4 controller for Hiwonder servos on channels 0-5."
    )
    parser.add_argument("--event-device", default=None)
    parser.add_argument("--confirm-motion-ready", action="store_true")
    parser.add_argument(
        "--test-channel",
        type=int,
        choices=CONTROLLED_CHANNELS,
        default=None,
        help="Limit controller responses to one channel during safety testing.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    DEBUG_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    DEBUG_LOG_PATH.write_text("", encoding="utf-8")
    DIAGNOSTICS.start_heartbeat()
    DIAGNOSTICS.log("STARTUP", f"pid={os.getpid()} controller_active=no")
    if not args.confirm_motion_ready:
        DIAGNOSTICS.log("STARTUP", "refusing_to_run_without_confirm_motion_ready=yes")
        print("Refusing to run without --confirm-motion-ready.", file=sys.stderr)
        DIAGNOSTICS.stop()
        return 2

    try:
        event_device = args.event_device or find_controller_event()
        controller_name = controller_name_for_event(event_device)
        DIAGNOSTICS.event_device = event_device
        DIAGNOSTICS.controller_name = controller_name
        DIAGNOSTICS.log(
            "STARTUP",
            "selected_wireless_controller=yes event_device={event_device} "
            "controller_name={controller_name} pid={pid} controller_active=no".format(
                event_device=event_device,
                controller_name=controller_name,
                pid=os.getpid(),
            ),
        )
    except Exception as exc:
        DIAGNOSTICS.log("STARTUP", f"controller_setup_failed={exc}")
        print(f"Controller setup failed: {exc}", file=sys.stderr)
        DIAGNOSTICS.stop()
        return 2

    pca: PCA9685 | None = None
    motion: ServoMotionEngine | None = None
    pressed_buttons: set[int] = set()
    trigger_event_sources: dict[int, int | None] = {
        BTN_TL2: None,
        BTN_TR2: None,
    }
    trigger_values = {ABS_Z: 0, ABS_RZ: 0}
    stick_values = {
        ABS_X: JOYSTICK_CENTER,
        ABS_Y: JOYSTICK_CENTER,
        ABS_RX: JOYSTICK_CENTER,
        ABS_RY: JOYSTICK_CENTER,
    }
    previous_stick_zones = {
        ABS_RX: stick_zone(JOYSTICK_CENTER),
        ABS_RY: stick_zone(JOYSTICK_CENTER),
        ABS_Y: stick_zone(JOYSTICK_CENTER),
        ABS_X: stick_zone(JOYSTICK_CENTER),
    }

    try:
        DIAGNOSTICS.log("STARTUP", "pca9685_initialization_status=starting")
        i2c = busio.I2C(board.SCL, board.SDA)
        pca = PCA9685(i2c, address=PCA9685_ADDRESS)
        pca.frequency = FREQUENCY_HZ
        disable_all_controlled_pwm(pca)
        motion = ServoMotionEngine(pca, CONTROLLED_CHANNELS)
        DIAGNOSTICS.log(
            "STARTUP",
            f"pca9685_initialization_status=ok address=0x{PCA9685_ADDRESS:02x} frequency_hz={FREQUENCY_HZ}",
        )

        print("PWM disabled on startup for channels 0-5.", flush=True)
        print(f"Controller: {event_device}", flush=True)
        if args.test_channel is not None:
            print(f"TEST CHANNEL MODE: only channel {args.test_channel} will respond.", flush=True)

        if not confirm_home():
            print("Home confirmation failed; servos were not moved.", file=sys.stderr)
            return 2

        with open(event_device, "rb", buffering=0) as event_file:
            home_targets = (args.test_channel,) if args.test_channel is not None else CONTROLLED_CHANNELS
            home_channels(motion, home_targets, event_file)

            DIAGNOSTICS.controller_active = True
            DIAGNOSTICS.log(
                "STARTUP",
                f"controller_active=yes event_device={event_device} controller_name={controller_name}",
            )
            print("ARM HOMED - CONTROLLER ACTIVE", flush=True)
            print("READY - FULL ARM CONTROLLER", flush=True)
            print("OPTIONS/START or PS/MODE = EMERGENCY STOP", flush=True)

            next_update = time.monotonic()
            while True:
                now = time.monotonic()
                motion.advance_due_steps()
                next_due = motion.next_due_time()
                wake_times = [next_update]
                if next_due is not None:
                    wake_times.append(next_due)
                timeout = max(0.0, min(wake_times) - now)
                timeout = min(timeout, 0.02)
                readable, _, _ = select.select([event_file], [], [], timeout)
                if readable:
                    while True:
                        event = read_event(event_file)
                        if event is None:
                            break
                        _, _, event_type, code, value = event
                        print_l2_r2_event(event_type, code, value)
                        if event_type == EV_KEY:
                            if code in (BTN_START, BTN_MODE) and value == KEY_PRESSED:
                                raise EmergencyStop
                            if code in BUTTON_ACTIONS:
                                if code in trigger_event_sources:
                                    if value == KEY_PRESSED and trigger_event_sources[code] is None:
                                        trigger_event_sources[code] = EV_KEY
                                    if trigger_event_sources[code] != EV_KEY:
                                        pass
                                    elif value == KEY_PRESSED:
                                        pressed_buttons.add(code)
                                    elif value == KEY_RELEASED:
                                        pressed_buttons.discard(code)
                                elif value == KEY_PRESSED:
                                    pressed_buttons.add(code)
                                elif value == KEY_RELEASED:
                                    pressed_buttons.discard(code)
                        elif event_type == EV_ABS:
                            if code in trigger_values:
                                trigger_button = TRIGGER_AXIS_TO_BUTTON[code]
                                if value >= TRIGGER_AXIS_THRESHOLD and trigger_event_sources[trigger_button] is None:
                                    trigger_event_sources[trigger_button] = EV_ABS
                                if trigger_event_sources[trigger_button] == EV_ABS:
                                    trigger_values[code] = value
                            elif code in stick_values:
                                stick_values[code] = value

                        action = active_mapping(
                            pressed_buttons,
                            trigger_values,
                            stick_values,
                            previous_stick_zones,
                            args.test_channel,
                        )
                        if action is not None:
                            apply_action(motion, action)

                        readable_more, _, _ = select.select([event_file], [], [], 0)
                        if not readable_more:
                            break

                now = time.monotonic()
                motion.advance_due_steps()
                if now < next_update:
                    continue
                next_update = now + UPDATE_SECONDS

                action = active_mapping(
                    pressed_buttons,
                    trigger_values,
                    stick_values,
                    previous_stick_zones,
                    args.test_channel,
                )
                if action is not None:
                    apply_action(motion, action)

    except EmergencyStop:
        DIAGNOSTICS.log("EMERGENCY_STOP", "emergency_stop=yes disabling_pwm=yes")
        print("EMERGENCY STOP - disabling PWM on channels 0-5.", file=sys.stderr)
        return 0
    except KeyboardInterrupt:
        DIAGNOSTICS.log("SHUTDOWN", "keyboard_interrupt=yes disabling_pwm=yes")
        print("Ctrl+C received; disabling PWM on channels 0-5.", file=sys.stderr)
        return 130
    except Exception as exc:
        DIAGNOSTICS.log("ERROR", f"exception={exc}")
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        DIAGNOSTICS.controller_active = False
        if pca is not None:
            try:
                disable_all_controlled_pwm(pca)
                DIAGNOSTICS.log("SHUTDOWN", "pwm_disabled=yes channels=0-5")
                print("PWM disabled on channels 0-5.", flush=True)
            except Exception as exc:  # pragma: no cover - hardware cleanup path
                DIAGNOSTICS.log("SHUTDOWN", f"pwm_disable_failed={exc}")
                print(f"Warning: failed to disable PWM on channels 0-5: {exc}", file=sys.stderr)
            try:
                pca.deinit()
                DIAGNOSTICS.log("SHUTDOWN", "pca9685_deinit=yes")
            except Exception as exc:  # pragma: no cover - hardware cleanup path
                DIAGNOSTICS.log("SHUTDOWN", f"pca9685_deinit_failed={exc}")
                print(f"Warning: failed to deinitialize PCA9685: {exc}", file=sys.stderr)
        DIAGNOSTICS.stop()


if __name__ == "__main__":
    raise SystemExit(main())
