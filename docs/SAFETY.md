# Safety

> [!DANGER]
> This is a physical robotic arm capable of causing injury. Keep clear during startup and `HOME`. Provide an accessible servo power disconnect. Test new movement ranges slowly. `Ctrl+C` or a service stop must disable PWM. Software limits do not replace continuous physical supervision.

Never treat pulse clamps, dead zones, cooldowns, motion acknowledgements, or automatic PWM cleanup as a substitute for an attentive person who can immediately disconnect servo power. Supervise every powered movement, including previously tested behavior.

## Primary hazards

- Pinch and crush points between links, gears, gripper jaws, and the work surface
- Sudden movement at boot, service restart, `HOME`, or after target reacquisition
- Stall current, overheating, wiring damage, or fire from an undersized supply
- Electrical damage or fire caused by backfeeding between the Pi/HAT logic rail and external servo supply
- Falling or tipping if the base is unsecured
- Camera privacy exposure through the unauthenticated MJPEG stream
- Unexpected travel from incorrect horn indexing, channel mapping, or copied calibration values

## Before every powered run

1. Secure the base and inspect the arm, cables, and servo horns.
2. Remove tools and loose objects from the motion envelope.
3. Keep children, pets, hair, sleeves, jewelry, and fingers clear.
4. Confirm the correct script and mode; many files in this repository are motion experiments.
5. Confirm the servo supply voltage/current rating and common ground.
6. Make the physical servo-power disconnect immediately reachable.
7. Start with no payload and with a second person nearby when testing new behavior.

## Startup and HOME

`HOME` is a movement command, not a harmless reset. The main controller moves channels 2, 3, 4, and 5 toward 1500 µs after confirmation. The systemd launcher supplies this confirmation automatically after a 20-second delay. Never stand within reach while booting an autostart-enabled arm.

## Emergency stop and normal shutdown

- For normal interactive shutdown, press `Ctrl+C` once and observe cleanup messages.
- For service shutdown, run `sudo systemctl stop person-follow.service` and watch the journal.
- If motion continues, the process hangs, or the arm binds, disconnect servo power immediately. Do not wait for software.
- Do not cut Raspberry Pi power as the primary servo emergency stop unless that same action independently removes servo power.

The working controller installs `SIGINT` and `SIGTERM` handlers and sets managed channel PWM duty cycles to zero in `finally`. This is a risk-reduction measure, not a physical safety system. Experimental scripts should be individually reviewed for equivalent cleanup before use.

## Changing motion

Treat every range, direction, sequence, speed, and home-position change as a new physical experiment. Test one servo at a time, in small increments, at low load. Software clamps are not collision detection. See [SERVO_MAPPING.md](SERVO_MAPPING.md).

## Network and privacy

The MJPEG service binds on all interfaces and has no login or encryption. Limit access with the LAN/firewall; do not expose port 8080 publicly. Never commit Wi-Fi credentials, passwords, SSH keys, tokens, private IP history, camera captures, or debug logs.

