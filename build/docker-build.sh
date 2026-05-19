#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/.."
VERSION=${1:-"1.0.0"}
IMAGE="luxolis/robot_ui:${VERSION}"

# Copy lux_dsr_control into build context
cp -r ~/ros2_ws/src/lux_dsr_control ./lux_dsr_control

# Minify JS + HTML
bash build/minify.sh

# Build image
docker build --tag "${IMAGE}" --tag "luxolis/robot_ui:latest" .

# Clean up
rm -rf ./lux_dsr_control

echo "✓ ${IMAGE}"
