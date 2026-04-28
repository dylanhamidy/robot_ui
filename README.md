# Luxolis Robot Control

A web UI for controlling a Doosan robot arm model a0912. Load a plan (a list of joint moves), connect to the robot, run it. You can also teach by hand and record positions directly into a new plan.

## Prerequisites

- Ubuntu 22.04 (Jammy)
- ROS 2 Humble Desktop
- Doosan robot ROS 2 package — see the [doosan-robot-guides](https://github.com/Luxolis-AI/doosan-robot-guides) repo, specifically `DOOSAN_ROBOT_GUIDE.md`, for setup instructions

## Running

```bash
# One-time setup
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Start
.venv/bin/python3 server.py
```

Open `http://localhost:8000`. Browser needs internet access for CDN scripts (Alpine.js, Tailwind, SortableJS).

## How it works

The backend is one file: `server.py`. FastAPI, a WebSocket endpoint that streams terminal output to the browser, with no database. Plans are JSON files in `plans/`. Run history goes in `stats/`.

The frontend uses Alpine.js and Tailwind, loaded from CDN. No build step.

Starting a plan spawns a `ros2 run lux_dsr_control move_joint_node` subprocess and streams its stdout into the terminal panel. The UI watches that stream for sentinel strings like `[CONNECTED]`, `[DONE]`, and `[ERROR]` to update state. There are no separate polling endpoints.

The robot node lives in a sibling repo (`robot/lux_dsr_control`). This app launches it as a subprocess with `--plan-file <path>`. For live pose capture during hand teaching, the node POSTs back to this server.

## Hand teaching

Connect to the robot via the UI, open a plan, switch to the Hand Guide tab. Enable hand guide mode, move the arm to a position, press Record. Each recorded point appears in the step list. Steps can be reordered by dragging, and you can select any existing step and re-record it to update just that position.

Switching a step between MoveJ and MoveL resets its position values. The two move types use different coordinate systems (joint angles vs Cartesian), so the old values would be meaningless after a type switch anyway.

## Architecture

**Single-file backend.** `server.py` is one file with no internal modules. The app is small enough that splitting it would add navigation cost without adding clarity.

**Sentinel strings instead of polling.** The terminal stream already carries all state transitions. Parsing sentinels from that stream means one WebSocket connection handles both terminal output and UI state. A separate polling loop would just duplicate what the stream already tells us.

**Plans as flat JSON files.** No database. Plans are small, writes are infrequent, and being able to inspect or hand-edit a plan file is genuinely useful.

**Alpine.js, no bundler.** The UI is one reactive component. A build pipeline would cost more in setup and maintenance than it saves here.

**Full replace on save.** The modal loads all steps into memory. Save dumps the whole array back. No partial updates, no diffing. Simple enough that it does not need to be smarter.
