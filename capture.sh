#!/bin/bash
# Placeholder capture script — replace with your real camera capture commands.
# Example using libcamera-still / raspistill style tools:
#
# libcamera-still -o "captures/$(date +%Y%m%d_%H%M%S).jpg"

set -e
mkdir -p captures
touch "captures/$(date +%Y%m%d_%H%M%S).jpg"
echo "Capture complete."
