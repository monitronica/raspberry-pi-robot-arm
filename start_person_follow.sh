#!/usr/bin/env bash
set -euo pipefail

sleep 20
exec /home/robota/robot-arm/.venv/bin/python \
    /home/robota/robot-arm/person_follow_camera.py \
    --track --confirm-motion-ready --preview \
    < <(printf 'HOME\n')
