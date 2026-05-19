#!/usr/bin/env bash
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
IMAGE="luxolis/robot_ui:1.0.0"
DATA_DIR="$HOME/robot_ui_data"

# ── Step 1: Install Docker if missing ─────────────────────────────────────
if ! command -v docker &>/dev/null; then
    echo "▶ Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    sudo usermod -aG docker "$USER"
    echo "✓ Docker installed. Re-running with docker group..."
    exec sg docker "$0 $@"
fi

# ── Step 2: Load image ────────────────────────────────────────────────────
IMAGE_TAR="$(ls "${SCRIPT_DIR}"/robot_ui_*.tar.gz 2>/dev/null | head -1)"
if [ -z "${IMAGE_TAR}" ]; then
    echo "Error: no robot_ui_*.tar.gz found in ${SCRIPT_DIR}"
    exit 1
fi

if ! docker image inspect "${IMAGE}" &>/dev/null; then
    echo "▶ Loading image from ${IMAGE_TAR}  (this takes a few minutes)..."
    docker load < "${IMAGE_TAR}"
fi

# ── Step 3: Prepare data dirs ─────────────────────────────────────────────
mkdir -p "${DATA_DIR}/plans" "${DATA_DIR}/stats"

# ── Step 4: Allow X11 for RViz2 ──────────────────────────────────────────
xhost +local:docker 2>/dev/null || true

# ── Step 5: Start container ───────────────────────────────────────────────
docker rm -f robot_ui 2>/dev/null || true

# Pass through any Arduino USB serial devices
DEVICE_FLAGS=""
for dev in /dev/ttyACM* /dev/ttyUSB*; do
    [ -e "$dev" ] && DEVICE_FLAGS="$DEVICE_FLAGS --device $dev"
done

docker run -d \
    --name robot_ui \
    --net=host \
    --cap-add=NET_ADMIN \
    -e DISPLAY="${DISPLAY}" \
    -v /tmp/.X11-unix:/tmp/.X11-unix \
    -v "${DATA_DIR}/plans":/app/plans \
    -v "${DATA_DIR}/stats":/app/stats \
    --group-add dialout \
    $DEVICE_FLAGS \
    --restart unless-stopped \
    "${IMAGE}"

echo ""
echo "✓ robot_ui is running"
echo "  Browser → http://localhost:8000"
echo "  Logs:     docker logs -f robot_ui"
echo "  Stop:     docker stop robot_ui"
echo "  Plans:    ${DATA_DIR}/plans"
