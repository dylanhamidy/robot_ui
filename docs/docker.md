# Running the Robot UI with Docker

## Standard deployment (bootstrap.sh)

This is the normal way to run on a client machine. Requires a pre-built image tarball (`robot_ui_*.tar.gz`) in the same directory as `bootstrap.sh`.

```bash
bash bootstrap.sh
```

`bootstrap.sh` will:
1. Install Docker if missing
2. Load the image from `robot_ui_*.tar.gz`
3. Start the container with correct flags (network, USB devices, data volumes)

Open UI at `http://localhost:8000`.

### Custom robot IP or model

Pass env vars before the script — no rebuild needed:

```bash
ROBOT_IP=192.168.1.20 PC_IP=192.168.1.50 ROBOT_MODEL=a0509 bash bootstrap.sh
```

| Variable | Default | Description |
|---|---|---|
| `ROBOT_IP` | `192.168.0.20` | Robot controller IP |
| `PC_IP` | `192.168.0.50` | IP assigned to PC's ethernet interface |
| `ROBOT_MODEL` | `a0912` | Doosan model passed to `dsr_bringup2` |

---

## Building the image from source (dev)

### 1) Prerequisites

Install minifier tools once:

```bash
npm install -g terser html-minifier-terser
```

### 2) Build UI assets

```bash
cd ~/Work/Luxolis/robot_ui
bash build/minify.sh
```

Creates `ui_dist/` — required by the Dockerfile.

### 3) Build the image

```bash
docker build -t robot-ui .
```

Takes several minutes (clones Doosan repo, compiles ROS2 packages, compiles server with Nuitka).

### 4) Run the container

```bash
docker run -d \
  --name robot-ui \
  --network host \
  --cap-add=NET_ADMIN \
  --privileged \
  -v ~/robot_ui_data/plans:/app/plans \
  -v ~/robot_ui_data/stats:/app/stats \
  robot-ui
```

With custom IPs:

```bash
docker run -d \
  --name robot-ui \
  --network host \
  --cap-add=NET_ADMIN \
  --privileged \
  -e ROBOT_IP=192.168.1.20 \
  -e PC_IP=192.168.1.50 \
  -e ROBOT_MODEL=a0509 \
  -v ~/robot_ui_data/plans:/app/plans \
  -v ~/robot_ui_data/stats:/app/stats \
  robot-ui
```

---

## Useful commands

```bash
docker logs -f robot_ui        # stream logs
docker stop robot_ui           # stop
docker start robot_ui          # restart
docker rm robot_ui             # remove container
```
