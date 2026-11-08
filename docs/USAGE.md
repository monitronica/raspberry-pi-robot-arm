# Usage

## Main controller

`person_follow_camera.py` is the working entry point. Camera-only operation does not write servo PWM:

```bash
.venv/bin/python person_follow_camera.py --preview --web-stream
```

Motion requires `--track`, `--confirm-motion-ready`, and a line containing `HOME` on standard input:

```bash
printf 'HOME\n' | .venv/bin/python person_follow_camera.py \
  --track --confirm-motion-ready --preview --web-stream
```

This command can move the arm immediately after the `HOME` confirmation. Clear the workspace first.

## Behavior states

### FOLLOW

OpenCV detects people and chooses a tracking target. Channels 5 and 4 track horizontal and vertical error. Channels 3 and 2 provide limited support/helper movement. The overlay is mirrored for a natural local display while control remains based on the unmirrored camera frame.

### GREET

After a valid person remains horizontally engaged for about three seconds, the controller performs a non-blocking three-channel greeting on channels 0–2, then returns to `FOLLOW`. The normal greeting cooldown is 15 seconds.

### HAPPY_DANCE

The source retains a 10-second trigger constant, a 30-second cooldown, and nominal 1400–1650 µs dance bounds. However, this repository snapshot has no `start_dance` sequence or transition into a `HAPPY_DANCE` state. Treat it as planned/incomplete, not a working movement mode.

### HELLO overlay

The preview reports the active behavior and tracking data. A literal `HELLO` overlay during `GREET` is not present in this snapshot; it is a documented UI enhancement rather than behavior to rely on.

## Stopping safely

Use `Ctrl+C` for an interactive run or `sudo systemctl stop person-follow` for the service. The controller handles `SIGINT`/`SIGTERM` and disables PWM in `finally` cleanup. Watch the log and physically verify relaxation; if software does not stop promptly, disconnect servo power.

Closing the preview while tracking also stops the main loop. Never use loss of the browser stream as proof that PWM is disabled.

## Options

```text
--camera INDEX|/dev/videoN  select a camera; default is auto-detect
--width N --height N       requested capture size (default 640x480)
--preview                   enable the local mirrored preview
--web-stream                serve the mirrored overlay on port 8080
--headless                  deprecated alias for the default no-preview mode
--track                     enable servo tracking
--confirm-motion-ready      required acknowledgement for --track
--dry-start-no-pwm          exercise startup/HOME without hardware access
--frames N                  stop after N frames (0 means run continuously)
```

## Experiments

Do not use experimental scripts as routine launchers. They may have different ranges, ports, or motion semantics. See [EXPERIMENTS.md](EXPERIMENTS.md).

