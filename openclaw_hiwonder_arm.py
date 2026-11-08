#!/usr/bin/env python3
"""Bounded OpenClaw-style command helper for the Hiwonder PCA9685 arm.

This file is intentionally separate from the PS4 manual controller. It gives an
agent or shell a small, validated command surface for this exact arm setup.
Motion commands require --confirm-motion-ready. Dry-run/status commands do not.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import board
import busio
from adafruit_pca9685 import PCA9685


PCA9685_ADDRESS = 0x40
FREQUENCY_HZ = 50
CONTROLLED_CHANNELS = (0, 1, 2, 3, 4, 5)

MIN_PULSE_US = 500
MAX_PULSE_US = 2500
START_PULSE_US = 1500
RAMP_STEP_US = 5
RAMP_STEP_SECONDS = 0.96

CONTROL_TARGETS = {
    "L2": (0, 1500),
    "R2": (0, 2500),
    "L1": (1, 500),
    "R1": (1, 2500),
    "RIGHT_STICK_LEFT": (2, 500),
    "RIGHT_STICK_CENTER_X": (2, 1500),
    "RIGHT_STICK_RIGHT": (2, 2500),
    "RIGHT_STICK_UP": (3, 500),
    "RIGHT_STICK_CENTER_Y": (3, 1500),
    "RIGHT_STICK_DOWN": (3, 2500),
    "LEFT_STICK_UP": (4, 1000),
    "LEFT_STICK_CENTER_Y": (4, 1500),
    "LEFT_STICK_DOWN": (4, 2500),
    "LEFT_STICK_LEFT": (5, 500),
    "LEFT_STICK_CENTER_X": (5, 1500),
    "LEFT_STICK_RIGHT": (5, 2500),
}


class MotionRefused(Exception):
    pass


@dataclass
class ServoState:
    current_pulse: int = START_PULSE_US
    target_pulse: int = START_PULSE_US
    moving: bool = False
    next_step_time: float = 0.0


def pulse_us_to_duty_cycle(pulse_us: int) -> int:
    period_us = 1_000_000 / FREQUENCY_HZ
    duty_fraction = pulse_us / period_us
    return max(0, min(0xFFFF, round(duty_fraction * 0xFFFF)))


def clamp_pulse(pulse_us: int) -> int:
    return max(MIN_PULSE_US, min(MAX_PULSE_US, pulse_us))


def validate_channel(channel: int) -> None:
    if channel not in CONTROLLED_CHANNELS:
        raise ValueError(f"Channel must be one of {CONTROLLED_CHANNELS}; got {channel}.")


def disable_all_pwm(pca: PCA9685) -> None:
    for channel in CONTROLLED_CHANNELS:
        pca.channels[channel].duty_cycle = 0


class MotionEngine:
    def __init__(self, pca: PCA9685) -> None:
        self.pca = pca
        self.states = {channel: ServoState() for channel in CONTROLLED_CHANNELS}

    def command_target(self, channel: int, pulse_us: int) -> None:
        validate_channel(channel)
        state = self.states[channel]
        state.target_pulse = clamp_pulse(pulse_us)
        if state.current_pulse != state.target_pulse:
            state.moving = True
            state.next_step_time = time.monotonic()

    def advance_due_steps(self) -> None:
        now = time.monotonic()
        for channel, state in self.states.items():
            if not state.moving or now < state.next_step_time:
                continue
            step = RAMP_STEP_US if state.target_pulse > state.current_pulse else -RAMP_STEP_US
            next_pulse = state.current_pulse + step
            if step > 0:
                next_pulse = min(next_pulse, state.target_pulse)
            else:
                next_pulse = max(next_pulse, state.target_pulse)

            duty = pulse_us_to_duty_cycle(next_pulse)
            self.pca.channels[channel].duty_cycle = duty
            readback = self.pca.channels[channel].duty_cycle
            state.current_pulse = next_pulse
            print(
                f"PWM_WRITE channel={channel} pulse_us={next_pulse} "
                f"duty_cycle={duty} readback={readback}",
                flush=True,
            )
            if state.current_pulse == state.target_pulse:
                state.moving = False
                state.next_step_time = 0.0
            else:
                state.next_step_time = now + RAMP_STEP_SECONDS

    def wait_until_idle(self) -> None:
        while any(state.moving for state in self.states.values()):
            self.advance_due_steps()
            time.sleep(0.02)


def load_plan(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("Plan must be a JSON list.")
    return [validate_plan_step(step) for step in data]


def validate_plan_step(step: Any) -> dict[str, Any]:
    if not isinstance(step, dict):
        raise ValueError("Every plan step must be an object.")
    if "control" in step:
        control = str(step["control"]).upper()
        if control not in CONTROL_TARGETS:
            raise ValueError(f"Unknown control {control!r}.")
        channel, pulse_us = CONTROL_TARGETS[control]
    else:
        channel = int(step["channel"])
        pulse_us = int(step["pulse_us"])
        validate_channel(channel)
        pulse_us = clamp_pulse(pulse_us)
    hold_s = float(step.get("hold_s", 0.0))
    if hold_s < 0.0 or hold_s > 30.0:
        raise ValueError("hold_s must be between 0 and 30 seconds.")
    return {"channel": channel, "pulse_us": pulse_us, "hold_s": hold_s}


def print_status() -> None:
    print("HIWONDER_OPENCLAW_ADAPTER")
    print(f"pca9685_address=0x{PCA9685_ADDRESS:02x}")
    print(f"frequency_hz={FREQUENCY_HZ}")
    print(f"channels={','.join(str(channel) for channel in CONTROLLED_CHANNELS)}")
    print(f"pulse_limits_us={MIN_PULSE_US}-{MAX_PULSE_US}")
    print(f"home_pulse_us={START_PULSE_US}")
    print(f"ramp_step_us={RAMP_STEP_US}")
    print(f"ramp_step_seconds={RAMP_STEP_SECONDS}")
    print("motion_requires=--confirm-motion-ready")


def require_motion_confirmation(args: argparse.Namespace) -> None:
    if not args.confirm_motion_ready:
        raise MotionRefused("Refusing motion without --confirm-motion-ready.")


def run_steps(steps: list[dict[str, Any]], args: argparse.Namespace) -> int:
    require_motion_confirmation(args)
    i2c = busio.I2C(board.SCL, board.SDA)
    pca = PCA9685(i2c, address=PCA9685_ADDRESS)
    pca.frequency = FREQUENCY_HZ
    try:
        disable_all_pwm(pca)
        print("PWM_DISABLED channels=0-5", flush=True)
        motion = MotionEngine(pca)
        for index, step in enumerate(steps, start=1):
            channel = step["channel"]
            pulse_us = step["pulse_us"]
            print(f"STEP index={index} channel={channel} target_pulse_us={pulse_us}", flush=True)
            motion.command_target(channel, pulse_us)
            motion.wait_until_idle()
            if step["hold_s"]:
                time.sleep(step["hold_s"])
        return 0
    finally:
        disable_all_pwm(pca)
        pca.deinit()
        print("PWM_DISABLED channels=0-5", flush=True)


def run_home(args: argparse.Namespace) -> int:
    steps = [{"channel": channel, "pulse_us": START_PULSE_US, "hold_s": 0.0} for channel in CONTROLLED_CHANNELS]
    return run_steps(steps, args)


def run_control(args: argparse.Namespace) -> int:
    control = args.control.upper()
    channel, pulse_us = CONTROL_TARGETS[control]
    if args.test_channel is not None and channel != args.test_channel:
        print(f"IGNORED test_channel={args.test_channel} control_channel={channel}")
        return 0
    return run_steps([{"channel": channel, "pulse_us": pulse_us, "hold_s": 0.0}], args)


def run_plan(args: argparse.Namespace) -> int:
    steps = load_plan(Path(args.plan))
    if args.test_channel is not None:
        steps = [step for step in steps if step["channel"] == args.test_channel]
    if args.dry_run:
        print(json.dumps(steps, indent=2))
        return 0
    return run_steps(steps, args)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OpenClaw-safe Hiwonder arm command helper.")
    parser.add_argument("--confirm-motion-ready", action="store_true")
    parser.add_argument("--test-channel", type=int, choices=CONTROLLED_CHANNELS)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("status")
    subparsers.add_parser("home")

    control_parser = subparsers.add_parser("control")
    control_parser.add_argument("control", choices=sorted(CONTROL_TARGETS))

    plan_parser = subparsers.add_parser("plan")
    plan_parser.add_argument("plan")
    plan_parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "status":
            print_status()
            return 0
        if args.command == "home":
            return run_home(args)
        if args.command == "control":
            return run_control(args)
        if args.command == "plan":
            return run_plan(args)
        raise ValueError(f"Unknown command {args.command!r}.")
    except MotionRefused as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted; PWM cleanup attempted if hardware was active.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
