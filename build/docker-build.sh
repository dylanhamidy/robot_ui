#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/.."
VERSION=${1:-"1.0.0"}
IMAGE="luxolis/robot_ui:${VERSION}"

# Copy lux_dsr_control into build context (skip if already present, e.g. transferred via SCP)
if [ ! -d ./lux_dsr_control ]; then
    cp -r ~/ros2_ws/src/doosan-robot-guides/lux_dsr_control ./lux_dsr_control
fi

# Minify JS + HTML
bash build/minify.sh

# Build image
docker build --tag "${IMAGE}" --tag "luxolis/robot_ui:latest" .

# Clean up
rm -rf ./lux_dsr_control

echo "✓ ${IMAGE}"
