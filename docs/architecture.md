# System Architecture

This document explains how `robot_ui` talks to the Doosan robot, the Arduino
turntable/laser controller, and how a plan moves through the system from the
browser down to the robot controller.

It's the ROS2 counterpart to `robot_ui_v2`'s `robot_stack.md`: same idea,
different transport. v2 talks to a C++ DRFL daemon over stdin/stdout. This
repo talks to ROS2 nodes over `ros2 run` / `ros2 service call` subprocesses,
backed by [doosan-robot2](https://github.com/DoosanRobotics/doosan-robot2).

---

## 1. Layer overview

```
Browser (Alpine.js, ui/app.js)
   │  fetch() / WebSocket
   ▼
FastAPI server (server.py)
   │  subprocess: ros2 run / ros2 service call / ros2 launch
   ▼
ROS2 nodes (lux_dsr_control)
   - move_joint_node    : runs plan steps, one subprocess per "Start"
   - pose_capture_node  : persistent, hand guide + pose capture
   │  dsr_msgs2 services
   ▼
dsr_bringup2 driver (doosan-robot2)
   │  TCP, port 12345
   ▼
Doosan robot controller
```

A second branch runs alongside, independent of ROS2:

```
FastAPI server (server.py)
   │  pyserial, 9600 baud
   ▼
Arduino (arduino_test01.ino)
   - DM542A stepper driver (turntable)
   - laser relay
   - emergency-stop button
```

- **Browser**: the operator UI. Sends `fetch()` for discrete actions (connect,
  start/stop a plan, jog the turntable, record a hand-guide point) and keeps
  one WebSocket open for a live terminal feed plus state sentinels.
- **FastAPI server**: owns every subprocess and the serial port. It never
  talks to the robot controller directly; it spawns `move_joint_node` to run a
  plan and shells out to `ros2 service call` for one-off hand-guide actions.
- **move_joint_node**: a short-lived ROS2 node, one process per "Start". Runs
  every step in the plan (including Turntable/Laser steps, which it just
  signals back to the server for) and exits when the plan finishes, is
  stopped, or hits an emergency stop.
- **pose_capture_node**: a long-lived ROS2 node, started once on Connect.
  Provides `Trigger` services for hand guide (enable/disable compliance,
  record a point, capture a pose) and pushes recorded points to the server
  over HTTP.
- **dsr_bringup2**: the official Doosan ROS2 driver, launched via
  `ros2 launch dsr_bringup2 dsr_bringup2_rviz.launch.py`. This is what exposes
  `dsr_msgs2` services (`/dsr01/motion/...`, `/dsr01/aux_control/...`, etc.)
  and what actually talks to the controller over TCP.

---

## 2. Connection lifecycle

Triggered by `POST /api/robot/connect` (`server.py:475`):

1. **Workspace check**: if `~/ros2_ws/install/setup.bash` is missing, run
   `colcon build --packages-select lux_dsr_control --symlink-install` and
   stream the output. Skipped if `ROBOT_UI_SKIP_BUILD=1` (set in the Docker
   image, where the workspace is pre-built).
2. **Network setup** (real mode only): `ip addr flush/up/add` on the chosen
   interface so the PC sits on `192.168.0.x` next to the robot.
3. **Ping**: `ping -c 4 $ROBOT_IP`. Abort on failure.
4. **Launch RViz**: `ros2 launch dsr_bringup2 dsr_bringup2_rviz.launch.py
   mode:=real host:=$ROBOT_IP port:=12345 model:=$ROBOT_MODEL` (or
   `mode:=virtual` if `ROBOT_MODE=virtual`). Fire-and-forget, stored in
   `_rviz_proc`.
5. **Launch pose_capture_node**: `ros2 run lux_dsr_control pose_capture_node`.
   Fire-and-forget, stored in `_capture_proc`. This is the node hand guide and
   pose capture talk to for the rest of the session.

Steps 1-3 are awaited and any failure aborts the connection with `[ERROR]`.
Steps 4-5 are fire-and-forget: `[CONNECTED]` is broadcast once they're
spawned, not once they're ready.

---

## 3. Running a plan

`POST /api/robot/start` (`server.py:566`) sets `_active_plan` and hands off to
`_run_plan_task` (`server.py:578`) as a background task, so the HTTP response
returns immediately.

### Build guardrail

Independent of the connect-time check, `_run_plan_task` checks
`ros2 pkg list | grep lux_dsr_control` before spawning anything, and runs
`colcon build` if it's missing. Skipped entirely if every step in the plan is
`Turntable`/`Laser` (no ROS2 needed at all), and skipped if
`ROBOT_UI_SKIP_BUILD=1`.

### One process, all step types

Unlike earlier versions of this project, there's no segment-splitting anymore.
`_run_robot_segment` (`server.py:754`) writes the active (non-disabled) steps
to a temp plan file and spawns one `move_joint_node` subprocess:

```bash
ros2 run lux_dsr_control move_joint_node --plan-file <tmp.json> [--single-pass]
```

`move_joint_node` (`move_joint_node.py`) loops through `self.steps` itself,
including `Turntable` and `Laser` steps, it just doesn't have a robot motion
for those. `--single-pass` is passed when the plan's `loop` flag is `false`;
the node calls `os.kill(os.getpid(), signal.SIGINT)` after the last step
instead of wrapping back to index 0.

For pure Turntable/Laser plans (no robot steps at all), the server skips
`move_joint_node` entirely and drives the Arduino directly in a Python
`while True` loop (`server.py:720`).

### The [WAIT] / RESUME handshake

When `move_joint_node.send_next()` (`move_joint_node.py:90`) hits a
`Turntable` or `Laser` step, it logs `[WAIT] <idx>` and starts polling a
`threading.Event` every 50ms instead of calling a DRFL service. It does **not**
advance on its own.

The server's `_on_wait_line` callback (`server.py:669`, passed into
`_stream_proc` as `on_line`) watches the subprocess stdout for `[WAIT] <idx>`:

1. Looks up `active_steps[idx]`.
2. Calls `_run_turntable_segment` or `_run_laser_segment` (`server.py:787`,
   `server.py:821`), which talk to the Arduino directly over serial and block
   for the step's `duration`, polling `_stop_requested` every 100ms so Stop
   works mid-spin.
3. Writes `RESUME\n` to the node's stdin.

`move_joint_node`'s stdin-reader thread (`_stdin_reader`, `move_joint_node.py:63`)
sets the `threading.Event`, the timer fires `_advance()`, and the loop
continues to the next step.

### [STEP_START] and per-step lasers

For every robot step (`MoveJ`/`MoveL`/`MoveC`/`WeldStraight`), the node logs
`[STEP_START] <idx>` right as it dispatches the motion service call. The
server's `_on_wait_line` callback also watches for this and, if
`active_steps[idx].with_laser` is true, turns the laser on (after an optional
`laser_delay`) via the Arduino. Laser is turned off again as soon as the *next*
`[WAIT]` or `[STEP_START]` line arrives.

This means `with_laser` works on **any** step type, not just Turntable,
useful for "weld while moving" plans where the laser should track a specific
`MoveL`/`WeldStraight`/`MoveC` step.

### Parallel turntable mode

If the plan has a top-level `turntable_parallel` key, the server takes a
different path entirely (`server.py:632`):

- The node runs every step normally, looping if `loop: true`
  (`use_single_pass = not plan.loop`).
- No `[WAIT]` ever fires for Turntable steps because parallel plans don't
  *have* Turntable steps. The turntable spins continuously alongside the
  robot.
- `tt_step_callback` (`server.py:638`) watches `[STEP_START] <idx>` and checks
  `with_turntable[idx]` (built from each active step's `with_turntable` flag).
  If true: `ENABLE` → `DIR:<direction>` → `SPEED:<speed_us>` (from the plan's
  `turntable_parallel` config). If false: `DISABLE`.
- After the subprocess exits, the server force-sends `DISABLE` as a final
  cleanup.

Sequential (`Turntable`/`Laser` steps + `[WAIT]`) and parallel
(`turntable_parallel` + `with_turntable`) are mutually exclusive: the UI
hides the Turntable step type while parallel mode is on.

### Stop and run results

`POST /api/robot/stop` (`server.py:840`) sets `_stop_requested = True`, then:

- If a build is in progress, kills the build subprocess.
- If `move_joint_node` is mid-`Turntable`/`Laser` wait (no subprocess running
  but `_active_plan` is set), the flag alone is enough: the turntable
  segment's `while` loop checks it every 100ms.
- Otherwise sends `SIGINT` to the process group.

`_run_plan_task` records one of three results to `stats/<plan>.json`:

| Condition | Result |
|---|---|
| `_stop_requested` was set | `success` |
| Process exited on its own, `rc >= 0` | `fail` |
| Process killed by signal, `rc < 0` | `unknown` |

`rc=0` is **not** treated as success: a plan that exits cleanly without a stop
request did something unexpected (e.g. `--single-pass` ran out of steps when
the operator expected a loop).

---

## 4. Step types

All step types live in one plan JSON. `move_joint_node.send_next()`
(`move_joint_node.py:94`) switches on `step['type']`.

### MoveJ: joint move

One `dsr_msgs2/srv/MoveJoint` call to `/dsr01/motion/move_joint`. `pos` is six
joint angles in degrees, `mode=0` (absolute). `time` overrides `vel`/`acc` if
non-zero.

### MoveL: linear move

One `dsr_msgs2/srv/MoveLine` call to `/dsr01/motion/move_line`. `pos` is a
Cartesian pose `[X, Y, Z, A, B, C]` (ZYZ Euler angles), `ref=0` (base frame),
`mode=0` (absolute). `vel`/`acc` are `[linear, angular]` pairs.

### MoveC: circular arc

Two or three phases, depending on whether it's a full circle:

1. **Approach**: fast `MoveLine` to `pos_start` at `{100, 100}` vel/acc.
   `[STEP_START]` is *not* emitted yet, so any `with_laser` step doesn't fire
   during the repositioning move.
2. **Arc**: once the approach lands, `done_cb` (`move_joint_node.py:374`)
   immediately fires `_execute_circle_arc` (`move_joint_node.py:317`):
   `dsr_msgs2/srv/MoveCircle` to `/dsr01/motion/move_circle` with
   `pos=[via, end]`. `[STEP_START]` fires here.
3. **Return arc (full circle only)**: if `angle2 == 360`, the node computes a
   fourth point `D` from the circumcenter of `pos_start`/`pos_via`/`pos_end`
   (`_compute_return_via`, `move_joint_node.py:270`) and queues a second
   `MoveCircle` call `C → D → A` to close the loop. This works around
   `angle2=360` not behaving as a "full circle" parameter on its own.

### WeldStraight: two-point weld pass

Two phases:

1. **Approach**: fast `MoveLine` to `pos_a` at `{100, 100}` vel/acc, no
   `[STEP_START]`.
2. **Weld**: `done_cb` fires `_execute_weld_displacement`
   (`move_joint_node.py:192`): an absolute `MoveLine` to `pos_b` at the step's
   configured (usually much slower) `vel`/`acc`. `[STEP_START]` fires here, so
   `with_laser` turns the laser on right as the weld pass starts.

The server precomputes `displacement` and `distance_mm` when a `WeldStraight`
step is saved (`_compute_weld_displacement`, `server.py:253`). It rotates the
A→B vector into A's tool frame and zeroes the tool-Z component, giving an
in-plane distance for display in the UI. **The node does not use this value
for motion**: it moves to `pos_b` directly in absolute base-frame
coordinates, so the laser stays at A's recorded orientation throughout.

### Turntable / Laser: server-driven steps

No DSR service call at all. The node emits `[WAIT] <idx>` and blocks until the
server sends `RESUME` after driving the Arduino directly (see Section 3).

- `Turntable`: `ENABLE` → `DIR:<CW|CCW>` → `SPEED:<speed_us>` → wait
  `duration` → `DISABLE`. If `with_laser` is set, `LAS:ENA`/`LAS:DIS` bracket
  the spin too.
- `Laser`: `LAS:ENA` → wait `duration` → `LAS:DIS`. No motor commands.

---

## 5. Hand guide and pose capture

All of this goes through `pose_capture_node` (`/pose_capture_client`),
launched once at Connect and kept alive for the session.

| UI action | Endpoint | Service called |
|---|---|---|
| Enable hand guide | `POST /api/robot/hand_guide/enable` | `~/enable_hand_guide` → `SetRobotMode(MANUAL)` + `TaskComplianceCtrl(stx=[3000]*6)` |
| Disable hand guide | `POST /api/robot/hand_guide/disable` | `~/disable_hand_guide` → `ReleaseComplianceCtrl` + `SetRobotMode(AUTONOMOUS)` |
| Record a point | `POST /api/robot/hand_guide/record` | `~/record_point` → `GetCurrentPosj` + `GetCurrentPosx`, builds a `MoveJ`/`MoveL` step from whichever `current_type` is set |
| Set capture type | `POST /api/robot/hand_guide/type` | `ros2 param set /pose_capture_client current_type MoveJ\|MoveL` |
| Save plan | `POST /api/robot/hand_guide/save` | `~/save_plan` → POSTs a full plan to `/api/plans/import` |
| Clear captures | `POST /api/robot/hand_guide/clear` | `~/clear_plan` + clears `_captured_points` |

`record_point` runs with stdout/stderr to `DEVNULL`. Its `Trigger` response
already triggers `pose_capture_node` to POST the step to
`/api/robot/hand_guide/captured`, which broadcasts `[CAPTURE] <json>`.
Broadcasting the `ros2 service call` output too would double-add the step.

### capture_pose: single-shot pose read

`POST /api/robot/capture_pose` (`server.py:979`) calls
`~/capture_posx` (a `Trigger` whose `message` field carries a JSON
`[x, y, z, A, B, C]` array), parses it with a regex, and returns `{pos: [...]}`.
This is what the UI's "Capture" buttons use to fill `pos_a`/`pos_b` on a
`WeldStraight` step, or `pos_start`/`pos_via`/`pos_end` on a `MoveC` step,
from the robot's current position. No plan needs to be running.

---

## 6. Turntable, laser, and emergency stop (Arduino)

The Arduino (`arduino_test01.ino`) runs three things off one serial link at
9600 baud, newline-terminated commands:

| Command | Effect |
|---|---|
| `ENABLE` / `DISABLE` | DM542A `enPin` LOW/HIGH (turntable motor) |
| `DIR:CW` / `DIR:CCW` | `dirPin` + 10μs settle (DM542A t2 timing) |
| `SPEED:<N>` | pulse delay `N` μs, min 5 (DM542A t3/t4 timing) |
| `LAS:ENA` / `LAS:DIS` | laser relay on/off |

It also continuously reports the emergency-stop button state, unprompted:

```
EMG:1   # button released, normal
EMG:0   # button pressed
```

### Auto-detect

Two background asyncio tasks run for the whole server lifetime
(`lifespan`, `server.py:51`):

- `_turntable_scan_task` (`server.py:119`): every 2s, scans
  `serial.tools.list_ports.comports()` for known Arduino VIDs (`0x2341`
  Arduino, `0x1A86` CH340 clones, `0x0403` FTDI) and sets `_tt_pending_port`.
  The frontend's `pollTurntableStatus()` sees `pending_port` and opens the
  detection modal: Connect → `POST /api/turntable/confirm`, Not now →
  `POST /api/turntable/reject` (session-only skip list).
- `_turntable_watchdog_task` (`server.py:138`): every 2s, pokes
  `_turntable.in_waiting`; an exception (unplugged) closes the port.

### Emergency stop

`_tt_serial_read_task` (`server.py:190`) reads every line from the Arduino at
~200Hz. On `EMG:0` (button pressed), it calls `_handle_emergency`
(`server.py:148`):

1. Sends `DISABLE` and `LAS:DIS` to the Arduino directly. Turntable and laser
   stop immediately regardless of what the robot is doing.
2. If `move_joint_node` is running, writes `EMERGENCY\n` to its stdin instead
   of sending `SIGINT`. This is faster, since the node can call
   `dsr_msgs2/srv/MoveStop` (`stop_mode=1`, quick stop) itself
   (`_do_emergency_stop`, `move_joint_node.py:71`) before exiting via
   `SIGINT` on itself. A 2-second fallback task
   (`_emergency_fallback_kill`, `server.py:181`) sends `SIGINT` from the
   server side if the node hasn't exited by then.
3. Broadcasts `[EMERGENCY STOP]`, which the UI turns into a full-screen
   "release the button to resume" overlay.

On `EMG:1` with a prior `EMG:0` (button released), a 200ms debounce
(`_debounced_emg_clear`, `server.py:175`) broadcasts `[EMG_CLEAR]` if the state
is still `1`, which avoids flicker on a noisy button.

---

## 7. End-to-end flow

1. **Connect**: workspace check, network config, ping, RViz +
   `pose_capture_node` launch (Section 2). `[CONNECTED]` flips the UI into the
   connected state.
2. **Build a plan**: manual entry, or Hand Guide (drag the arm, Record) for
   `MoveJ`/`MoveL` waypoints, or Capture buttons for `WeldStraight`/`MoveC`
   reference points (Section 5). Turntable/Laser steps and `with_laser` flags
   are configured per step.
3. **Run**: `POST /api/robot/start` spawns `move_joint_node` (or drives the
   Arduino directly for turntable-only plans). The node loops through every
   step; `[WAIT]`/`RESUME` hands Turntable/Laser steps to the server
   (Section 3).
4. **Live feedback**: every sentinel (`[STEP_START]`, `[WAIT]`, `[CONNECTED]`,
   `[ERROR]`, `[DONE]`, `[EMERGENCY STOP]`, `[EMG_CLEAR]`, `[CAPTURE]`,
   `[PLAN_IMPORTED]`, `[STAT]`) goes out over `/ws/terminal` to every browser.
5. **Stop**: `POST /api/robot/stop` sets `_stop_requested`, which either
   short-circuits the current Turntable/Laser wait or sends `SIGINT` to the
   node.
6. **Emergency stop**: the Arduino button overrides all of the above
   immediately, independent of what step is running (Section 6).
7. **Disconnect**: kills any running plan, RViz, and `pose_capture_node`;
   clears captured points; broadcasts `[DISCONNECTED]`.
