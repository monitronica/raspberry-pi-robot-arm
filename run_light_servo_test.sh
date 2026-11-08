#!/usr/bin/env bash
set -euo pipefail

cd /home/robota/robot-arm
printf 'HOME\n' > /tmp/person_follow_camera_light_home.txt
exec /home/robota/robot-arm/.venv/bin/python \
    /home/robota/robot-arm/person_follow_camera_light.py \
    --track --confirm-motion-ready --preview --web-stream --light-react-servo-test \
    < /tmp/person_follow_camera_light_home.txt
