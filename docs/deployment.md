# Deployment Process

How to get `robot_ui` from a dev checkout to a running client machine. For
the reasoning behind Docker/Ubuntu/ROS2 being required at all, see
`architecture_decisions.md`. For ARM64/Jetson specifics, see
`jetson_deployment_plan.md`.

---

## 1. Local dev run (no Docker)

For working on the UI/server itself, on a machine that already has
ROS2 Humble + `lux_dsr_control` built:

```bash
cd ~/Work/Luxolis/robot_ui
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python3 server.py
```

Open `http://localhost:8000`. Connect runs the workspace build check itself
the first time, so `~/ros2_ws` doesn't need to be pre-built.

---

## 2. Building a client image

This is the path for shipping a build to a client PC. Run on a dev machine
with Docker and the same architecture as the target (x86_64 for a normal PC).

### One-time setup

```bash
bash build/prereqs.sh   # npm install -g terser html-minifier-terser
```

### Build and export

```bash
cd ~/Work/Luxolis/robot_ui

# 1. Copies lux_dsr_control into the build context, minifies ui/, docker build
bash build/docker-build.sh 1.0.0

# 2. Saves the image to dist/robot_ui_v1.0.0.tar.gz
bash build/docker-save.sh 1.0.0
```

What `docker-build.sh` does, end to end:

1. Copies `~/ros2_ws/src/doosan-robot-guides/lux_dsr_control` into
   `./lux_dsr_control` (skipped if already present, e.g. it was transferred
   via SCP for an ARM64 build).
2. `build/minify.sh` → `ui_dist/` (minified `app.js` + `index.html`).
3. `docker build` runs the three-stage `Dockerfile`:
   - Stage 1: clones `doosan-robot2`, builds `lux_dsr_control` and the DSR
     ROS2 packages with colcon.
   - Stage 2: compiles `server.py` to a standalone binary with Nuitka.
   - Stage 3: runtime image (`osrf/ros:humble-desktop` + the compiled binary
     - `ui_dist/`).
4. Removes the temporary `./lux_dsr_control` copy.

The result is `dist/robot_ui_v1.0.0.tar.gz`, the artifact that ships to the
client.

---

## 3. Running on the client

Copy `dist/robot_ui_v1.0.0.tar.gz` and `bootstrap.sh` to the client machine,
in the same directory, then:

```bash
bash bootstrap.sh
```

`bootstrap.sh` handles everything from there:

1. Installs Docker if it's missing.
2. Loads the image from the `robot_ui_*.tar.gz` in the same directory.
3. Creates `~/robot_ui_data/{plans,stats}` for persistent storage.
4. Allows X11 access for RViz (`xhost +local:docker`).
5. Starts the container with `--net=host`, `--cap-add=NET_ADMIN`, the data
   volumes, and any detected `/dev/ttyACM*`/`/dev/ttyUSB*` serial devices for
   the turntable.

Open `http://localhost:8000`.

### Custom robot IP, PC IP, or model

Set these as environment variables before `bootstrap.sh` runs, no rebuild
needed:

```bash
ROBOT_IP=192.168.1.20 PC_IP=192.168.1.50 ROBOT_MODEL=a0509 bash bootstrap.sh
```

| Variable      | Default        | Description                                |
| ------------- | -------------- | ------------------------------------------ |
| `ROBOT_IP`    | `192.168.0.20` | Robot controller IP                        |
| `PC_IP`       | `192.168.0.50` | IP assigned to the PC's ethernet interface |
| `ROBOT_MODEL` | `a0912`        | Doosan model, passed to `dsr_bringup2`     |

### Updating

```bash
docker rm -f robot_ui
bash bootstrap.sh   # picks up the new tarball if it's been replaced
```

Plans and stats live in `~/robot_ui_data/` on the host, outside the
container, so they survive an update.

---

## 4. Jetson / ARM64

Same image and `bootstrap.sh`, but the image has to be built for `arm64`.
Two options, both covered in detail in `extras/jetson_deployment_plan.md`:

- **Build on the Jetson itself** (recommended): same `build/docker-build.sh`
  flow as above, run directly on the Jetson. About 25 minutes for the first
  build.
- **Cross-compile on a dev machine with `buildx`**: needed only if the
  Jetson can't build natively (e.g. a Jetson Nano stuck on JetPack 4.x,
  which can't run ROS2 Humble at all and isn't a supported target).

Jetson must be on **JetPack 6.x (Ubuntu 22.04)**. JetPack 5.x and earlier are
Ubuntu 20.04 and can't run ROS2 Humble. See `architecture_decisions.md` §4
for the hardware compatibility table.

---

## 5. Useful commands

```bash
docker logs -f robot_ui        # stream logs
docker stop robot_ui           # stop
docker start robot_ui          # restart without re-running bootstrap
docker rm -f robot_ui           # remove container (keeps the image)
ls ~/robot_ui_data/plans/       # plans persisted outside the container
```

If something goes wrong during connect or a plan run, check
`troubleshooting.md` first.
