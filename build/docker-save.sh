#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/.."
VERSION=${1:-"1.0.0"}
IMAGE="luxolis/robot_ui:${VERSION}"
OUTPUT="dist/robot_ui_v${VERSION}.tar.gz"

mkdir -p dist
echo "▶ Saving ${IMAGE} → ${OUTPUT}  (may take a few minutes)..."
docker save "${IMAGE}" | gzip > "${OUTPUT}"
echo "✓ ${OUTPUT}  $(du -sh ${OUTPUT} | cut -f1)"
