# Servers, Functions, and Features

A reference list of every process this app runs, every API endpoint it
exposes, and the feature set built on top of them. For the bigger picture
(how these pieces fit together), see `architecture.md`.

---

## 1. Servers and processes

| Process | Started by | Lifetime | Role |
|---|---|---|---|
| `server.py` (FastAPI/uvicorn, port 8000) | manual run, Docker container, or systemd | whole session | Serves the UI, REST API, and `/ws/terminal`. Owns every subprocess and the Arduino serial port. |
| `move_joint_node` (`/move_joint_client`) | `POST /api/robot/start` | one per plan run | ROS2 node that executes plan steps against `dsr_msgs2` services. Exits on plan finish, Stop, or emergency stop. |
| `pose_capture_node` (`/pose_capture_client`) | `POST /api/robot/connect` | until disconnect | ROS2 node providing hand guide and pose capture services. Pushes captured points back to `server.py` over HTTP. |
| `dsr_bringup2` + RViz | `POST /api/robot/connect` | until disconnect | Official Doosan ROS2 driver. Exposes the `dsr_msgs2` services everything else calls, and talks to the robot controller over TCP on port 12345. |
| Arduino (`arduino_test01.ino`) | external, plugged in over USB | always on | DM542A turntable driver, laser relay, and emergency-stop button. Talks over serial at 9600 baud. |

`server.py` never talks to the robot controller or the Arduino's neighbors
directly. ROS2 access always goes through a subprocess (`ros2 run`,
`ros2 service call`, `ros2 launch`, `ros2 param set`, `ros2 pkg list`); the
Arduino is the only thing reached directly, via `pyserial`.

---

## 2. API reference

All endpoints are on `server.py`. Bodies are JSON unless noted.

### Plans

| Endpoint | Body | Notes |
|---|---|---|
| `GET /api/plans` | | List all plans, each with its run stats attached. |
| `POST /api/plans` | `{name, steps, loop, turntable_parallel?}` | Create a new plan. 400 if the name exists. |
| `GET /api/plans/{name}` | | Fetch one plan. |
| `PUT /api/plans/{name}` | `{steps, loop, turntable_parallel?}` | Replace a plan's steps/config. |
| `DELETE /api/plans/{name}` | | Delete a plan and its stats file. |
| `POST /api/plans/import` | `{name, steps, created_at?}` | Upsert by name. Used by the file picker and by `pose_capture_node`'s save action. Broadcasts `[PLAN_IMPORTED] <name>`. |

### Robot connection and execution

| Endpoint | Body | Notes |
|---|---|---|
| `POST /api/robot/connect` | `{sudo_password, interface}` | Runs the connect sequence (workspace check, network setup, ping, RViz, `pose_capture_node`). Streams progress over `/ws/terminal`. |
| `POST /api/robot/disconnect` | | Kills any running plan, RViz, and `pose_capture_node`. Clears captured points. |
| `GET /api/robot/status` | | `{connected, running, active_plan}`. |
| `POST /api/robot/start` | `{plan_name}` | 409 if a plan is already running. Spawns `_run_plan_task` and returns immediately. |
| `POST /api/robot/stop` | | Sets `_stop_requested`, then signals whatever's running (build, robot subprocess, or turntable/laser wait). |

### Hand guide and pose capture

| Endpoint | Body | Notes |
|---|---|---|
| `POST /api/robot/hand_guide/enable` | | Switches the robot to MANUAL mode and enables compliance. |
| `POST /api/robot/hand_guide/disable` | | Releases compliance, restores AUTONOMOUS mode. |
| `POST /api/robot/hand_guide/type` | `{move_type}` | `MoveJ` or `MoveL`, sets which type the next recorded point becomes. |
| `POST /api/robot/hand_guide/record` | | Records the current joint and Cartesian pose as a step. Runs silently; the resulting `[CAPTURE]` broadcast comes via `/captured`. |
| `POST /api/robot/hand_guide/captured` | (internal) | Called by `pose_capture_node` after each record. Appends to `_captured_points` and broadcasts `[CAPTURE] <json>`. |
| `GET /api/robot/hand_guide/points` | | Returns `{points: _captured_points}`. |
| `DELETE /api/robot/hand_guide/points` | | Clears `_captured_points`. Called when the plan modal opens. |
| `POST /api/robot/hand_guide/clear` | | Clears `_captured_points` on both sides. |
| `POST /api/robot/hand_guide/save` | | Tells `pose_capture_node` to POST a full plan to `/api/plans/import`. |
| `POST /api/robot/capture_pose` | | One-shot read of the current TCP pose `[x, y, z, A, B, C]`. Used to fill `pos_a`/`pos_b`/`pos_start`/`pos_via`/`pos_end` on `WeldStraight`/`MoveC` steps. 409 if not connected. |

### Turntable / Arduino

| Endpoint | Body | Notes |
|---|---|---|
| `POST /api/turntable/connect` | `{port, baud}` | Manual connect. 403 + sets `_tt_pending_port` on a permission error. |
| `POST /api/turntable/disconnect` | | Sends `DISABLE`, closes the port. |
| `GET /api/turntable/status` | | `{connected, enabled, direction, speed, pending_port, rejected_ports, emg_state}`. |
| `GET /api/turntable/emg` | | `{state}`: `None`/`0`/`1`. |
| `POST /api/turntable/confirm` | `{port, sudo_password}` | Connects an auto-detected port. If permission fails and a password is given, runs `sudo chmod a+rw <port>` first. |
| `POST /api/turntable/reject` | `{port}` | Adds the port to a session-only skip list so auto-detect stops offering it. |
| `POST /api/turntable/enable` | | Sends `DIR`, `SPEED`, then `ENABLE`. |
| `POST /api/turntable/disable` | | Sends `DISABLE`. |
| `POST /api/turntable/direction` | `{direction}` | `CW` or `CCW`. |
| `POST /api/turntable/speed` | `{delay_us}` | Pulse delay in microseconds, minimum 5 (DM542A timing floor; the API rejects below 3). |

### WebSocket

| Endpoint | Notes |
|---|---|
| `/ws/terminal` | Single shared terminal stream. Every `_broadcast()` call (subprocess stdout lines, sentinels, status messages) goes out to every connected client. Dead connections are purged when a new client joins. |

---

## 3. Sentinel reference

The frontend drives almost all of its state off sentinel strings in the
`/ws/terminal` stream rather than polling. Full list:

| Sentinel | Meaning |
|---|---|
| `[STEP] ...` / `[INFO] ...` | Connect-sequence progress (workspace build, network config, ping, RViz, capture node). |
| `[CONNECTED]` | Connect sequence finished. |
| `[DISCONNECTED]` | Disconnected. |
| `[STEP_START] N` | `move_joint_node` is dispatching step `N` (robot motion). Drives `with_laser`. |
| `[WAIT] N` | `move_joint_node` is parked on a Turntable/Laser step, waiting for `RESUME`. |
| `[STAT] Finished in Xs` | Plan run elapsed time. |
| `[DONE] ...` | Plan run finished (success/fail/unknown). |
| `[ERROR] ...` | A step failed (build error, connect failure, motion failure). |
| `[CAPTURE] <json>` | A hand-guide point was recorded. |
| `[PLAN_IMPORTED] <name>` | A plan was created/overwritten via import. |
| `[EMG] <0\|1>` | Raw emergency-button state from the Arduino. |
| `[EMERGENCY STOP]` | Emergency button pressed, shows full-screen overlay. |
| `[EMG_CLEAR]` | Emergency button released (debounced). |

---

## 4. Feature summary

**Plan editor**
- Step types: `MoveJ`, `MoveL`, `MoveC` (including full-circle arcs), `WeldStraight`, `Turntable`, `Laser`.
- Drag-to-reorder steps, per-step enable/disable toggle, per-step `delay`.
- `with_laser` + `laser_delay` on any step type, for "fire the laser during this move" plans.
- Plan-level `loop` toggle.

**Hand guide**
- Drag the arm by hand (compliance mode), press Record to capture a `MoveJ`/`MoveL` waypoint.
- Toggle capture type (`MoveJ`/`MoveL`) live.
- "Save" pushes all recorded points as a new plan via import.

**Pose capture for weld/arc steps**
- One-click "Capture" buttons fill `pos_a`/`pos_b` (`WeldStraight`) or `pos_start`/`pos_via`/`pos_end` (`MoveC`) from the robot's current pose, no plan run required.
- Live weld displacement and distance (mm) shown for `WeldStraight` steps.

**Turntable control**
- Manual page: enable/disable, direction, speed (log-scale slider + editable µs readout).
- Sequential mode: `Turntable`/`Laser` steps interleaved with robot steps, server-driven via the `[WAIT]`/`RESUME` handshake.
- Parallel mode: turntable spins continuously alongside the robot; per-step `with_turntable` checkbox marks which robot steps run with the turntable on.
- Auto-detect: scans for known Arduino USB VIDs, prompts to connect, remembers rejected ports for the session.
- Permission handling: 403 on `/dev/ttyACM0` permission errors, with an inline sudo-password prompt that runs `chmod a+rw`.

**Safety**
- Emergency-stop button on the Arduino: instantly disables turntable and laser, sends a quick-stop to the robot, and shows a full-screen "release to resume" overlay.
- Browser-disconnect watchdog: an idle/closed tab triggers a graceful shutdown of robot subprocesses after an 8-second grace period.
- Server-exit and `atexit` handlers guarantee robot subprocesses and the turntable are stopped even if the server itself crashes.

**Run history**
- Every plan run is recorded to `stats/<plan>.json`: total runs, success/fail/unknown counts, and a timestamped history.
- Results shown next to each plan in the list.

**UI**
- Live terminal panel (auto-classified log lines: success/error/info/log).
- Light/dark theme.
- Two pages: Robot Control and Turntable Control, switchable without losing connection state.
