#!/usr/bin/env python3
"""USB camera MJPEG stream with slow random motion on channels 3, 4, and 5."""

from __future__ import annotations

import argparse
import http.server
import os
import random
import re
import signal
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np
import board
import busio
from adafruit_pca9685 import PCA9685


DEFAULT_WIDTH = 640
DEFAULT_HEIGHT = 480
DEFAULT_CAMERA_INDEX_LIMIT = 10
PREVIEW_WIDTH = 960
PREVIEW_HEIGHT = 720
PREVIEW_MIN_HEIGHT = 360
PREVIEW_FPS = 12.0
WEB_STREAM_HOST = "0.0.0.0"
WEB_STREAM_PORT = 8082
WEB_STREAM_JPEG_QUALITY = 80
PCA9685_ADDRESS = 0x40
PCA9685_FREQUENCY_HZ = 50
STEP_US = 5
STEP_INTERVAL_SECONDS = 0.25
MOVE_DELAY_MIN_SECONDS = 5.0
MOVE_DELAY_MAX_SECONDS = 12.0
CENTER_PULSE_US = 1500
SERVO_RANGES = {
    3: (1450, 1550),
    4: (1400, 1600),
    5: (1300, 1700),
}


class StopRequested(Exception):
    def __init__(self, signum: int) -> None:
        self.signum = signum
        super().__init__(f"stop requested by signal {signum}")


def video_number(path: Path) -> int:
    match = re.search(r"video(\d+)$", path.name)
    return int(match.group(1)) if match else 9999


def list_video_devices() -> list[Path]:
    devices = sorted(Path("/dev").glob("video*"), key=video_number)
    return [device for device in devices if re.match(r"video\d+$", device.name)]


def usb_video_devices_from_v4l2() -> list[Path]:
    try:
        result = subprocess.run(
            ["v4l2-ctl", "--list-devices"],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return []

    devices: list[Path] = []
    current_is_usb = False
    for raw_line in result.stdout.splitlines():
        line = raw_line.rstrip()
        if not line:
            current_is_usb = False
            continue
        if not raw_line.startswith(("\t", " ")):
            current_is_usb = "usb-" in line.lower()
            continue
        if current_is_usb:
            match = re.search(r"(/dev/video\d+)", line)
            if match:
                devices.append(Path(match.group(1)))
    return devices


def camera_candidates() -> list[int | str]:
    candidates: list[int | str] = []
    candidates.extend(str(device) for device in usb_video_devices_from_v4l2())
    candidates.extend(str(device) for device in list_video_devices() if video_number(device) < DEFAULT_CAMERA_INDEX_LIMIT)
    candidates.extend(range(DEFAULT_CAMERA_INDEX_LIMIT))

    seen: set[int | str] = set()
    unique: list[int | str] = []
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        unique.append(candidate)
    return unique


def parse_camera(value: str | None) -> int | str | None:
    if value is None:
        return None
    if value.isdigit():
        return int(value)
    return value


def open_camera(camera: int | str | None, width: int, height: int) -> cv2.VideoCapture:
    candidates = [camera] if camera is not None else camera_candidates()
    errors: list[str] = []

    for candidate in candidates:
        cap = cv2.VideoCapture(candidate, cv2.CAP_V4L2)
        if not cap.isOpened():
            cap.release()
            errors.append(f"{candidate}: open failed")
            continue

        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        ok, frame = cap.read()
        if ok and frame is not None:
            print(f"camera_device={candidate}", flush=True)
            return cap

        cap.release()
        errors.append(f"{candidate}: read failed")

    raise RuntimeError("No working webcam found. Tried: " + "; ".join(errors))


def pulse_to_duty_cycle(pulse_us: int) -> int:
    period_us = 1_000_000 / PCA9685_FREQUENCY_HZ
    duty_fraction = pulse_us / period_us
    return max(0, min(0xFFFF, round(duty_fraction * 0xFFFF)))


def clamp_pulse(channel: int, pulse_us: int) -> int:
    min_pulse, max_pulse = SERVO_RANGES[channel]
    return max(min_pulse, min(max_pulse, pulse_us))


class ThreeServoRandomMover:
    def __init__(self) -> None:
        self.pca: PCA9685 | None = None
        self.random = random.Random()
        self.current = {channel: CENTER_PULSE_US for channel in SERVO_RANGES}
        self.target = {channel: CENTER_PULSE_US for channel in SERVO_RANGES}
        self.active_channel: int | None = None
        self.next_step_time = 0.0
        self.next_move_time = 0.0
        self.returning_home = False

    def open(self) -> None:
        i2c = busio.I2C(board.SCL, board.SDA)
        self.pca = PCA9685(i2c, address=PCA9685_ADDRESS)
        self.pca.frequency = PCA9685_FREQUENCY_HZ
        print(
            f"PCA9685 enabled address=0x{PCA9685_ADDRESS:02x} frequency_hz={PCA9685_FREQUENCY_HZ}",
            flush=True,
        )

    def write(self, channel: int, pulse_us: int) -> None:
        if self.pca is None:
            return
        pulse_us = clamp_pulse(channel, pulse_us)
        self.current[channel] = pulse_us
        self.pca.channels[channel].duty_cycle = pulse_to_duty_cycle(pulse_us)

    def disable(self) -> None:
        if self.pca is None:
            return
        for channel in SERVO_RANGES:
            self.pca.channels[channel].duty_cycle = 0
            print(f"PWM disabled on channel {channel}.", flush=True)

    def close(self) -> None:
        if self.pca is None:
            return
        self.disable()
        self.pca.deinit()
        self.pca = None

    def step_channel(self, channel: int, now: float) -> bool:
        if now < self.next_step_time:
            return False
        current = self.current[channel]
        target = self.target[channel]
        if current == target:
            return False
        if current < target:
            self.write(channel, min(target, current + STEP_US))
        else:
            self.write(channel, max(target, current - STEP_US))
        self.next_step_time = now + STEP_INTERVAL_SECONDS
        return True

    def home(self, stop_requested) -> bool:
        for channel in SERVO_RANGES:
            self.target[channel] = CENTER_PULSE_US
        print("HOME channels=3,4,5", flush=True)
        while not stop_requested():
            now = time.monotonic()
            moved = False
            for channel in SERVO_RANGES:
                moved = self.step_channel(channel, now) or moved
            if not moved and all(self.current[channel] == CENTER_PULSE_US for channel in SERVO_RANGES):
                self.schedule_next_move(time.monotonic())
                print("HOME complete", flush=True)
                return True
            time.sleep(0.02)
        return False

    def schedule_next_move(self, now: float) -> None:
        self.active_channel = None
        self.returning_home = False
        self.next_move_time = now + self.random.uniform(MOVE_DELAY_MIN_SECONDS, MOVE_DELAY_MAX_SECONDS)

    def choose_move(self, now: float) -> None:
        self.active_channel = self.random.choice(tuple(SERVO_RANGES))
        min_pulse, max_pulse = SERVO_RANGES[self.active_channel]
        self.target[self.active_channel] = self.random.randrange(min_pulse, max_pulse + 1, STEP_US)
        print(
            f"MOVE channel={self.active_channel} target={self.target[self.active_channel]}",
            flush=True,
        )
        self.next_step_time = now

    def update(self, now: float) -> None:
        if self.active_channel is None:
            if now >= self.next_move_time:
                self.choose_move(now)
            return

        channel = self.active_channel
        self.step_channel(channel, now)
        if self.current[channel] != self.target[channel]:
            return

        if self.returning_home:
            self.schedule_next_move(now)
            return

        self.returning_home = True
        self.target[channel] = CENTER_PULSE_US
        print(f"RETURN channel={channel} target={CENTER_PULSE_US}", flush=True)


class MjpegStreamServer:
    def __init__(self, host: str = WEB_STREAM_HOST, port: int = WEB_STREAM_PORT) -> None:
        self.host = host
        self.port = port
        self.frame: np.ndarray | None = None
        self.closed = False
        self.condition = threading.Condition()
        stream = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path in ("/", "/index.html"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(
                        b"<!doctype html><html><head><title>random_three_servo_stream</title></head>"
                        b"<body style='margin:0;background:#111;display:grid;place-items:center;min-height:100vh'>"
                        b"<img src='/stream.mjpg' style='max-width:100vw;max-height:100vh' alt='camera stream'>"
                        b"</body></html>"
                    )
                    return
                if self.path != "/stream.mjpg":
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Age", "0")
                self.send_header("Cache-Control", "no-cache, private")
                self.send_header("Pragma", "no-cache")
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.end_headers()
                while True:
                    with stream.condition:
                        stream.condition.wait_for(lambda: stream.frame is not None or stream.closed, timeout=2.0)
                        if stream.closed:
                            return
                        frame = stream.frame.copy() if stream.frame is not None else None
                    if frame is None:
                        continue
                    ok, encoded = cv2.imencode(
                        ".jpg",
                        frame,
                        [int(cv2.IMWRITE_JPEG_QUALITY), WEB_STREAM_JPEG_QUALITY],
                    )
                    if not ok:
                        continue
                    jpeg = encoded.tobytes()
                    try:
                        self.wfile.write(b"--frame\r\n")
                        self.wfile.write(b"Content-Type: image/jpeg\r\n")
                        self.wfile.write(f"Content-Length: {len(jpeg)}\r\n\r\n".encode("ascii"))
                        self.wfile.write(jpeg)
                        self.wfile.write(b"\r\n")
                    except (BrokenPipeError, ConnectionResetError):
                        return

            def log_message(self, format: str, *args) -> None:  # noqa: A002
                return

        class ThreadingHttpServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        self.httpd = ThreadingHttpServer((self.host, self.port), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="mjpeg-stream", daemon=True)

    def start(self) -> None:
        self.thread.start()
        print(f"web stream: http://{self.host}:{self.port}/", flush=True)

    def update(self, frame: np.ndarray) -> None:
        with self.condition:
            self.frame = frame.copy()
            self.condition.notify_all()

    def close(self) -> None:
        with self.condition:
            self.closed = True
            self.condition.notify_all()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2.0)


def preview_size_for(frame_width: int, frame_height: int) -> tuple[int, int]:
    width = PREVIEW_WIDTH
    height = round(frame_height * (width / frame_width))
    if height > PREVIEW_HEIGHT:
        height = PREVIEW_HEIGHT
        width = round(frame_width * (height / frame_height))
    if height < PREVIEW_MIN_HEIGHT and frame_height >= PREVIEW_MIN_HEIGHT:
        height = PREVIEW_MIN_HEIGHT
        width = round(frame_width * (height / frame_height))
    return width, height


def build_overlay(frame: np.ndarray, fps: float, mover: ThreeServoRandomMover) -> np.ndarray:
    mirrored = cv2.flip(frame, 1)
    preview_size = preview_size_for(frame.shape[1], frame.shape[0])
    overlay = cv2.resize(mirrored, preview_size, interpolation=cv2.INTER_AREA)
    lines = [
        f"FPS {fps:.1f}",
        f"active CH{mover.active_channel}" if mover.active_channel is not None else "waiting",
    ]
    for channel in SERVO_RANGES:
        lines.append(f"CH{channel} {mover.current[channel]}->{mover.target[channel]}us")
    for index, line in enumerate(lines):
        cv2.putText(
            overlay,
            line,
            (8, 24 + 24 * index),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )
    return overlay


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Random slow ch3/ch4/ch5 motion with MJPEG camera stream.")
    parser.add_argument("--camera", default=None, help="Camera index or /dev/videoN path. Default: auto-detect.")
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    parser.add_argument("--height", type=int, default=DEFAULT_HEIGHT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    stop_requested = False
    stop_signal = 0

    def request_stop(signum, frame):  # noqa: ARG001
        nonlocal stop_requested
        nonlocal stop_signal
        stop_requested = True
        stop_signal = signum
        raise StopRequested(signum)

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    cap: cv2.VideoCapture | None = None
    stream: MjpegStreamServer | None = None
    mover = ThreeServoRandomMover()
    try:
        mover.open()
        if not mover.home(lambda: stop_requested):
            print("Home failed or stop requested.", file=sys.stderr, flush=True)
            return 2

        cap = open_camera(parse_camera(args.camera), args.width, args.height)
        stream = MjpegStreamServer()
        stream.start()

        frame_count = 0
        fps_count = 0
        fps = 0.0
        fps_started = time.monotonic()
        last_preview = 0.0

        while not stop_requested:
            ok, frame = cap.read()
            if not ok or frame is None:
                print("camera_read_failed=yes", file=sys.stderr, flush=True)
                time.sleep(0.05)
                continue

            now = time.monotonic()
            mover.update(now)
            frame_count += 1
            fps_count += 1
            elapsed = now - fps_started
            if elapsed >= 1.0:
                fps = fps_count / elapsed
                fps_count = 0
                fps_started = now
                print(
                    f"FPS {fps:.2f} "
                    f"CH3 {mover.current[3]}->{mover.target[3]} "
                    f"CH4 {mover.current[4]}->{mover.target[4]} "
                    f"CH5 {mover.current[5]}->{mover.target[5]}",
                    flush=True,
                )

            if now - last_preview >= 1.0 / PREVIEW_FPS:
                stream.update(build_overlay(frame, fps, mover))
                last_preview = now

        return 0
    except StopRequested:
        code = 128 + stop_signal if stop_signal else 130
        print(f"stop requested; exiting with code {code}", file=sys.stderr, flush=True)
        return code
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        if cap is not None:
            cap.release()
        if stream is not None:
            stream.close()
        mover.close()
        print("camera released", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
