# Running the Robot UI with Docker

## 1) Prerequisites

Install minifier tools once:

```bash
npm install -g terser html-minifier-terser
```

## 2) Build UI assets

```bash
cd ~/Work/Luxolis/robot_ui
bash build/minify.sh
```

Creates `ui_dist/` — required by the Dockerfile.

## 3) Build the image

```bash
docker build -t robot-ui .
```

Takes several minutes (clones Doosan repo, compiles ROS2 packages, compiles server with Nuitka).

## 4) Run the container

```bash
docker run -d \
  --name robot-ui \
  --network host \
  --privileged \
  -v ~/robot_ui_plans:/app/plans \
  -v ~/robot_ui_stats:/app/stats \
  robot-ui
```

| Flag | Reason |
|---|---|
| `--network host` | Reach robot at `192.168.0.20` without bridge NAT |
| `--privileged` | USB access for Arduino turntable (`/dev/ttyACM0`) |
| `-v ~/robot_ui_plans:/app/plans` | Persist saved plans across restarts |
| `-v ~/robot_ui_stats:/app/stats` | Persist run history across restarts |

## 5) Open UI

```
http://localhost:8000
```

## Useful commands

```bash
docker logs -f robot-ui        # stream logs
docker stop robot-ui           # stop
docker start robot-ui          # restart
docker rm robot-ui             # remove container
```
