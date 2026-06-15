# Troubleshooting Guide

## General — always start here

```bash
docker logs -f robot_ui
```

---

## Robot won't connect

**Symptom:** UI shows connected but robot not responding, or connect step fails.

**Test:**
```bash
docker exec -it robot_ui bash -c \
  "source /opt/ros/humble/setup.bash && \
   source /ros2_ws/install/setup.bash && \
   ros2 launch dsr_bringup2 dsr_bringup2_rviz.launch.py \
   mode:=real host:=192.168.0.20 port:=12345 model:=a0912"
```

**Possible causes:**
- RViz/DSR driver crashed silently (fire-and-forget, no UI error shown)
- Missing shared library (Poco, ros2-control)
- Robot not powered on or wrong IP
- Wrong network interface name (default `enp2s0`)

**Fix:** Check exact error from command above. If missing lib → add to Stage 3 `apt-get install`.

---

## Plan fails to run

**Symptom:** Start plan → immediate error in terminal, or `[ERROR]` in terminal output.

**Test:**
```bash
docker exec -it robot_ui bash -c \
  "source /opt/ros/humble/setup.bash && \
   source /ros2_ws/install/setup.bash && \
   ros2 pkg list | grep lux_dsr"
```

**Possible causes:**
- `lux_dsr_control` not built correctly in Stage 1
- `dsr_msgs2` or other Doosan dep missing from build
- `move_joint_node` service not available (robot driver not running)

**Fix:** Verify `lux_dsr_control` appears in pkg list. If missing → rebuild image. If pkg exists but service unavailable → robot driver (DSR bringup) not running.

---

## Turntable not detected

**Symptom:** Arduino modal never appears, or turntable connect fails.

**Test:**
```bash
# Check devices passed through to container
docker exec -it robot_ui ls /dev/ttyACM* /dev/ttyUSB* 2>/dev/null

# Check dialout group
docker exec -it robot_ui groups
```

**Possible causes:**
- Arduino not plugged in before `bootstrap.sh` ran (device not passed through)
- Device at `/dev/ttyUSB1` or other non-standard path
- `dialout` group GID mismatch

**Fix:**
```bash
# Restart container after plugging Arduino
docker rm -f robot_ui && bash bootstrap.sh

# If permission error persists — use privileged mode (add to bootstrap.sh docker run)
--privileged
```

---

## UI loads blank or broken

**Symptom:** Browser shows empty page or JS errors in console.

**Test:**
```bash
docker exec -it robot_ui ls /app/ui/
```

**Possible causes:**
- `ui_dist/` was in `.dockerignore` (fixed — but check if reverted)
- `minify.sh` failed silently before docker build
- Nuitka `BASE` path wrong (binary can't find `ui/` folder)

**Fix:**
```bash
# Rebuild UI only (Stages 1+2 cached)
bash build/minify.sh
docker build --tag luxolis/robot_ui:1.0.0 .
```

---

## Plans / stats not saving

**Symptom:** Plans disappear after container restart.

**Test:**
```bash
docker inspect robot_ui | grep -A 10 Mounts
ls ~/robot_ui_data/plans/
```

**Possible causes:**
- Volume mount not applied (bootstrap.sh not used, container started manually)
- `~/robot_ui_data/` permissions issue

**Fix:** Always start container via `bootstrap.sh`. Never `docker run` manually without volume flags.

---

## Missing shared library at runtime

**Symptom:** `ros2 launch` or `ros2 run` crashes with `libXXX.so not found`.

**Test:**
```bash
docker exec -it robot_ui ldd \
  /ros2_ws/install/dsr_hardware2/lib/dsr_hardware2/libdsr_hardware2.so 2>/dev/null \
  | grep "not found"
```

**Fix:** Add missing lib to Stage 3 `apt-get install` in Dockerfile. Common ones:
- `libpocofoundation80`
- `libpoconet80`
- `libpocoutil80`
- `libpocoxml80`
- `ros-humble-xacro`
- `ros-humble-ros2-control`
- `ros-humble-ros2-controllers`

---

## RViz crashes on Wayland

**Symptom:** RViz fails to open on Ubuntu with Wayland session.

**Fix:** Add to `docker run` in `bootstrap.sh`:
```bash
-e QT_QPA_PLATFORM=xcb
```

---

## Nuitka binary can't find ui/ or plans/

**Symptom:** Server starts but returns 404 on all routes, or crashes on startup.

**Cause:** Nuitka `--onefile` extracts to a temp dir. `__file__` points there instead of `/app`. Already handled in `server.py`:
```python
BASE = Path(__file__).parent
if not (BASE / "ui").exists():
    BASE = Path(sys.argv[0]).resolve().parent
```

**If still failing:** Check `/app/ui/` exists in container. If missing → `COPY ui_dist/` step in Dockerfile failed.

---

## Quick rebuild reference

| What changed | Command |
|---|---|
| `ui/app.js` or `ui/index.html` only | `bash build/minify.sh && docker build --tag luxolis/robot_ui:1.0.0 .` |
| `server.py` | `docker build --tag luxolis/robot_ui:1.0.0 .` |
| `lux_dsr_control/` | `bash build/docker-build.sh 1.0.0` (full rebuild) |
| `Dockerfile` Stage 1 | `bash build/docker-build.sh 1.0.0` (full rebuild, ~25 min) |
