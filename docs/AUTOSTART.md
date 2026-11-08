# systemd Autostart

> [!WARNING]
> Enabling this service authorizes physical movement after every boot. Keep the arm's entire envelope clear during startup and `HOME`, and keep the servo-power disconnect accessible.

The included `person-follow.service` runs as user `robota` from `/home/robota/robot-arm`. `start_person_follow.sh` waits 20 seconds, supplies the required `HOME` confirmation, and starts tracking with local preview. It does not enable the MJPEG stream.

## Included `person-follow.service`

The repository includes this systemd unit as `person-follow.service`. It is the autostart definition; it does not contain the Python command directly. Instead, `ExecStart` launches `start_person_follow.sh`, which selects the project virtual environment and controller arguments.

| Unit setting | Purpose |
| --- | --- |
| `Wants=network-online.target` | Requests the network-online target as a non-hard dependency |
| `After=network-online.target` | Orders startup after that target |
| `Type=simple` | Treats the launched shell/Python process as the service process |
| `User=robota` | Runs without root as the dedicated installation user |
| `WorkingDirectory=/home/robota/robot-arm` | Sets the repository as the working directory |
| `ExecStart=/home/robota/robot-arm/start_person_follow.sh` | Starts the launcher script |
| `Restart=on-failure` | Restarts after an error/non-clean exit, but not after a normal stop |
| `RestartSec=5` | Waits five seconds before a failure restart |
| `KillSignal=SIGINT` | Uses the same signal as `Ctrl+C` so controller cleanup runs |
| `TimeoutStopSec=20` | Allows up to 20 seconds for shutdown before systemd escalates |
| `StandardOutput=journal` / `StandardError=journal` | Sends output and errors to journald |
| `Environment=PYTHONUNBUFFERED=1` | Makes Python logs appear promptly |
| `WantedBy=multi-user.target` | Enables normal multi-user boot autostart |

The paths and user are installation-specific. If the repository is not located at `/home/robota/robot-arm`, update `User`, `WorkingDirectory`, `ExecStart`, and any absolute paths inside `start_person_follow.sh` together. The selected user needs access to I2C, the USB camera, and `/dev/fb0` when local preview is enabled.

The unit must not be enabled until manual, supervised startup and shutdown have been verified on the physical arm. `Restart=on-failure` can repeat the 20-second delay and `HOME` sequence after a crash, so a restart is another motion event—not merely a software recovery.

## `multi-user.target`

`multi-user.target` is systemd's normal non-graphical, multi-user operating state and is appropriate for Raspberry Pi OS Lite. The service does not require a desktop session or a user to log in.

Running:

```bash
sudo systemctl enable person-follow.service
```

creates the systemd enablement link that makes `person-follow.service` wanted when the system reaches `multi-user.target`. Enabling does not immediately start the service; `systemctl start` starts it in the current boot, while the next normal boot starts it through the target.

Reaching `multi-user.target` does not prove that the arm is physically clear, the servo supply is safe, the camera is ready, `/dev/fb0` is writable, or the network is usable. The launcher's 20-second delay may allow devices to settle, but it is not a readiness check or safety interlock. Verify device dependencies and supervise every boot.

## `start_person_follow.sh`

`start_person_follow.sh` is the launcher called by `person-follow.service`. Its current behavior is:

1. `set -euo pipefail` makes the script stop on command failures, unset variables, or failed pipeline elements.
2. `sleep 20` waits 20 seconds after systemd starts the service.
3. It runs `/home/robota/robot-arm/.venv/bin/python` directly, so shell virtual-environment activation is unnecessary.
4. It starts `/home/robota/robot-arm/person_follow_camera.py` with `--track --confirm-motion-ready --preview`.
5. It supplies a single `HOME` line through standard input using process substitution.
6. `exec` replaces the shell with Python, allowing systemd signals and exit status to apply directly to the controller process.

The flags mean that servo tracking is enabled, the motion acknowledgement is supplied, and the local mirrored preview is requested. The launcher does **not** pass `--web-stream`, so the normal autostart configuration does not serve MJPEG on port 8080.

> [!WARNING]
> The automatically supplied `HOME` line removes the interactive confirmation pause. After the 20-second delay, the controller can command channels 2–5 toward their 1500 µs home positions. The delay does not inspect the workspace or ask whether a person is clear of the arm.

Both absolute paths in the script must match the installed repository and `.venv`. Keep the script executable:

```bash
chmod 0755 start_person_follow.sh
```

Do not add `sudo`, backgrounding (`&`), `nohup`, or another process wrapper to the launcher. The controller must remain the service's foreground process so `SIGINT`, stop timeouts, failure detection, journald output, and PWM cleanup behave as documented.

## Boot delay

The launcher currently uses a fixed `sleep 20`, so Python is started approximately 20 seconds after systemd launches the service. The delay gives Raspberry Pi OS, USB camera enumeration, I2C, and the display stack additional time to settle, but it does not actively check any of them.

The delay is **not**:

- a physical safety interlock;
- confirmation that the arm's workspace is clear;
- confirmation that the external servo PSU or corrected power wiring is safe;
- a camera, I2C, framebuffer, or network readiness test;
- a substitute for continuous physical supervision.

At the end of the delay, the launcher automatically supplies `HOME`, and movement may begin without another prompt. Start counting from service activation—not necessarily from the moment a screen becomes visible or a user can log in.

On a failure restart, systemd first waits `RestartSec=5`, then the launcher performs its 20-second delay again. Therefore, a crash can lead to a new startup/HOME attempt roughly 25 seconds later, plus normal systemd scheduling time. Do not rely on this interval as an emergency-stop window.

If hardware on a particular installation needs more startup time, change the delay only after supervised testing. A longer sleep can reduce timing-related startup failures, but it cannot make automatic movement intrinsically safe; real readiness checks and a physical power disconnect remain necessary.

## HOME automation

The main controller normally requires both `--track --confirm-motion-ready` and a line containing exactly `HOME` on standard input before it homes the tracking joints. `start_person_follow.sh` automates the second step with:

```bash
< <(printf 'HOME\n')
```

This process substitution gives the controller a one-line input stream. After the 20-second boot delay, the controller reads `HOME`, prints `HOME confirmed`, and commands these starting positions:

| Channel | Function | Automated HOME target |
| --- | --- | ---: |
| 2 | Pointing/helper joint | 1500 µs |
| 3 | Upright/support joint | 1500 µs |
| 4 | Vertical tracking | 1500 µs |
| 5 | Horizontal/base tracking | 1500 µs |

Channels 0 and 1 are not part of this HOME movement and are initially PWM-disabled by the behavior controller. They may later move during greeting or idle expressive behavior.

HOME is not calibration, collision detection, or a guarantee of a physically neutral pose. It assumes the installed horns, link geometry, channel mapping, and previously validated 1500 µs positions are correct. Incorrect assembly or an obstruction can make a nominal HOME command dangerous.

If the exact input is missing, interrupted, or rejected, the controller does not proceed to tracking and its cleanup path disables the PWM channels it opened. Nevertheless, software cleanup is not a physical safety system. Keep the workspace clear before service activation and remain ready to disconnect servo power throughout HOME.

Do not automate HOME on a newly assembled, rewired, recalibrated, or mechanically changed arm. First validate every affected channel manually and slowly under continuous physical supervision, then verify normal stop and forced-failure cleanup before restoring autostart.

## Install after supervised testing

Review paths and the service account first. On the Pi only:

```bash
sudo install -m 0644 person-follow.service /etc/systemd/system/person-follow.service
sudo systemctl daemon-reload
sudo systemctl enable person-follow.service
sudo systemctl start person-follow.service
```

These commands are deployment instructions; they were not run while preparing the repository.

## Service control with `systemctl`

### Start

```bash
sudo systemctl start person-follow.service
```

This starts the service in the current boot without enabling future boot startup. The launcher waits 20 seconds, automatically supplies `HOME`, and may then move the arm. Clear the entire movement envelope and begin continuous physical supervision before issuing the command—not after the delay begins.

### Stop

```bash
sudo systemctl stop person-follow.service
```

systemd sends `SIGINT` because the unit sets `KillSignal=SIGINT`. The controller should leave its main loop, disable PWM on managed channels, release the camera, close preview/stream resources, and exit. Watch the arm and journal until cleanup completes. Physically confirm that PWM has released; if motion continues or shutdown hangs, disconnect servo power immediately.

Stopping does not disable boot autostart. Use `sudo systemctl disable person-follow.service` separately if the service must remain off after reboot. To stop it now and disable future boot startup in one command, use:

```bash
sudo systemctl disable --now person-follow.service
```

### Status

```bash
systemctl status person-follow.service
```

Status reports whether systemd considers the unit active, inactive, failed, or still stopping, and shows recent journal lines. `active (running)` means the process exists; it does not prove that the camera, tracking, framebuffer, PWM cleanup, or physical arm is safe. After `stop`, wait for `inactive (dead)` and verify PWM release physically.

For complete or live logs, use:

```bash
journalctl -u person-follow.service -f
journalctl -u person-follow.service -b --no-pager
```

Press `Ctrl+C` to leave `journalctl -f`; this stops log viewing only and does not stop the robot service. Use `systemctl stop` in a separate command to stop the controller.

## `journalctl` logs

`person-follow.service` sends both standard output and standard error to the systemd journal. Python runs unbuffered, so controller status and cleanup messages should appear promptly.

| Purpose | Command |
| --- | --- |
| Follow new messages live | `journalctl -u person-follow.service -f` |
| Show this boot only | `journalctl -u person-follow.service -b` |
| Show the previous boot | `journalctl -u person-follow.service -b -1` |
| Show the last 100 lines | `journalctl -u person-follow.service -n 100 --no-pager` |
| Show messages since a time | `journalctl -u person-follow.service --since "2026-11-08 12:00:00"` |
| Show warnings/errors this boot | `journalctl -u person-follow.service -b -p warning` |
| Show ISO-style timestamps | `journalctl -u person-follow.service -o short-iso-precise` |

Replace the example date/time with the incident time being investigated. Add `sudo` if the configured user cannot read the system journal.

Useful startup messages include PCA9685 initialization, `READY TO HOME CHANNELS 2, 3, 4 AND 5`, `HOME confirmed`, `TRACKING READY`, `FOLLOW`, camera selection, and preview-backend information. During a normal stop, look for PWM-disabled messages for every managed channel and `camera released`, followed by an inactive service state.

Logs are diagnostic evidence, not proof of physical safety. A printed PWM-disabled message cannot confirm electrical isolation or that a stalled process released every output; observe the arm and keep the servo-power disconnect accessible.

Do not copy journal exports into this repository. Logs can contain usernames, absolute paths, hostnames, device names, camera/tracking data, timestamps, and network history. If logs must be shared for troubleshooting, extract only the smallest relevant interval, redact sensitive data, and attach it outside the Git history. The `.gitignore` excludes `*.log`, but ignore rules are not a substitute for reviewing data before publication.

The unit sends `SIGINT`, allows 20 seconds for shutdown, and restarts only on failure. The controller's `finally` block disables channels 0–5 that it manages and deinitializes the PCA9685. Supervise the first boot/stop cycles and confirm physically that PWM releases.

## Startup design notes

- The script's automatic `HOME` input bypasses an interactive operator pause. The 20-second delay is not a safety interlock.
- Startup can move channels 2–5 to 1500 µs. Channels 0–1 remain disabled until expressive/greeting behavior uses them.
- A service restart after failure can cause another startup/HOME cycle.
- Network availability is ordered before startup even though the normal local-preview command does not require internet access.
- Do not put passwords, tokens, Wi-Fi configuration, private addresses, or other secrets in the unit or shell script. Use system-managed configuration with restrictive permissions if future features need credentials.

