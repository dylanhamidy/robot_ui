# Deployment Plan — Jetson

## Prerequisites

- NVIDIA Jetson (Orin / Xavier / Nano) running **JetPack 6.x** (Ubuntu 22.04)
- Internet access on Jetson during first setup only
- Jetson connected to same network as robot (`192.168.0.20`)
- Arduino turntable connected via USB

> **JetPack version is critical.** ROS2 Humble requires Ubuntu 22.04.
> JetPack 5.x = Ubuntu 20.04 = incompatible. Upgrade to JetPack 6.x first.

---

## Two deployment paths

| | Path A: Build on Jetson | Path B: Cross-compile on dev machine |
|---|---|---|
| Build machine | Jetson itself | x86 Linux/Mac dev machine |
| Complexity | Low | High (QEMU, slow) |
| Build time | ~25 min first run | ~60-90 min first run |
| Recommended | Yes | Only if Jetson unavailable |

---

## Path A — Build directly on Jetson (recommended)

### Step 1: Verify JetPack version

```bash
cat /etc/nv_tegra_release
# or
dpkg -l | grep nvidia-jetpack
```

Must show JetPack 6.x. If 5.x → reflash with JetPack 6.x before continuing.

### Step 2: Install Docker on Jetson

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker

# Verify
docker --version
```

### Step 3: Install build tools (first time only)

```bash
npm install -g terser html-minifier-terser
# or run:
bash build/prereqs.sh
```

### Step 4: Clone repo onto Jetson

```bash
git clone <your-repo-url> ~/robot_ui
cd ~/robot_ui
```

Or transfer via USB/SCP:

```bash
# From dev machine
scp -r ~/Work/Luxolis/robot_ui user@jetson-ip:~/robot_ui
```

### Step 5: Build image (native ARM64)

```bash
cd ~/robot_ui
bash build/docker-build.sh 1.0.0
```

First build ~25 min (downloads ROS2 base, compiles DSR packages, runs Nuitka).
Subsequent builds ~3 min (Docker layer cache).

### Step 6: Run

```bash
bash bootstrap.sh
# Browser → http://localhost:8000
# Or from another machine → http://<jetson-ip>:8000
```

---

## Path B — Cross-compile on dev machine, ship to Jetson

Use only if you cannot build on the Jetson directly (e.g. Jetson Nano running JetPack 4.x).

> **Dockerfile note:** `arch=amd64` was replaced with `$(dpkg --print-architecture)` in both ROS2 apt source lines. This runs inside the container at build time — when buildx targets `linux/arm64`, it returns `arm64` automatically. Normal amd64 builds are unaffected.

### Step 1: Enable ARM64 builds on dev machine (one-time)

```bash
# Install QEMU for ARM64 emulation
docker run --privileged --rm tonistiigi/binfmt --install arm64

# Create buildx builder
docker buildx create --name jetson-builder --use
docker buildx inspect --bootstrap

# Verify
docker buildx ls
```

### Step 2: Prepare build context

```bash
cd ~/Work/Luxolis/robot_ui

# Copy lux_dsr_control into build context (docker-build.sh does this too)
cp -r ~/ros2_ws/src/doosan-robot-guides/lux_dsr_control ./lux_dsr_control

# Minify JS + HTML
bash build/minify.sh
```

### Step 3: Build ARM64 image

Do NOT use `docker-build.sh` — it calls `docker build` (amd64 only). Run buildx directly:

```bash
docker buildx build \
    --platform linux/arm64 \
    --tag luxolis/robot_ui:1.0.0 \
    --load \
    .

# Clean up build context
rm -rf ./lux_dsr_control
```

> Build will take 60-90 min — QEMU emulates ARM64 instruction by instruction. Plan accordingly.

### Step 4: Export image

```bash
mkdir -p dist
docker save luxolis/robot_ui:1.0.0 | gzip > dist/robot_ui_v1.0.0-arm64.tar.gz
```

### Step 5: Transfer to Jetson

```bash
# Via SCP (Jetson must be on same network)
scp dist/robot_ui_v1.0.0-arm64.tar.gz user@jetson-ip:~/
scp bootstrap.sh user@jetson-ip:~/

# Or USB drive — copy robot_ui_v1.0.0-arm64.tar.gz + bootstrap.sh to USB,
# plug into Jetson, copy to ~/
```

> **bootstrap.sh:** No changes needed. It auto-detects `robot_ui_*.tar.gz` by glob and uses image tag `luxolis/robot_ui:1.0.0` which matches the build tag above.

### Step 6: Load and run on Jetson

```bash
# Docker already installed (Jetson Nano ships with Docker 20.x on JetPack 4.x)

# Load image
docker load < ~/robot_ui_v1.0.0-arm64.tar.gz

# Run
bash bootstrap.sh
```

---

## Jetson-specific bootstrap.sh changes

Update `bootstrap.sh` — change image name to match ARM64 tag:

```bash
IMAGE="luxolis/robot_ui:1.0.0-arm64"
```

Add display handling. Jetson with monitor attached:

```bash
docker run -d \
    --name robot_ui \
    --net=host \
    --cap-add=NET_ADMIN \
    -e DISPLAY="${DISPLAY}" \
    -e QT_QPA_PLATFORM=xcb \          # force X11 on Wayland sessions
    -v /tmp/.X11-unix:/tmp/.X11-unix \
    -v "${DATA_DIR}/plans":/app/plans \
    -v "${DATA_DIR}/stats":/app/stats \
    --restart unless-stopped \
    "${IMAGE}"
```

Jetson headless (no monitor — RViz disabled):

```bash
docker run -d \
    --name robot_ui \
    --net=host \
    --cap-add=NET_ADMIN \
    -e QT_QPA_PLATFORM=offscreen \    # RViz won't display but won't crash server
    -v "${DATA_DIR}/plans":/app/plans \
    -v "${DATA_DIR}/stats":/app/stats \
    --restart unless-stopped \
    "${IMAGE}"
```

---

## Serial port permissions (turntable)

Jetson may need `dialout` group for serial access:

```bash
sudo usermod -aG dialout $USER
# Logout and login again
```

Verify Arduino detected:

```bash
ls /dev/ttyACM* /dev/ttyUSB*
```

---

## Dockerfile changes required

None. `osrf/ros:humble-desktop` and `python:3.11-slim` both publish official ARM64 images. Docker/buildx pulls correct variant automatically based on target platform.

---

## What works identically on Jetson

| Feature | Status |
|---|---|
| `--net=host` | Works — real Linux kernel, real network interface |
| `--cap-add=NET_ADMIN` | Works — real Linux kernel capability |
| Robot ethernet (`192.168.0.20`) | Works — direct network access |
| ROS2 DDS multicast | Works — real network stack |
| Serial `/dev/ttyACM0` | Works — real USB passthrough |
| RViz2 | Works if monitor attached |
| Web UI (`localhost:8000`) | Works — accessible from network too |

---

## Known limitations on Jetson

| Issue | Mitigation |
|---|---|
| RViz2 requires display | Use headless mode (`QT_QPA_PLATFORM=offscreen`) or attach monitor |
| First build slow on Jetson (~25 min) | Run once, cache persists for future rebuilds |
| Jetson thermal throttling under sustained load | Ensure adequate cooling during colcon build |
| JetPack 5.x incompatible | Must be JetPack 6.x (Ubuntu 22.04) |

---

## Quick reference

```bash
# Check running container
docker ps

# View live logs
docker logs -f robot_ui

# Stop
docker stop robot_ui

# Restart
docker start robot_ui

# Full restart (re-run bootstrap)
docker rm -f robot_ui && bash bootstrap.sh

# Check plans (persisted outside container)
ls ~/robot_ui_data/plans/
```
