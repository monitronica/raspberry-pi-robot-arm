#!/usr/bin/env python3
"""USB webcam person tracking with optional safe channel 5 following."""

from __future__ import annotations

import argparse
import os
import re
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

import cv2
import numpy as np
import board
import busio
from adafruit_pca9685 import PCA9685

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
try:
    import pygame
except ImportError:  # Preview can still run through OpenCV GUI on desktop installs.
    pygame = None


DEFAULT_WIDTH = 640
DEFAULT_HEIGHT = 480
PREVIEW_WIDTH = 640
PREVIEW_HEIGHT = 480
PREVIEW_MIN_HEIGHT = 360
PREVIEW_FPS = 12.0
DEFAULT_CAMERA_INDEX_LIMIT = 10
MIN_PERSON_CONFIDENCE = 0.35
PCA9685_ADDRESS = 0x40
PCA9685_FREQUENCY_HZ = 50
TRACK_CHANNEL = 5
TRACK_MIN_PULSE_US = 900
TRACK_HOME_PULSE_US = 1500
TRACK_MAX_PULSE_US = 2100
TRACK_UPDATE_SECONDS = 0.015
TRACK_STABILITY_SECONDS = 0.15
TRACK_LOST_HOLD_SECONDS = 1.0
PROFILE_LOST_HOLD_SECONDS = 2.0
POSITION_SMOOTHING = 0.7
VELOCITY_LEAD_SECONDS = 0.25
MAX_VELOCITY_LEAD_PX = 80
TRACK_TARGET_X_OFFSET_PX = 0
TRACK_DIRECTION_SIGN = 1
TRACK_HORIZONTAL_DEADZONE_PX = 50
VERTICAL_CHANNEL = 4
VERTICAL_MIN_PULSE_US = 1000
VERTICAL_HOME_PULSE_US = 1500
VERTICAL_MAX_PULSE_US = 1700
VERTICAL_UPDATE_SECONDS = 0.03
VERTICAL_DIRECTION_SIGN = 1
VERTICAL_DEADZONE_PX = 60
UPRIGHT_SUPPORT_CHANNEL = 3
UPRIGHT_SUPPORT_HOME_PULSE_US = 1500
UPRIGHT_SUPPORT_HOLD_PULSE_US = 1600
UPRIGHT_SUPPORT_MIN_PULSE_US = 1500
UPRIGHT_SUPPORT_MAX_PULSE_US = 1600
UPRIGHT_SUPPORT_STEP_US = 5
UPRIGHT_SUPPORT_UPDATE_SECONDS = 0.15
FOLLOW_CHANNEL2 = 2
FOLLOW_CHANNEL2_MIN_PULSE_US = 1400
FOLLOW_CHANNEL2_HOME_PULSE_US = 1500
FOLLOW_CHANNEL2_MAX_PULSE_US = 1600
FOLLOW_CHANNEL2_STEP_US = 5
FOLLOW_CHANNEL2_UPDATE_SECONDS = 0.30
GREET_COOLDOWN = 15.0
HAPPY_DANCE_COOLDOWN = 30.0
GREET_TRIGGER_SECONDS = 3.0
HAPPY_DANCE_TRIGGER_SECONDS = 10.0
BEHAVIOR_STEP_US = 5
BEHAVIOR_STEP_SECONDS = 0.15
BEHAVIOR_LOST_HOLD_SECONDS = 1.0
GREET_CHANNELS = (0, 1, 2)
GREET_MIN_PULSE_US = 1300
GREET_MAX_PULSE_US = 1750
HAPPY_DANCE_MIN_PULSE_US = 1400
HAPPY_DANCE_MAX_PULSE_US = 1650
HOME_POLL_SECONDS = 0.05


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

    for device in usb_video_devices_from_v4l2():
        candidates.append(str(device))

    for device in list_video_devices():
        if video_number(device) < DEFAULT_CAMERA_INDEX_LIMIT:
            candidates.append(str(device))

    for index in range(DEFAULT_CAMERA_INDEX_LIMIT):
        candidates.append(index)

    seen: set[int | str] = set()
    unique: list[int | str] = []
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        unique.append(candidate)
    return unique


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


def create_person_detector() -> cv2.HOGDescriptor:
    detector = cv2.HOGDescriptor()
    detector.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
    return detector


def detect_people(detector: cv2.HOGDescriptor, frame: np.ndarray) -> list[tuple[int, int, int, int]]:
    scale = 1.0
    detect_frame = frame
    height, width = frame.shape[:2]
    if width > 640:
        scale = 640.0 / width
        detect_frame = cv2.resize(frame, (640, int(height * scale)))

    rects, weights = detector.detectMultiScale(
        detect_frame,
        winStride=(8, 8),
        padding=(8, 8),
        scale=1.05,
    )

    people: list[tuple[float, tuple[int, int, int, int]]] = []
    for rect, weight in zip(rects, weights):
        confidence = float(weight)
        if confidence < MIN_PERSON_CONFIDENCE:
            continue
        x, y, w, h = [int(v / scale) for v in rect]
        area = w * h
        people.append((area + confidence, (x, y, w, h)))

    if not people:
        return []
    people.sort(key=lambda item: item[0], reverse=True)
    return [person_box for _, person_box in people]


def detect_one_person(detector: cv2.HOGDescriptor, frame: np.ndarray) -> tuple[int, int, int, int] | None:
    people = detect_people(detector, frame)
    if not people:
        return None
    return people[0]


def upper_body_tracking_point(person_box: tuple[int, int, int, int]) -> tuple[int, int]:
    x, y, w, h = person_box
    return (x + w // 2, y + round(h * 0.35))


def box_area(person_box: tuple[int, int, int, int]) -> int:
    return person_box[2] * person_box[3]


class PersonTargetSelector:
    def __init__(self) -> None:
        self.box: tuple[int, int, int, int] | None = None
        self.point: tuple[int, int] | None = None
        self.predicted_point: tuple[int, int] | None = None
        self.velocity = (0.0, 0.0)
        self.chosen_index: int | None = None
        self.last_seen_time = 0.0
        self.lost_events = 0
        self.was_lost = False
        self.state = "LOST"
        self.smoothed_point: tuple[float, float] | None = None
        self.previous_gray: np.ndarray | None = None
        self.profile_loss_started = 0.0
        self.predicted_box: tuple[int, int, int, int] | None = None

    def _track_box_with_flow(
        self, frame: np.ndarray, now: float
    ) -> tuple[tuple[int, int, int, int] | None, tuple[int, int] | None]:
        if self.box is None or self.point is None or self.previous_gray is None:
            return None, None
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        x, y, w, h = self.box
        points = []
        for fx in (0.25, 0.5, 0.75):
            for fy in (0.2, 0.35, 0.5, 0.7, 0.85):
                points.append((x + w * fx, y + h * fy))
        previous_points = np.float32(points).reshape(-1, 1, 2)
        next_points, status, _ = cv2.calcOpticalFlowPyrLK(
            self.previous_gray, gray, previous_points, None,
            winSize=(15, 15), maxLevel=2,
            criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 10, 0.03),
        )
        if next_points is None or status is None:
            return None, None
        valid = status.reshape(-1).astype(bool)
        if int(valid.sum()) < 4:
            return None, None
        displacement = np.median(
            next_points.reshape(-1, 2)[valid] - previous_points.reshape(-1, 2)[valid],
            axis=0,
        )
        dx, dy = float(displacement[0]), float(displacement[1])
        tracked_box = (
            max(0, round(x + dx)), max(0, round(y + dy)), w, h
        )
        tracked_point = upper_body_tracking_point(tracked_box)
        dt = max(0.001, now - self.last_seen_time)
        measured_vx = (tracked_point[0] - self.point[0]) / dt
        measured_vy = (tracked_point[1] - self.point[1]) / dt
        self.velocity = (
            0.75 * self.velocity[0] + 0.25 * measured_vx,
            0.75 * self.velocity[1] + 0.25 * measured_vy,
        )
        self.box = tracked_box
        self.predicted_box = tracked_box
        self.point = tracked_point
        self.last_seen_time = now
        self.state = "TRACKING PROFILE"
        self.was_lost = True
        self.previous_gray = gray
        return tracked_box, tracked_point

    def update(
        self,
        person_boxes: list[tuple[int, int, int, int]],
        now: float,
        frame: np.ndarray | None = None,
    ) -> tuple[tuple[int, int, int, int] | None, tuple[int, int] | None]:
        if self.point is not None and self.last_seen_time:
            dt = max(0.0, now - self.last_seen_time)
            predicted_x = round(self.point[0] + self.velocity[0] * dt)
            predicted_y = round(self.point[1] + self.velocity[1] * dt)
            self.predicted_point = (predicted_x, predicted_y)
        else:
            self.predicted_point = None

        if not person_boxes:
            self.chosen_index = None
            if not self.was_lost:
                self.lost_events += 1
            if self.profile_loss_started == 0.0:
                self.profile_loss_started = now
            if frame is not None and self.box is not None and now - self.profile_loss_started <= PROFILE_LOST_HOLD_SECONDS:
                tracked = self._track_box_with_flow(frame, now)
                if tracked[0] is not None:
                    self.state = "TRACKING PROFILE"
                    return tracked
            if self.box is not None and now - self.profile_loss_started <= PROFILE_LOST_HOLD_SECONDS:
                self.was_lost = True
                self.state = "PREDICTING"
                self.predicted_box = self.box
                return self.box, self.point
            self.box = None
            self.point = None
            self.smoothed_point = None
            self.velocity = (0.0, 0.0)
            self.was_lost = True
            self.state = "LOST"
            self.predicted_box = None
            self.profile_loss_started = 0.0
            if frame is not None:
                self.previous_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            return None, None

        if self.box is None or self.point is None or self.predicted_point is None:
            chosen_index = 0
        else:
            previous_area = max(1, box_area(self.box))
            predicted_x, predicted_y = self.predicted_point

            def score_candidate(index_and_box: tuple[int, tuple[int, int, int, int]]) -> float:
                index, candidate = index_and_box
                point = upper_body_tracking_point(candidate)
                dx = point[0] - predicted_x
                dy = point[1] - predicted_y
                distance = (dx * dx + dy * dy) ** 0.5
                size_delta = abs(box_area(candidate) - previous_area) / previous_area
                current_bias = 0.0 if index == self.chosen_index else 15.0
                return distance + 80.0 * size_delta + current_bias

            chosen_index = min(enumerate(person_boxes), key=score_candidate)[0]

        chosen_box = person_boxes[chosen_index]
        measured_point = upper_body_tracking_point(chosen_box)
        if self.smoothed_point is None:
            self.smoothed_point = (float(measured_point[0]), float(measured_point[1]))
        else:
            self.smoothed_point = (
                POSITION_SMOOTHING * measured_point[0] + (1 - POSITION_SMOOTHING) * self.smoothed_point[0],
                POSITION_SMOOTHING * measured_point[1] + (1 - POSITION_SMOOTHING) * self.smoothed_point[1],
            )
        chosen_point = (round(self.smoothed_point[0]), round(self.smoothed_point[1]))
        if self.point is not None and self.last_seen_time:
            dt = max(0.001, now - self.last_seen_time)
            measured_vx = (chosen_point[0] - self.point[0]) / dt
            measured_vy = (chosen_point[1] - self.point[1]) / dt
            self.velocity = (
                0.6 * self.velocity[0] + 0.4 * measured_vx,
                0.6 * self.velocity[1] + 0.4 * measured_vy,
            )
        self.box = chosen_box
        self.point = chosen_point
        self.chosen_index = chosen_index
        self.last_seen_time = now
        self.was_lost = False
        self.state = "DETECTED"
        self.predicted_box = chosen_box
        self.profile_loss_started = 0.0
        if frame is not None:
            self.previous_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        return chosen_box, chosen_point

    def control_point(self) -> tuple[int, int] | None:
        if self.point is None:
            return None
        lead_x = max(-MAX_VELOCITY_LEAD_PX, min(MAX_VELOCITY_LEAD_PX, self.velocity[0] * VELOCITY_LEAD_SECONDS))
        lead_y = max(-MAX_VELOCITY_LEAD_PX, min(MAX_VELOCITY_LEAD_PX, self.velocity[1] * VELOCITY_LEAD_SECONDS))
        return round(self.point[0] + lead_x), round(self.point[1] + lead_y)

    def predicted_text(self) -> str:
        if self.predicted_point is None:
            return "none"
        return f"{self.predicted_point[0]} {self.predicted_point[1]}"

    def velocity_text(self) -> str:
        return f"{self.velocity[0]:.1f} {self.velocity[1]:.1f}"


def display_available() -> bool:
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def opencv_gui_available() -> bool:
    try:
        build_info = cv2.getBuildInformation()
    except cv2.error:
        return False
    for line in build_info.splitlines():
        if line.strip().startswith("GUI:"):
            return "NONE" not in line.upper()
    return True


def draw_overlay(
    frame: np.ndarray,
    person_box: tuple[int, int, int, int] | None,
    image_center: tuple[int, int],
    tracking_point: tuple[int, int] | None,
    fps: float,
    track_status: str = "TRACKING OFF",
    channel5_current_pulse: int | None = None,
    channel5_target_pulse: int | None = None,
    channel4_current_pulse: int | None = None,
    channel4_target_pulse: int | None = None,
    preview_size: tuple[int, int] | None = None,
    predicted_box: tuple[int, int, int, int] | None = None,
    velocity: tuple[float, float] | None = None,
    tracking_state: str = "LOST",
) -> np.ndarray:
    if preview_size is None:
        overlay = frame.copy()
        scale_x = 1.0
        scale_y = 1.0
    else:
        preview_width, preview_height = preview_size
        overlay = cv2.resize(frame, (preview_width, preview_height), interpolation=cv2.INTER_AREA)
        frame_height, frame_width = frame.shape[:2]
        scale_x = preview_width / frame_width
        scale_y = preview_height / frame_height

    target_x = image_center[0] + TRACK_TARGET_X_OFFSET_PX
    cx = int(image_center[0] * scale_x)
    cy = int(image_center[1] * scale_y)
    tx = int(target_x * scale_x)
    cv2.drawMarker(overlay, (cx, cy), (255, 255, 255), cv2.MARKER_CROSS, 28, 2)
    cv2.line(overlay, (tx, 0), (tx, overlay.shape[0] - 1), (255, 255, 0), 1)
    if person_box is not None and tracking_point is not None:
        x, y, w, h = person_box
        x = int(x * scale_x)
        y = int(y * scale_y)
        w = int(w * scale_x)
        h = int(h * scale_y)
        px = int(tracking_point[0] * scale_x)
        py = int(tracking_point[1] * scale_y)
        cv2.rectangle(overlay, (x, y), (x + w, y + h), (0, 255, 0), 2)
        cv2.circle(overlay, (px, py), 9, (0, 0, 255), -1)
        cv2.circle(overlay, (px, py), 13, (255, 255, 255), 2)
        tracking_text = "TRACKING ON"
        tracking_color = (0, 255, 0)
    else:
        tracking_text = "TRACKING OFF"
        tracking_color = (0, 0, 255)

    if predicted_box is not None and predicted_box != person_box:
        x, y, w, h = predicted_box
        x = int(x * scale_x)
        y = int(y * scale_y)
        w = int(w * scale_x)
        h = int(h * scale_y)
        cv2.rectangle(overlay, (x, y), (x + w, y + h), (0, 200, 255), 2)
    if tracking_point is not None and velocity is not None:
        px = int(tracking_point[0] * scale_x)
        py = int(tracking_point[1] * scale_y)
        vx = int(px + max(-MAX_VELOCITY_LEAD_PX, min(MAX_VELOCITY_LEAD_PX, velocity[0] * VELOCITY_LEAD_SECONDS)) * scale_x)
        vy = int(py + max(-MAX_VELOCITY_LEAD_PX, min(MAX_VELOCITY_LEAD_PX, velocity[1] * VELOCITY_LEAD_SECONDS)) * scale_y)
        cv2.arrowedLine(overlay, (px, py), (vx, vy), (0, 165, 255), 2, tipLength=0.25)

    cv2.putText(overlay, tracking_text, (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.55, tracking_color, 2, cv2.LINE_AA)
    cv2.putText(overlay, f"FPS {fps:.1f}", (8, 46), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 2, cv2.LINE_AA)
    cv2.putText(overlay, tracking_state, (8, 70), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 0), 2, cv2.LINE_AA)
    if channel5_current_pulse is not None and channel5_target_pulse is not None:
        pulse_text = f"CH5 {channel5_current_pulse}->{channel5_target_pulse}us"
        cv2.putText(overlay, pulse_text, (8, 94), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 2, cv2.LINE_AA)
    if channel4_current_pulse is not None and channel4_target_pulse is not None:
        pulse_text = f"CH4 {channel4_current_pulse}->{channel4_target_pulse}us"
        cv2.putText(overlay, pulse_text, (8, 118), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 2, cv2.LINE_AA)
    return overlay


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


class PreviewWindow:
    def __init__(self) -> None:
        self.backend = "none"
        self.preview_size: tuple[int, int] | None = None
        self.pygame_display = None
        self.framebuffer_fd: int | None = None
        self.framebuffer_width = 0
        self.framebuffer_height = 0
        self.framebuffer_stride = 0
        self.framebuffer_bpp = 0
        self.framebuffer_offset_x = 0
        self.framebuffer_offset_y = 0

    def open(self, frame_width: int, frame_height: int) -> bool:
        self.preview_size = preview_size_for(frame_width, frame_height)
        if display_available() and opencv_gui_available():
            self.backend = "opencv"
            print(f"preview backend: opencv window {self.preview_size[0]}x{self.preview_size[1]}", flush=True)
            return True

        if not display_available() and self.open_framebuffer():
            return True

        if pygame is None:
            print("preview disabled: pygame is not installed", file=sys.stderr, flush=True)
            return False

        os.environ.setdefault("SDL_VIDEO_CENTERED", "1")
        if not display_available():
            os.environ.setdefault("SDL_VIDEODRIVER", "kmsdrm")
            os.environ.setdefault("SDL_FBDEV", "/dev/fb0")

        try:
            pygame.init()
            pygame.display.init()
            self.pygame_display = pygame.display.set_mode(self.preview_size)
            pygame.display.set_caption("person_follow_camera")
            pygame.mouse.set_visible(False)
            self.backend = "pygame"
            driver = pygame.display.get_driver()
            print(f"preview backend: pygame/{driver} {self.preview_size[0]}x{self.preview_size[1]}", flush=True)
            return True
        except Exception as exc:
            print(f"preview disabled: pygame display failed: {exc}", file=sys.stderr, flush=True)
            self.close()
            return False

    def open_framebuffer(self) -> bool:
        if self.preview_size is None:
            return False
        try:
            virtual_size = Path("/sys/class/graphics/fb0/virtual_size").read_text().strip()
            width_text, height_text = virtual_size.split(",", 1)
            self.framebuffer_width = int(width_text)
            self.framebuffer_height = int(height_text)
            self.framebuffer_bpp = int(Path("/sys/class/graphics/fb0/bits_per_pixel").read_text().strip())
            self.framebuffer_stride = int(Path("/sys/class/graphics/fb0/stride").read_text().strip())
            if self.framebuffer_bpp != 16:
                print(
                    f"preview disabled: framebuffer bpp {self.framebuffer_bpp} is not supported",
                    file=sys.stderr,
                    flush=True,
                )
                return False
            self.framebuffer_fd = os.open("/dev/fb0", os.O_RDWR | os.O_SYNC)
            preview_width, preview_height = self.preview_size
            self.framebuffer_offset_x = max(0, (self.framebuffer_width - preview_width) // 2)
            self.framebuffer_offset_y = max(0, (self.framebuffer_height - preview_height) // 2)
            self.backend = "framebuffer"
            print(
                "preview backend: framebuffer /dev/fb0 "
                f"{preview_width}x{preview_height} on {self.framebuffer_width}x{self.framebuffer_height}",
                flush=True,
            )
            return True
        except Exception as exc:
            print(f"preview disabled: framebuffer failed: {exc}", file=sys.stderr, flush=True)
            self.close()
            return False

    def show(
        self,
        frame: np.ndarray,
        person_box: tuple[int, int, int, int] | None,
        tracking_point: tuple[int, int] | None,
        fps: float,
        track_status: str,
        channel5_current_pulse: int | None,
        channel5_target_pulse: int | None,
        channel4_current_pulse: int | None,
        channel4_target_pulse: int | None,
        predicted_box: tuple[int, int, int, int] | None = None,
        velocity: tuple[float, float] | None = None,
        tracking_state: str = "LOST",
    ) -> bool:
        if self.preview_size is None:
            return True

        height, width = frame.shape[:2]
        image_center = (width // 2, height // 2)
        overlay = draw_overlay(
            frame,
            person_box,
            image_center,
            tracking_point,
            fps,
            track_status,
            channel5_current_pulse,
            channel5_target_pulse,
            channel4_current_pulse,
            channel4_target_pulse,
            self.preview_size,
            predicted_box,
            velocity,
            tracking_state,
        )

        if self.backend == "opencv":
            try:
                cv2.imshow("person_follow_camera", overlay)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    print("preview closed by Q", flush=True)
                    return False
            except cv2.error as exc:
                print(f"preview disabled: {exc}", file=sys.stderr, flush=True)
                self.close()
            return True

        if self.backend == "pygame" and self.pygame_display is not None:
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    print("preview closed", flush=True)
                    return False
                if event.type == pygame.KEYDOWN and event.key == pygame.K_q:
                    print("preview closed by Q", flush=True)
                    return False

            rgb = cv2.cvtColor(overlay, cv2.COLOR_BGR2RGB)
            surface = pygame.surfarray.make_surface(np.swapaxes(rgb, 0, 1))
            self.pygame_display.blit(surface, (0, 0))
            pygame.display.flip()
        elif self.backend == "framebuffer" and self.framebuffer_fd is not None:
            self.write_framebuffer(overlay)
        return True

    def write_framebuffer(self, bgr_image: np.ndarray) -> None:
        rgb = cv2.cvtColor(bgr_image, cv2.COLOR_BGR2RGB)
        r = (rgb[:, :, 0].astype(np.uint16) >> 3) << 11
        g = (rgb[:, :, 1].astype(np.uint16) >> 2) << 5
        b = rgb[:, :, 2].astype(np.uint16) >> 3
        rgb565 = (r | g | b).astype("<u2")
        row_bytes = rgb565.shape[1] * 2
        base_offset = (
            self.framebuffer_offset_y * self.framebuffer_stride
            + self.framebuffer_offset_x * 2
        )
        for row_index, row in enumerate(rgb565):
            os.pwrite(
                self.framebuffer_fd,
                row.tobytes(),
                base_offset + row_index * self.framebuffer_stride,
            )

    def close(self) -> None:
        if self.backend == "opencv":
            cv2.destroyWindow("person_follow_camera")
        elif self.backend == "pygame" and pygame is not None:
            pygame.display.quit()
            pygame.quit()
        if self.framebuffer_fd is not None:
            os.close(self.framebuffer_fd)
        self.backend = "none"
        self.pygame_display = None
        self.framebuffer_fd = None


def print_status(
    width: int,
    height: int,
    fps: float,
    person_box: tuple[int, int, int, int] | None,
    tracking_point: tuple[int, int] | None,
    track_status: str = "TRACKING OFF",
    channel5_current_pulse: int | None = None,
    channel5_target_pulse: int | None = None,
    channel4_current_pulse: int | None = None,
    channel4_target_pulse: int | None = None,
    channel3_current_pulse: int | None = None,
    channel2_current_pulse: int | None = None,
    channel2_target_pulse: int | None = None,
    servo_steps_last_second: int | None = None,
    selected_step_us: int | None = None,
    vertical_step_us: int | None = None,
    tracking_state: str | None = None,
    vertical_tracking_state: str | None = None,
    predicted_point: tuple[int, int] | None = None,
    velocity: tuple[float, float] | None = None,
    chosen_person_index: int | None = None,
    lost_events: int | None = None,
    behavior_state: str | None = None,
    engagement_time: float | None = None,
    greet_cooldown_remaining: float | None = None,
    dance_cooldown_remaining: float | None = None,
) -> None:
    image_center = (width // 2, height // 2)
    target_x = image_center[0] + TRACK_TARGET_X_OFFSET_PX
    print(f"camera resolution: {width}x{height}", flush=True)
    print(f"measured FPS: {fps:.2f}", flush=True)
    if person_box is None or tracking_point is None:
        print("NO PERSON", flush=True)
        print("person box: none", flush=True)
        print("upper-body tracking point X/Y: none", flush=True)
        horizontal_error = "none"
        vertical_error = "none"
        person_x = "none"
    else:
        print("PERSON DETECTED", flush=True)
        x, y, w, h = person_box
        print(f"person box: x={x} y={y} w={w} h={h}", flush=True)
        print(f"upper-body tracking point X/Y: {tracking_point[0]} {tracking_point[1]}", flush=True)
        horizontal_error = str(tracking_point[0] - target_x)
        vertical_error = str(tracking_point[1] - image_center[1])
        person_x = str(tracking_point[0])
    print(f"image center X/Y: {image_center[0]} {image_center[1]}", flush=True)
    print(f"person X: {person_x}", flush=True)
    print(f"target X: {target_x}", flush=True)
    print(f"horizontal error: {horizontal_error}", flush=True)
    print(f"vertical error: {vertical_error}", flush=True)
    print(f"track status: {track_status}", flush=True)
    if person_box is not None:
        x, y, w, h = person_box
        print(f"target box: x={x} y={y} w={w} h={h}", flush=True)
    else:
        print("target box: none", flush=True)
    if predicted_point is not None:
        print(f"predicted X/Y: {predicted_point[0]} {predicted_point[1]}", flush=True)
    else:
        print("predicted X/Y: none", flush=True)
    if velocity is not None:
        print(f"velocity X/Y: {velocity[0]:.1f} {velocity[1]:.1f}", flush=True)
    if chosen_person_index is not None:
        print(f"chosen person index: {chosen_person_index}", flush=True)
    else:
        print("chosen person index: none", flush=True)
    if selected_step_us is not None:
        print(f"selected step_us: {selected_step_us}", flush=True)
    if vertical_step_us is not None:
        print(f"vertical step_us: {vertical_step_us}", flush=True)
    if channel5_current_pulse is not None and channel5_target_pulse is not None:
        print(f"channel 5 current pulse: {channel5_current_pulse}", flush=True)
        print(f"channel 5 target pulse: {channel5_target_pulse}", flush=True)
        print(f"ch5 pulse: {channel5_current_pulse}", flush=True)
    if channel4_current_pulse is not None and channel4_target_pulse is not None:
        print(f"channel 4 current pulse: {channel4_current_pulse}", flush=True)
        print(f"channel 4 target pulse: {channel4_target_pulse}", flush=True)
        print(f"channel 4 clamped target: {channel4_target_pulse}", flush=True)
        print(f"ch4 pulse: {channel4_current_pulse}", flush=True)
    if channel3_current_pulse is not None:
        print(f"channel 3 current pulse: {channel3_current_pulse}", flush=True)
        print(f"ch3 pulse: {channel3_current_pulse}", flush=True)
    if channel2_current_pulse is not None and channel2_target_pulse is not None:
        print(f"channel 2 current pulse: {channel2_current_pulse}", flush=True)
        print(f"channel 2 target pulse: {channel2_target_pulse}", flush=True)
        print(f"ch2 pulse: {channel2_current_pulse}", flush=True)
    if servo_steps_last_second is not None:
        print(f"servo_steps_last_second: {servo_steps_last_second}", flush=True)
    if tracking_state is not None:
        print(f"tracking state: {tracking_state}", flush=True)
    if vertical_tracking_state is not None:
        print(f"vertical tracking state: {vertical_tracking_state}", flush=True)
    if lost_events is not None:
        print(f"lost target events: {lost_events}", flush=True)
    if behavior_state is not None:
        print(f"behavior state: {behavior_state}", flush=True)
    if engagement_time is not None:
        print(f"person engagement time: {engagement_time:.1f}s", flush=True)
    if greet_cooldown_remaining is not None:
        print(f"greet cooldown remaining: {greet_cooldown_remaining:.1f}s", flush=True)
    if dance_cooldown_remaining is not None:
        print(f"dance cooldown remaining: {dance_cooldown_remaining:.1f}s", flush=True)


def track_target_x(width: int) -> int:
    return width // 2 + TRACK_TARGET_X_OFFSET_PX


def track_status_for(tracking_point: tuple[int, int] | None, width: int) -> tuple[str, int | None]:
    if tracking_point is None:
        return "NO PERSON", None
    horizontal_error = tracking_point[0] - track_target_x(width)
    if abs(horizontal_error) <= TRACK_HORIZONTAL_DEADZONE_PX:
        return "CENTERED", horizontal_error
    if horizontal_error < 0:
        return "TRACK LEFT", horizontal_error
    return "TRACK RIGHT", horizontal_error


def vertical_status_for(tracking_point: tuple[int, int] | None, height: int) -> tuple[str, int | None]:
    if tracking_point is None:
        return "NO PERSON", None
    vertical_error = tracking_point[1] - height // 2
    if abs(vertical_error) <= VERTICAL_DEADZONE_PX:
        return "CENTERED", vertical_error
    if vertical_error < 0:
        return "TRACK UP", vertical_error
    return "TRACK DOWN", vertical_error


def clamp_track_pulse(pulse_us: int) -> int:
    return max(TRACK_MIN_PULSE_US, min(TRACK_MAX_PULSE_US, pulse_us))


def clamp_vertical_pulse(pulse_us: int) -> int:
    return max(VERTICAL_MIN_PULSE_US, min(VERTICAL_MAX_PULSE_US, pulse_us))


def clamp_upright_support_pulse(pulse_us: int) -> int:
    return max(UPRIGHT_SUPPORT_MIN_PULSE_US, min(UPRIGHT_SUPPORT_MAX_PULSE_US, pulse_us))


def clamp_follow_channel2_pulse(pulse_us: int) -> int:
    return max(FOLLOW_CHANNEL2_MIN_PULSE_US, min(FOLLOW_CHANNEL2_MAX_PULSE_US, pulse_us))


def track_pulse_to_duty_cycle(pulse_us: int) -> int:
    period_us = 1_000_000 / PCA9685_FREQUENCY_HZ
    duty_fraction = pulse_us / period_us
    return max(0, min(0xFFFF, round(duty_fraction * 0xFFFF)))


def tracking_step_us(horizontal_error: int | None) -> int:
    if horizontal_error is None:
        return 0
    abs_error = abs(horizontal_error)
    if abs_error <= TRACK_HORIZONTAL_DEADZONE_PX:
        return 0
    if abs_error <= 100:
        return 5
    if abs_error <= 180:
        return 10
    return 20


def vertical_step_us(vertical_error: int | None) -> int:
    if vertical_error is None:
        return 0
    abs_error = abs(vertical_error)
    if abs_error <= VERTICAL_DEADZONE_PX:
        return 0
    if abs_error <= 120:
        return 5
    if abs_error <= 200:
        return 8
    return 12


class Channel5Tracker:
    def __init__(self) -> None:
        self.pca: PCA9685 | None = None
        self.current_pulse = TRACK_HOME_PULSE_US
        self.target_pulse = TRACK_HOME_PULSE_US
        self.next_step_time = 0.0
        self.step_count = 0
        self.steps_last_second = 0
        self.step_window_started = time.monotonic()
        self.selected_step_us = 0
        self.tracking_state = "NO PERSON"
        self.pending_side: int | None = None
        self.pending_side_started = 0.0
        self.active_side: int | None = None
        self.last_seen_time = 0.0

    def open(self) -> None:
        i2c = busio.I2C(board.SCL, board.SDA)
        self.pca = PCA9685(i2c, address=PCA9685_ADDRESS)
        self.pca.frequency = PCA9685_FREQUENCY_HZ
        print(
            f"TRACK PCA9685 enabled address=0x{PCA9685_ADDRESS:02x} "
            f"frequency_hz={PCA9685_FREQUENCY_HZ} channel={TRACK_CHANNEL}",
            flush=True,
        )
        self.disable()

    def home(self) -> None:
        self.current_pulse = TRACK_HOME_PULSE_US
        self.target_pulse = TRACK_HOME_PULSE_US
        duty_cycle, readback = self.write_pulse(self.current_pulse)
        print(
            f"HOME channel={TRACK_CHANNEL} current_pulse={self.current_pulse} "
            f"target_pulse={self.target_pulse} duty_cycle={duty_cycle} readback={readback}",
            flush=True,
        )

    def update(self, status: str, horizontal_error: int | None) -> None:
        now = time.monotonic()
        self.selected_step_us = tracking_step_us(horizontal_error)

        if status == "NO PERSON":
            if not self.last_seen_time or now - self.last_seen_time >= TRACK_LOST_HOLD_SECONDS:
                self.pending_side = None
                self.active_side = None
            self.target_pulse = self.current_pulse
            self.tracking_state = "NO PERSON"
            self.print_hold_decision(status, now)
            return

        if status == "CENTERED" or horizontal_error is None or self.selected_step_us == 0:
            self.tracking_state = "CENTERED"
            self.pending_side = None
            self.active_side = None
            self.last_seen_time = now
            self.target_pulse = self.current_pulse
            self.print_hold_decision("CENTERED", now)
            return

        self.last_seen_time = now
        side = -1 if horizontal_error < 0 else 1
        if self.active_side != side:
            if self.pending_side != side:
                self.pending_side = side
                self.pending_side_started = now
            if now - self.pending_side_started < TRACK_STABILITY_SECONDS:
                self.tracking_state = "STABILIZING LEFT" if side < 0 else "STABILIZING RIGHT"
                self.target_pulse = self.current_pulse
                self.print_hold_decision(self.tracking_state, now)
                return
            self.active_side = side

        self.tracking_state = "TRACK LEFT" if side < 0 else "TRACK RIGHT"
        if now < self.next_step_time:
            return

        if side < 0:
            self.target_pulse = clamp_track_pulse(
                self.current_pulse - TRACK_DIRECTION_SIGN * self.selected_step_us
            )
        else:
            self.target_pulse = clamp_track_pulse(
                self.current_pulse + TRACK_DIRECTION_SIGN * self.selected_step_us
            )

        if self.target_pulse != self.current_pulse:
            previous_pulse = self.current_pulse
            self.current_pulse = self.target_pulse
            duty_cycle, readback = self.write_pulse(self.current_pulse)
            self.step_count += 1
        self.next_step_time = now + TRACK_UPDATE_SECONDS

    def print_hold_decision(self, status: str, now: float) -> None:
        if now < self.next_step_time:
            return
        self.next_step_time = now + TRACK_UPDATE_SECONDS

    def update_step_rate(self, now: float) -> None:
        elapsed = now - self.step_window_started
        if elapsed >= 1.0:
            self.steps_last_second = round(self.step_count / elapsed)
            self.step_count = 0
            self.step_window_started = now

    def write_pulse(self, pulse_us: int) -> tuple[int, int]:
        if self.pca is None:
            return 0, 0
        duty_cycle = track_pulse_to_duty_cycle(pulse_us)
        self.pca.channels[TRACK_CHANNEL].duty_cycle = duty_cycle
        readback = self.pca.channels[TRACK_CHANNEL].duty_cycle
        return duty_cycle, readback

    def duty_readback(self) -> int:
        if self.pca is None:
            return 0
        return int(self.pca.channels[TRACK_CHANNEL].duty_cycle)

    def disable(self) -> None:
        if self.pca is not None:
            self.pca.channels[TRACK_CHANNEL].duty_cycle = 0
            print(f"PWM disabled on channel {TRACK_CHANNEL}.", flush=True)

    def close(self) -> None:
        if self.pca is not None:
            self.disable()
            self.pca.deinit()
            self.pca = None


class Channel4VerticalTracker:
    def __init__(self, pca: PCA9685) -> None:
        self.pca = pca
        self.current_pulse = VERTICAL_HOME_PULSE_US
        self.target_pulse = VERTICAL_HOME_PULSE_US
        self.next_step_time = 0.0
        self.selected_step_us = 0
        self.tracking_state = "NO PERSON"

    def home(self) -> None:
        self.current_pulse = VERTICAL_HOME_PULSE_US
        self.target_pulse = VERTICAL_HOME_PULSE_US
        duty_cycle, readback = self.write_pulse(self.current_pulse)
        print(
            f"HOME channel={VERTICAL_CHANNEL} current_pulse={self.current_pulse} "
            f"target_pulse={self.target_pulse} duty_cycle={duty_cycle} readback={readback}",
            flush=True,
        )

    def update(self, status: str, vertical_error: int | None) -> None:
        now = time.monotonic()
        self.selected_step_us = vertical_step_us(vertical_error)

        if status == "NO PERSON":
            self.tracking_state = "NO PERSON"
            self.target_pulse = self.current_pulse
            self.next_step_time = max(self.next_step_time, now + VERTICAL_UPDATE_SECONDS)
            return

        if status == "CENTERED" or vertical_error is None or self.selected_step_us == 0:
            self.tracking_state = "CENTERED"
            self.target_pulse = self.current_pulse
            self.next_step_time = max(self.next_step_time, now + VERTICAL_UPDATE_SECONDS)
            return

        self.tracking_state = status
        if now < self.next_step_time:
            return

        if status == "TRACK UP":
            self.target_pulse = clamp_vertical_pulse(
                self.current_pulse - VERTICAL_DIRECTION_SIGN * self.selected_step_us
            )
        else:
            self.target_pulse = clamp_vertical_pulse(
                self.current_pulse + VERTICAL_DIRECTION_SIGN * self.selected_step_us
            )

        if self.target_pulse != self.current_pulse:
            self.current_pulse = self.target_pulse
            self.write_pulse(self.current_pulse)
        self.next_step_time = now + VERTICAL_UPDATE_SECONDS

    def write_pulse(self, pulse_us: int) -> tuple[int, int]:
        pulse_us = clamp_vertical_pulse(pulse_us)
        duty_cycle = track_pulse_to_duty_cycle(pulse_us)
        self.pca.channels[VERTICAL_CHANNEL].duty_cycle = duty_cycle
        readback = self.pca.channels[VERTICAL_CHANNEL].duty_cycle
        return duty_cycle, readback

    def disable(self) -> None:
        self.pca.channels[VERTICAL_CHANNEL].duty_cycle = 0
        print(f"PWM disabled on channel {VERTICAL_CHANNEL}.", flush=True)


class Channel3UprightSupport:
    def __init__(self, pca: PCA9685) -> None:
        self.pca = pca
        self.current_pulse = UPRIGHT_SUPPORT_HOME_PULSE_US
        self.target_pulse = UPRIGHT_SUPPORT_HOME_PULSE_US
        self.next_step_time = 0.0
        self.hold_reported = False

    def home(self) -> None:
        self.current_pulse = UPRIGHT_SUPPORT_HOME_PULSE_US
        self.target_pulse = UPRIGHT_SUPPORT_HOLD_PULSE_US
        self.next_step_time = 0.0
        self.hold_reported = False
        duty_cycle, readback = self.write_pulse(self.current_pulse)
        print(
            f"HOME channel={UPRIGHT_SUPPORT_CHANNEL} current_pulse={self.current_pulse} "
            f"target_pulse={self.target_pulse} duty_cycle={duty_cycle} readback={readback}",
            flush=True,
        )

    def update(self) -> None:
        self.target_pulse = UPRIGHT_SUPPORT_HOLD_PULSE_US
        now = time.monotonic()
        if self.current_pulse == self.target_pulse:
            if not self.hold_reported:
                print("UPRIGHT SUPPORT", flush=True)
                self.hold_reported = True
            return
        if now < self.next_step_time:
            return
        if self.current_pulse < self.target_pulse:
            self.current_pulse = min(self.target_pulse, self.current_pulse + UPRIGHT_SUPPORT_STEP_US)
        else:
            self.current_pulse = max(self.target_pulse, self.current_pulse - UPRIGHT_SUPPORT_STEP_US)
        self.write_pulse(self.current_pulse)
        self.next_step_time = now + UPRIGHT_SUPPORT_UPDATE_SECONDS

    def write_pulse(self, pulse_us: int) -> tuple[int, int]:
        pulse_us = clamp_upright_support_pulse(pulse_us)
        duty_cycle = track_pulse_to_duty_cycle(pulse_us)
        self.pca.channels[UPRIGHT_SUPPORT_CHANNEL].duty_cycle = duty_cycle
        readback = self.pca.channels[UPRIGHT_SUPPORT_CHANNEL].duty_cycle
        return duty_cycle, readback

    def disable(self) -> None:
        self.pca.channels[UPRIGHT_SUPPORT_CHANNEL].duty_cycle = 0
        print(f"PWM disabled on channel {UPRIGHT_SUPPORT_CHANNEL}.", flush=True)


class Channel2FollowHelper:
    def __init__(self, pca: PCA9685) -> None:
        self.pca = pca
        self.current_pulse = FOLLOW_CHANNEL2_HOME_PULSE_US
        self.target_pulse = FOLLOW_CHANNEL2_HOME_PULSE_US
        self.next_step_time = 0.0

    def home(self) -> None:
        self.current_pulse = FOLLOW_CHANNEL2_HOME_PULSE_US
        self.target_pulse = FOLLOW_CHANNEL2_HOME_PULSE_US
        duty_cycle, readback = self.write_pulse(self.current_pulse)
        print(
            f"HOME channel={FOLLOW_CHANNEL2} current_pulse={self.current_pulse} "
            f"target_pulse={self.target_pulse} duty_cycle={duty_cycle} readback={readback}",
            flush=True,
        )

    def update(self, vertical_status: str, vertical_error: int | None) -> None:
        if vertical_status == "NO PERSON" or vertical_error is None:
            self.target_pulse = self.current_pulse
            return

        if vertical_status == "TRACK UP":
            self.target_pulse = FOLLOW_CHANNEL2_MIN_PULSE_US
        elif vertical_status == "TRACK DOWN":
            self.target_pulse = FOLLOW_CHANNEL2_MAX_PULSE_US
        else:
            self.target_pulse = FOLLOW_CHANNEL2_HOME_PULSE_US

        now = time.monotonic()
        if now < self.next_step_time or self.target_pulse == self.current_pulse:
            return

        if self.current_pulse < self.target_pulse:
            self.current_pulse = min(self.target_pulse, self.current_pulse + FOLLOW_CHANNEL2_STEP_US)
        else:
            self.current_pulse = max(self.target_pulse, self.current_pulse - FOLLOW_CHANNEL2_STEP_US)
        self.write_pulse(self.current_pulse)
        self.next_step_time = now + FOLLOW_CHANNEL2_UPDATE_SECONDS

    def write_pulse(self, pulse_us: int) -> tuple[int, int]:
        pulse_us = clamp_follow_channel2_pulse(pulse_us)
        duty_cycle = track_pulse_to_duty_cycle(pulse_us)
        self.pca.channels[FOLLOW_CHANNEL2].duty_cycle = duty_cycle
        readback = self.pca.channels[FOLLOW_CHANNEL2].duty_cycle
        return duty_cycle, readback


class BehaviorController:
    """Non-blocking GREET sequencing for channels 0, 1, and 2."""

    def __init__(self, pca: PCA9685) -> None:
        self.pca = pca
        self.state = "FOLLOW"
        self.sequence: list[tuple[int | None, int | None, int | None]] = []
        self.sequence_index = 0
        self.step_started = 0.0
        self.channel_pulses = {0: 1500, 1: 1500, 2: 1500}
        self.channel_targets: dict[int, int] = {}
        self.engagement_started = 0.0
        self.last_valid_tracked_at = 0.0
        self.last_engaged_at = 0.0
        self.greet_finished_at = 0.0
        self.last_greet_at = -GREET_COOLDOWN
        self.last_dance_at = -HAPPY_DANCE_COOLDOWN
        self.greet_cooldown_remaining = 0.0
        self.dance_cooldown_remaining = 0.0
        self.engagement_time = 0.0
        self.disable_all()

    def disable_channel(self, channel: int) -> None:
        self.pca.channels[channel].duty_cycle = 0

    def disable_all(self) -> None:
        for channel in GREET_CHANNELS:
            self.disable_channel(channel)
            print(f"PWM disabled on channel {channel}.", flush=True)

    def home(self) -> None:
        return

    def pulse_from_readback(self, channel: int, fallback: int) -> int:
        duty_cycle = int(self.pca.channels[channel].duty_cycle)
        if duty_cycle <= 0:
            return fallback
        period_us = 1_000_000 / PCA9685_FREQUENCY_HZ
        return round((duty_cycle / 0xFFFF) * period_us)

    def sync_channel(self, channel: int) -> None:
        fallback = self.channel_pulses.get(channel, 1500)
        if channel in GREET_CHANNELS:
            self.channel_pulses[channel] = max(
                GREET_MIN_PULSE_US,
                min(GREET_MAX_PULSE_US, self.pulse_from_readback(channel, fallback)),
            )

    def write(self, channel: int, pulse_us: int) -> None:
        if channel not in GREET_CHANNELS:
            return
        if channel in GREET_CHANNELS:
            pulse_us = max(GREET_MIN_PULSE_US, min(GREET_MAX_PULSE_US, pulse_us))
        self.channel_pulses[channel] = pulse_us
        self.pca.channels[channel].duty_cycle = track_pulse_to_duty_cycle(pulse_us)

    def start_greet(self, now: float) -> None:
        self.state = "GREET"
        self.sequence = [
            (1750, 1600, 1600),
            (1500, 1400, 1400),
            (1500, 1500, 1500),
        ]
        self.sequence_index = 0
        self.step_started = now
        self.last_greet_at = now
        for channel in GREET_CHANNELS:
            self.sync_channel(channel)
        self.apply_targets()
        print("GREET START", flush=True)

    def apply_targets(self) -> None:
        channel0_pulse, channel1_pulse, channel2_pulse = self.sequence[self.sequence_index]
        self.channel_targets = {}
        if channel0_pulse is not None:
            self.channel_targets[0] = channel0_pulse
        if channel1_pulse is not None:
            self.channel_targets[1] = channel1_pulse
        if channel2_pulse is not None:
            self.channel_targets[2] = channel2_pulse

    def targets_reached(self) -> bool:
        return all(self.channel_pulses[channel] == target for channel, target in self.channel_targets.items())

    def advance_sequence(self, now: float) -> None:
        while self.state == "GREET" and self.targets_reached():
            self.sequence_index += 1
            if self.sequence_index >= len(self.sequence):
                self.greet_finished_at = now
                self.finish(now)
                return
            self.apply_targets()

    def ramp_targets(self) -> None:
        moved = False
        for channel, target in self.channel_targets.items():
            current = self.channel_pulses[channel]
            if current < target:
                self.write(channel, min(target, current + BEHAVIOR_STEP_US))
                moved = True
            elif current > target:
                self.write(channel, max(target, current - BEHAVIOR_STEP_US))
                moved = True
        if moved:
            print(
                "GREET STEP "
                f"ch0={self.channel_pulses[0]} "
                f"ch1={self.channel_pulses[1]} "
                f"ch2={self.channel_pulses[2]}",
                flush=True,
            )

    def finish(self, now: float) -> None:
        self.state = "FOLLOW"
        self.sequence = []
        self.sequence_index = 0
        self.channel_targets = {}
        self.greet_finished_at = now if self.last_greet_at >= self.last_dance_at else self.greet_finished_at
        print("GREET COMPLETE -> FOLLOW", flush=True)

    def update(self, now: float, engaged: bool, valid_tracked_person: bool) -> bool:
        self.greet_cooldown_remaining = max(0.0, self.last_greet_at + GREET_COOLDOWN - now)
        self.dance_cooldown_remaining = max(0.0, self.last_dance_at + HAPPY_DANCE_COOLDOWN - now)
        if valid_tracked_person:
            self.last_valid_tracked_at = now

        if engaged and valid_tracked_person:
            if self.engagement_started == 0.0:
                self.engagement_started = now
            self.last_engaged_at = now
            self.engagement_time = now - self.engagement_started
        elif (
            self.engagement_started
            and not valid_tracked_person
            and now - self.last_engaged_at < BEHAVIOR_LOST_HOLD_SECONDS
        ):
            self.engagement_time = now - self.engagement_started
        else:
            self.engagement_started = 0.0
            self.last_engaged_at = 0.0
            self.engagement_time = 0.0

        if self.state == "GREET":
            self.advance_sequence(now)
            if self.state == "GREET" and now - self.step_started >= BEHAVIOR_STEP_SECONDS:
                self.ramp_targets()
                self.step_started = now
                self.advance_sequence(now)
            return self.state == "GREET"

        if (
            engaged
            and valid_tracked_person
            and self.engagement_time >= GREET_TRIGGER_SECONDS
            and self.greet_cooldown_remaining <= 0.0
        ):
            self.start_greet(now)
            return True
        return False


def parse_camera(value: str | None) -> int | str | None:
    if value is None:
        return None
    if value.isdigit():
        return int(value)
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="USB webcam generic person tracking diagnostics.")
    parser.add_argument("--camera", default=None, help="Camera index or /dev/videoN path. Default: auto-detect.")
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    parser.add_argument("--height", type=int, default=DEFAULT_HEIGHT)
    parser.add_argument("--preview", action="store_true", help="Show a low-quality OpenCV preview window.")
    parser.add_argument("--headless", action="store_true", help="Deprecated alias for default no-preview mode.")
    parser.add_argument("--track", action="store_true", help="Enable safe channel 5 person-following.")
    parser.add_argument("--confirm-motion-ready", action="store_true", help="Required with --track.")
    parser.add_argument(
        "--dry-start-no-pwm",
        action="store_true",
        help="Exercise startup/HOME handling without opening PCA9685, camera, preview, or writing PWM.",
    )
    parser.add_argument("--frames", type=int, default=0, help="Stop after N frames. Default: run until Ctrl+C.")
    return parser.parse_args()


def confirm_home(stop_requested) -> bool:
    print("READY TO HOME CHANNELS 2, 3, 4 AND 5", flush=True)
    try:
        line = ""
        while not stop_requested():
            readable, _, _ = select.select([sys.stdin], [], [], HOME_POLL_SECONDS)
            if not readable:
                continue
            char = sys.stdin.read(1)
            if char == "":
                return False
            if char in ("\n", "\r"):
                return line.strip() == "HOME"
            line += char
        return False
    except EOFError:
        return False


def stdin_requested_quit() -> bool:
    if not sys.stdin.isatty():
        return False
    readable, _, _ = select.select([sys.stdin], [], [], 0)
    if not readable:
        return False
    return sys.stdin.read(1).lower() == "q"


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
    preview = PreviewWindow()
    preview_enabled = False
    tracker: Channel5Tracker | None = None
    vertical_tracker: Channel4VerticalTracker | None = None
    upright_support: Channel3UprightSupport | None = None
    follow_channel2: Channel2FollowHelper | None = None
    behavior: BehaviorController | None = None
    try:
        if args.dry_start_no_pwm:
            if not args.track or not args.confirm_motion_ready:
                print("Dry startup requires --track --confirm-motion-ready.", file=sys.stderr, flush=True)
                return 2
            if not confirm_home(lambda: stop_requested):
                print("Home confirmation failed; dry startup did not continue.", file=sys.stderr, flush=True)
                return 2
            print("TRACKING READY", flush=True)
            print("DRY START COMPLETE - no PCA9685, camera, preview, or PWM writes performed.", flush=True)
            return 0

        if args.track:
            if not args.confirm_motion_ready:
                print("Refusing to track without --confirm-motion-ready.", file=sys.stderr, flush=True)
                return 2
            tracker = Channel5Tracker()
            tracker.open()
            if tracker.pca is None:
                raise RuntimeError("PCA9685 did not initialize.")
            vertical_tracker = Channel4VerticalTracker(tracker.pca)
            upright_support = Channel3UprightSupport(tracker.pca)
            follow_channel2 = Channel2FollowHelper(tracker.pca)
            vertical_tracker.disable()
            upright_support.disable()
            behavior = BehaviorController(tracker.pca)
            if not confirm_home(lambda: stop_requested):
                print("Home confirmation failed; channels 2, 3, 4 and 5 were not homed.", file=sys.stderr, flush=True)
                return 2
            print("HOME confirmed", flush=True)
            follow_channel2.home()
            upright_support.home()
            vertical_tracker.home()
            tracker.home()
            print("TRACKING READY", flush=True)
            print("FOLLOW", flush=True)

        cap = open_camera(parse_camera(args.camera), args.width, args.height)
        detector = create_person_detector()
        target_selector = PersonTargetSelector()

        frame_count = 0
        fps_count = 0
        fps = 0.0
        fps_started = time.monotonic()
        last_status = 0.0
        last_preview = 0.0

        while not stop_requested:
            ok, frame = cap.read()
            if not ok or frame is None:
                print("camera_read_failed=yes", file=sys.stderr, flush=True)
                time.sleep(0.05)
                continue
            if stdin_requested_quit():
                print("quit requested by Q", flush=True)
                break

            frame_count += 1
            fps_count += 1
            height, width = frame.shape[:2]
            if args.preview and not preview_enabled and frame_count == 1:
                preview_enabled = preview.open(width, height)
            now = time.monotonic()
            elapsed = now - fps_started
            if elapsed >= 1.0:
                fps = fps_count / elapsed
                fps_count = 0
                fps_started = now

            person_boxes = detect_people(detector, frame)
            person_box, tracking_point = target_selector.update(person_boxes, now, frame)
            control_point = target_selector.control_point()
            track_status, horizontal_error = track_status_for(control_point, width)
            vertical_status, vertical_error = vertical_status_for(control_point, height)
            engaged = (
                control_point is not None
                and horizontal_error is not None
                and abs(horizontal_error) <= TRACK_HORIZONTAL_DEADZONE_PX
            )
            valid_tracked_person = (
                person_box is not None
                and tracking_point is not None
                and target_selector.state in ("DETECTED", "TRACKING PROFILE")
            )
            behavior_active = (
                behavior.update(now, engaged, valid_tracked_person)
                if behavior is not None
                else False
            )
            if tracker is not None:
                if not behavior_active:
                    tracker.update(track_status, horizontal_error)
                tracker.update_step_rate(now)
                channel5_current_pulse = tracker.current_pulse
                channel5_target_pulse = tracker.target_pulse
                servo_steps_last_second = tracker.steps_last_second
                selected_step_us = tracker.selected_step_us
                tracking_state = tracker.tracking_state
                if vertical_tracker is not None:
                    if not behavior_active:
                        vertical_tracker.update(vertical_status, vertical_error)
                    channel4_current_pulse = vertical_tracker.current_pulse
                    channel4_target_pulse = vertical_tracker.target_pulse
                    vertical_selected_step_us = vertical_tracker.selected_step_us
                    vertical_tracking_state = vertical_tracker.tracking_state
                else:
                    channel4_current_pulse = None
                    channel4_target_pulse = None
                    vertical_selected_step_us = None
                    vertical_tracking_state = None
                if follow_channel2 is not None:
                    if not behavior_active:
                        follow_channel2.update(vertical_status, vertical_error)
                    channel2_current_pulse = follow_channel2.current_pulse
                    channel2_target_pulse = follow_channel2.target_pulse
                else:
                    channel2_current_pulse = None
                    channel2_target_pulse = None
                if upright_support is not None:
                    if not behavior_active:
                        upright_support.update()
                    channel3_current_pulse = upright_support.current_pulse
                else:
                    channel3_current_pulse = None
            else:
                channel5_current_pulse = None
                channel5_target_pulse = None
                channel4_current_pulse = None
                channel4_target_pulse = None
                channel3_current_pulse = None
                channel2_current_pulse = None
                channel2_target_pulse = None
                servo_steps_last_second = None
                selected_step_us = None
                vertical_selected_step_us = None
                tracking_state = None
                vertical_tracking_state = None
            behavior_state = behavior.state if behavior is not None else "FOLLOW"
            engagement_time = behavior.engagement_time if behavior is not None else None
            greet_cooldown_remaining = behavior.greet_cooldown_remaining if behavior is not None else None
            dance_cooldown_remaining = behavior.dance_cooldown_remaining if behavior is not None else None

            if now - last_status >= 1.0 or frame_count == 1:
                print_status(
                    width,
                    height,
                    fps,
                    person_box,
                    tracking_point,
                    track_status,
                    channel5_current_pulse,
                    channel5_target_pulse,
                    channel4_current_pulse,
                    channel4_target_pulse,
                    channel3_current_pulse,
                    channel2_current_pulse,
                    channel2_target_pulse,
                    servo_steps_last_second,
                    selected_step_us,
                    vertical_selected_step_us,
                    tracking_state,
                    vertical_tracking_state,
                    target_selector.predicted_point,
                    target_selector.velocity,
                    target_selector.chosen_index,
                    target_selector.lost_events,
                    behavior_state,
                    engagement_time,
                    greet_cooldown_remaining,
                    dance_cooldown_remaining,
                )
                last_status = now

            if preview_enabled and now - last_preview >= 1.0 / PREVIEW_FPS:
                if not preview.show(
                    frame,
                    person_box,
                    tracking_point,
                    fps,
                    track_status,
                    channel5_current_pulse,
                    channel5_target_pulse,
                    channel4_current_pulse,
                    channel4_target_pulse,
                    target_selector.predicted_box,
                    target_selector.velocity,
                    behavior_state,
                ):
                    preview.close()
                    preview_enabled = False
                    if tracker is not None:
                        print("preview closed; stopping tracking", flush=True)
                        break
                last_preview = now

            if args.frames and frame_count >= args.frames:
                break

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
        if behavior is not None:
            behavior.disable_all()
        if upright_support is not None:
            upright_support.disable()
        if vertical_tracker is not None:
            vertical_tracker.disable()
        if tracker is not None:
            tracker.close()
        preview.close()
        print("camera released", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
