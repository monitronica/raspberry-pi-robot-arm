# Camera Preview and MJPEG Stream

## Image path

The controller captures frames from a USB V4L2 camera, runs OpenCV's person detector, selects and predicts a target, and builds an annotated preview. The displayed/streamed frame is horizontally mirrored; tracking math uses the original frame.

## Camera requirements

- Linux UVC/V4L2-compatible USB camera exposed as a capture-capable `/dev/videoN` device
- Reliable 640×480 capture for the default configuration
- Stable mounting and cable routing outside the arm's moving/pinch areas
- Lighting and field of view sufficient to show a substantial portion of the tracked person
- Adequate Raspberry Pi USB bandwidth and power; avoid an unpowered hub shared with other high-load devices

Some cameras create multiple video nodes for image data, compressed formats, or metadata. Use `v4l2-ctl --list-devices` and `--list-formats-ext` to identify the capture node. Test video with servo power disconnected before enabling tracking.

### Low-light limitations

OpenCV person detection and tracking depend on usable contrast and sharp frames. In low light, camera auto-exposure can increase gain or exposure time, causing noise, motion blur, latency, lower effective frame rate, missed detections, target switching, or stale prediction. Mirrored preview visibility does not guarantee reliable detector input.

Use stable, diffuse lighting and test the full expected scene with servo power disconnected. Do not treat `NO PERSON`, a frozen/dark stream, or loss of a tracking box as proof that the arm is safe or PWM is disabled. The separate light-react experiment has additional low-light limitations documented in [EXPERIMENTS.md](EXPERIMENTS.md).

## Exclusive camera ownership

The USB camera must not be opened by two processes simultaneously. Run only one main controller, preview test, MJPEG controller, demo, or experiment against a given `/dev/videoN` device at a time. Competing processes can cause `device busy` errors, failed reads, unstable frame delivery, or one process silently selecting another camera node.

Before starting a manual camera command, check and stop the systemd controller cleanly:

```bash
systemctl status person-follow.service
sudo systemctl stop person-follow.service
```

Then inspect ownership if the device is still busy:

```bash
fuser /dev/video0
lsof /dev/video0
```

Identify the process before stopping it; do not blindly kill unfamiliar processes. When the manual test is finished, exit it cleanly and verify the camera has been released before restarting the service. A browser viewing `/stream.mjpg` is a client of the existing controller and does not open `/dev/videoN` itself, so multiple browser clients are different from multiple camera-controller processes.

Local preview tries the available display backend and can write directly to a 16-bit `/dev/fb0`. The default preview target is up to 960×720 at approximately 12 preview frames per second.

## Framebuffer requirements

For direct LCD output, the controller requires:

- `/dev/fb0` provided by the installed LCD/display driver
- 16-bit framebuffer color depth
- readable `/sys/class/graphics/fb0/virtual_size`
- readable `/sys/class/graphics/fb0/bits_per_pixel`
- readable `/sys/class/graphics/fb0/stride`
- permission for the interactive or systemd user to write `/dev/fb0`
- framebuffer dimensions of at least 960×720 for the default 640×480 camera path; the direct writer does not crop to a smaller display

Direct framebuffer output is optional. If it is unavailable, a configured pygame/SDL display may work; otherwise use headless processing or `--web-stream`. Framebuffer access affects preview only and must not be used as evidence that servo control or PWM shutdown is safe.

## Browser stream

### `--web-stream`

`--web-stream` starts the main controller's built-in HTTP/MJPEG server. It does not enable servo motion by itself and does not require `--track`; it publishes the same mirrored, annotated preview frames that the controller generates from the camera. It can be used with or without the local `--preview` option.

Camera and browser stream only, with no PWM access:

```bash
.venv/bin/python person_follow_camera.py --preview --web-stream
```

Headless browser stream without the local LCD/window:

```bash
.venv/bin/python person_follow_camera.py --web-stream
```

To combine the stream with person-follow motion, all normal motion safeguards are still required:

```bash
printf 'HOME\n' | .venv/bin/python person_follow_camera.py \
  --track --confirm-motion-ready --preview --web-stream
```

The last command can move the physical arm. `--web-stream` does not change the HOME, supervision, range, or emergency-disconnect requirements.

Then browse to:

```text
http://<raspberry-pi-hostname>:8080/
```

The `/` and `/index.html` paths serve a minimal page containing the stream. The raw multipart MJPEG endpoint is:

```text
http://<raspberry-pi-hostname>:8080/stream.mjpg
```

The server binds to all interfaces at `0.0.0.0:8080`, uses JPEG quality 80, disables client caching, and serves connected clients from background threads. It has no authentication, authorization, TLS, or access log. Restrict it to a trusted LAN/firewall and never forward the port directly to the public internet. Avoid publishing screenshots that expose faces, home interiors, hostnames, or network details.

When the controller exits normally or handles `SIGINT`/`SIGTERM`, it closes the HTTP server and joins its thread during cleanup. A browser disconnect only closes that client's stream; it does not stop camera processing, servo tracking, or the systemd service. Likewise, closing a browser tab is not an emergency stop.

## Port 8080

The main controller's fixed web-stream port is TCP **8080**. Because it binds to `0.0.0.0`, enabling `--web-stream` listens on every active IPv4 interface, not only localhost.

| Access location | URL |
| --- | --- |
| On the Raspberry Pi | `http://127.0.0.1:8080/` |
| Trusted LAN using hostname | `http://<raspberry-pi-hostname>:8080/` |
| Raw MJPEG endpoint | `http://<raspberry-pi-hostname>:8080/stream.mjpg` |

### `/` endpoint

An HTTP `GET /` request returns a minimal HTML viewer page. The page has a dark background and a single responsive `<img>` element whose source is `/stream.mjpg`; it scales the image to fit the browser viewport. `GET /index.html` returns the same page.

```text
http://<raspberry-pi-hostname>:8080/
```

The `/` page is view-only. It has no start, stop, HOME, mode, authentication, or configuration controls. Loading or closing it does not change the controller state, and a successfully displayed page does not prove that person tracking or PWM cleanup is safe. Any other path except `/index.html` and `/stream.mjpg` returns HTTP 404.

### `/stream.mjpg` endpoint

An HTTP `GET /stream.mjpg` request returns a continuous multipart MJPEG response:

```text
Content-Type: multipart/x-mixed-replace; boundary=frame
```

Each part is a JPEG image with its own `Content-Type: image/jpeg` and `Content-Length` headers. Frames are encoded from the latest mirrored overlay at JPEG quality 80. The response also sends `Cache-Control: no-cache, private`, `Pragma: no-cache`, and `Age: 0` so clients do not reuse stale frames.

```text
http://<raspberry-pi-hostname>:8080/stream.mjpg
```

The endpoint is a continuous response rather than a downloadable `.mjpg` file. It is suitable for an `<img>` element or an MJPEG-capable viewer. Each connected client has a server thread and receives newly available frames until the client disconnects or the controller closes the server. Multiple viewers increase CPU, JPEG-encoding, and network load on the Raspberry Pi.

The stream contains only video frames; it has no audio, robot-control commands, status API, authentication, or encryption. Disconnecting from `/stream.mjpg` does not stop the camera, tracking, service, or PWM. Do not expose the endpoint outside a trusted network.

Confirm the listener without exposing or moving the arm:

```bash
ss -ltnp | grep ':8080'
```

Only one process can listen on the same address and port. If startup reports an address-in-use error, stop the unintended process or other stream instance; do not start multiple robot controllers to troubleshoot the port. The experimental light-react controller uses 8081, while the demo and random-motion streams use 8082.

Allow TCP 8080 only from a trusted local network if a host firewall is used. Do not configure router port forwarding, public reverse proxies, or broad internet exposure. The stream is unencrypted and unauthenticated, and its images may contain people or private surroundings.

Blocking port 8080 or closing a browser does not stop the controller, camera, systemd unit, or PWM output. Use `systemctl stop person-follow.service` or the interactive process's `Ctrl+C`, then verify PWM release physically.

## Local LAN access

The viewing device and Raspberry Pi normally need to be on the same trusted local network, with client isolation disabled between them and TCP 8080 allowed by the Pi's firewall. Start the controller with `--web-stream`, then use the Pi's current hostname or LAN address in the browser:

```text
http://<raspberry-pi-hostname>:8080/
http://<current-lan-address>:8080/
```

On the Pi, inspect the current values without adding them to project files:

```bash
hostname
hostname -I
```

If mDNS/Avahi is installed and permitted by the network, `http://<hostname>.local:8080/` may work. Otherwise use local DNS or the current LAN address. Address assignment can change after reboot unless the router provides a reservation; configure reservations in network infrastructure, not in this repository.

Test reachability from another trusted LAN device by opening the viewer page. If it fails, confirm that the service actually started with `--web-stream`, port 8080 is listening, both devices are on the same reachable network, and firewall rules allow only the intended local clients.

Do not document real private addresses, Wi-Fi names/passwords, router details, host histories, or remote-access credentials in Git. Use placeholders in examples. Do not solve LAN reachability by exposing port 8080 to the internet, enabling unrestricted router forwarding, or placing the Pi in a public DMZ.

`--web-stream` is optional. The current `start_person_follow.sh` enables local preview but does not enable the browser stream; edit/deploy service configuration only after local safety testing.

## Related experimental ports

- `person_follow_camera_light.py`: port 8081
- `demo_stream_motion.py`: port 8082
- `random_three_servo_stream.py`: port 8082

Only one process can bind a given port at a time. These scripts can also move hardware; do not start them merely to test video.

## Troubleshooting

- If no camera opens, list `/dev/video*` and pass a specific device with `--camera`.
- If local preview is absent, check `/dev/fb0`, its permissions, and its bits-per-pixel setting.
- If the page does not load, confirm `--web-stream`, the selected port, firewall rules, and the service log.
- A working stream does not prove that PWM cleanup works; test stop behavior under direct supervision.

