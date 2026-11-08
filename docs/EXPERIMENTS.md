# Experiments and Historical Scripts

The repository contains development utilities and motion experiments alongside the working controller. They are retained for traceability, not presented as safe examples or supported modes.

## Current production-oriented files

- `person_follow_camera.py` — main person-follow controller
- `start_person_follow.sh` — delayed systemd launcher for the main controller
- `person-follow.service` — systemd unit

## Separate experiments

| File/group | Purpose | Important distinction |
| --- | --- | --- |
| `person_follow_camera_light.py` | Bright-light reaction plus person tracking | Separate experimental mode; port 8081 and narrower light-react tracking bounds |
| `run_light_servo_test.sh` | Launcher for light-react servo test | Automatically supplies `HOME`; can move hardware |
| `demo_stream_motion.py` | Fixed slow demo sequence on channels 3–5 with MJPEG | Independent controller on port 8082 |
| `random_three_servo_stream.py` | Random slow motion on channels 3–5 with MJPEG | Independent controller on port 8082 |
| `ps_*.py` / `controller_event_probe.py` | PlayStation controller development and manual tests | Historical/manual control paths; review each range and cleanup path |
| `pca9685_*.py`, `servo0_*.py`, `single_servo_test.py`, `gripper_*.py` | Calibration and hardware diagnostics | Some deliberately exercise individual servos or broad pulse ranges |
| `arm_home_calibration.py` | Six-channel center calibration utility | Its 500–2500 µs validation bounds are not assembled-arm safety limits |
| `openclaw_hiwonder_arm.py` | Separate command-driven arm experiment | Not used by person-follow autostart |

## Light-react experiment

> [!CAUTION]
> `person_follow_camera_light.py` and `run_light_servo_test.sh` are experimental motion tools, not production-stable modes. They must not replace the main controller or be added to autostart without separate physical validation.

The light-react variant looks for a sufficiently bright, contrasting region in the camera image and moves the tracking joints toward it. With `--light-react-servo-test`, the experimental reaction is limited to:

| Channel | Function | Experimental light-react bounds |
| --- | --- | ---: |
| 4 | Vertical tracking | 1350–1650 µs |
| 5 | Horizontal/base tracking | 1300–1700 µs |

Movement advances in 5 µs steps at approximately 0.25-second update intervals, outside 55-pixel horizontal/vertical dead zones. Candidate light regions are filtered using brightness, contrast, area, and smoothing thresholds embedded in the experiment. These thresholds are scene-dependent and are not safety sensors: reflections, windows, flashlights, exposure changes, and camera noise can create unexpected targets.

The separate `run_light_servo_test.sh` launcher:

- automatically writes and supplies a `HOME` confirmation;
- starts `person_follow_camera_light.py` with motion enabled;
- requests local preview and the MJPEG stream;
- selects `--light-react-servo-test`;
- uses fixed `/home/robota/robot-arm` and `.venv` paths.

Although the light-react movement itself is limited to channels 4 and 5, startup HOME handling references channels 2–5. Treat launching it as a multi-joint motion event. Stop `person-follow.service` first so neither the camera nor PCA9685 is controlled by two processes.

The light-react controller's actual stream constant is port **8081**. Its current `--web-stream` help text incorrectly mentions port 8080; use the implemented port and treat that mismatch as another sign that this file is experimental.

Before any powered test, validate the 1350–1650 µs and 1300–1700 µs ranges on the exact physical arm, clear the movement envelope, supervise continuously, and keep the servo-power disconnect accessible. Test `Ctrl+C`/`SIGTERM` cleanup and physically verify PWM release. Do not point intense light sources into eyes or use heat-producing lamps near the robot, camera, wiring, or flammable material.

### Low-light limitations

The light-react experiment is especially unreliable in dim or uneven lighting. Automatic camera exposure may increase gain and exposure time, producing noise, motion blur, delayed frames, and apparent bright patches. A small lamp, reflective surface, display, window, or exposure transition may dominate the scene, while a real light source may remain below the experiment's brightness/contrast thresholds.

Low light can therefore cause no target, an unstable target, false target changes, slow reaction, or unexpected movement toward image noise/reflections. The fixed brightness threshold is not calibrated in physical light units and will behave differently across camera models, exposure settings, and scenes.

Do not tune thresholds or movement ranges simultaneously. First evaluate detection with servo power disconnected and inspect the overlay under every expected lighting condition. If motion is later enabled, use diffuse stable lighting, keep the movement envelope clear, supervise continuously, and be prepared to disconnect servo power. Loss of a light target is not a safe-stop mechanism.

## Random three-servo motion

> [!DANGER]
> `random_three_servo_stream.py` begins hardware initialization and HOME without an interactive motion-confirmation flag. It is an experimental motion generator, not a production service, test fixture, or unattended demonstration.

The script controls channels 3, 4, and 5 through the PCA9685 while serving a mirrored camera stream on TCP port 8082. Its embedded bounds are:

| Channel | Function | Experimental random-motion bounds |
| --- | --- | ---: |
| 3 | Upright/support joint | 1450–1550 µs |
| 4 | Vertical tracking | 1400–1600 µs |
| 5 | Horizontal/base tracking | 1300–1700 µs |

On startup, it attempts to HOME all three channels to 1500 µs. It then waits a random 5–12 seconds, selects one of the three channels, chooses a random target within that channel's range in 5 µs increments, and ramps toward the target in 5 µs steps every 0.25 seconds. After reaching the target, that channel returns to 1500 µs before the next random delay. Only one channel is selected for each random excursion, but the other joints remain part of the powered three-channel controller.

The MJPEG server starts automatically on `0.0.0.0:8082`; there is no separate `--web-stream` switch. The script also opens a camera immediately after HOME. Stop `person-follow.service` and every other camera/PCA9685 process before considering an experimental run. Do not expose port 8082 outside a trusted LAN.

Random selection does not make the movement envelope safe. Validate every endpoint and the complete path back to 1500 µs on the exact arm before using random targets. Secure the base, remove payloads, clear the full envelope, supervise continuously, and keep the servo-power disconnect in hand. An unexpected mechanical interaction can occur even when every individual pulse value was previously tested.

The script handles `SIGINT` and `SIGTERM`; its final cleanup releases the camera, closes the stream, disables PWM on channels 3–5, and deinitializes the PCA9685. Verify this behavior physically during a controlled first test. Do not install this script as `person-follow.service` or another boot service.

## Demo stream mode

> [!DANGER]
> `demo_stream_motion.py` is a scripted motion demonstration, not production-stable behavior. It initializes the PCA9685 and performs HOME without an interactive motion confirmation.

The script combines a mirrored MJPEG camera view with a repeating, deterministic movement sequence on channels 3–5. Its software bounds match the random-motion experiment:

| Channel | Function | Experimental demo bounds |
| --- | --- | ---: |
| 3 | Upright/support joint | 1450–1550 µs |
| 4 | Vertical tracking | 1400–1600 µs |
| 5 | Horizontal/base tracking | 1300–1700 µs |

After homing all three channels to 1500 µs, it repeats this target sequence:

```text
CH5 1650 → 1350 → 1500
CH4 1450 → 1550 → 1500
CH3 1550 → 1450 → 1500
```

Each move ramps in 5 µs steps with a randomly selected interval of approximately 0.20–0.30 seconds. It pauses approximately 1–3 seconds between targets. The target order is fixed, but timing varies. The presence of center targets in the sequence does not make the preceding path collision-free.

The camera and mirrored MJPEG stream start automatically after HOME on `0.0.0.0:8082`; there is no `--web-stream` switch. The overlay labels the run as demo mode. The script's “dark-room demo” description does not mean camera quality is guaranteed in low light; exposure noise, blur, and stream latency still apply.

Stop `person-follow.service` and all other camera/PCA9685 programs before considering a run. Validate every target and every transition slowly on the exact mechanism, remove payloads, secure the base, supervise continuously, and keep the servo-power disconnect accessible. Do not run it for unattended entertainment, exhibitions, or boot autostart.

The script handles `SIGINT` and `SIGTERM`. Final cleanup releases the camera, closes the HTTP server, disables PWM on channels 3–5, and deinitializes the PCA9685. Verify that cleanup physically during a controlled test; deterministic motion does not reduce the need for an emergency power disconnect.

## Feature status notes

- `FOLLOW` and `GREET` are active in the main controller.
- Idle expressive movement on channels 0–1 is active while a valid person is tracked.
- `HAPPY_DANCE` timing and range constants remain, but no dance state/sequence is reachable in this snapshot.
- The preview is mirrored, but the literal `HELLO` greeting text is not currently drawn.
- Light-react and random/demo movement are intentionally not integrated into normal autostart.

## Rules for future experiments

1. Fork/copy the experiment instead of changing the known working entry point during calibration.
2. Default to no motion; require an explicit motion acknowledgement.
3. Clamp every channel independently and document whether a value is operational or experimental.
4. Ramp slowly from a verified neutral position.
5. Handle `SIGINT` and `SIGTERM`, then disable every PWM channel touched by the process in `finally`.
6. Never run two PCA9685 control processes concurrently.
7. Never open the same `/dev/videoN` camera from two controller/experiment processes; stop the systemd service before manual camera tests.
8. Do not commit logs, controller event dumps, camera images, host/network history, or credentials.
9. Promote an experiment to a documented working mode only after repeatable supervised validation on the physical arm.

