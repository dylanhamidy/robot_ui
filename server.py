import atexit
import asyncio
import json
import os
import signal
import subprocess
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    serial = None

ARDUINO_VIDS = {
    0x2341,  # Arduino LLC (Uno, Mega, etc.)
    0x1A86,  # QinHeng CH340/CH341 (clones/Nanos)
    0x0403,  # FTDI FT232 (older boards)
}

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE = Path(__file__).parent
PLANS_DIR = BASE / "plans"
STATS_DIR = BASE / "stats"
PLANS_DIR.mkdir(exist_ok=True)
STATS_DIR.mkdir(exist_ok=True)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    scan = asyncio.create_task(_turntable_scan_task())
    watchdog = asyncio.create_task(_turntable_watchdog_task())
    yield
    scan.cancel()
    watchdog.cancel()
    try:
        await asyncio.gather(scan, watchdog)
    except asyncio.CancelledError:
        pass
    _kill_robot_procs()


app = FastAPI(lifespan=lifespan)

# ── state ──────────────────────────────────────────────────────────────────
_active_proc: Optional[subprocess.Popen] = None
_active_plan: Optional[str] = None
_build_proc = None  # asyncio.subprocess.Process during colcon build
_rviz_proc: Optional[subprocess.Popen] = None
_capture_proc: Optional[subprocess.Popen] = None
_captured_points: list = []
_connected: bool = False
_stop_requested: bool = False
_ws_clients: list[WebSocket] = []
_disconnect_task: Optional[asyncio.Task] = None

# ── turntable state ────────────────────────────────────────────────────────
_turntable = None  # serial.Serial instance when connected
_tt_enabled: bool = False
_tt_direction: str = "CW"
_tt_speed: int = 50  # microseconds between pulses
_tt_pending_port: Optional[str] = None  # detected Arduino port awaiting user confirm
_tt_rejected_ports: set = set()  # ports user declined; auto-scanner skips these


# ── helpers ────────────────────────────────────────────────────────────────

def _kill_robot_procs():
    """Kill all robot subprocesses. Synchronous and idempotent — safe to call from atexit."""
    global _active_proc, _rviz_proc, _capture_proc
    for proc in filter(None, [_active_proc, _rviz_proc, _capture_proc]):
        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGINT)
                proc.wait(timeout=3)
            except (ProcessLookupError, subprocess.TimeoutExpired, OSError):
                pass

def _close_turntable():
    global _turntable, _tt_enabled
    if _turntable is not None:
        try:
            if _turntable.is_open:
                _turntable.write(b"DISABLE\n")
                _turntable.close()
        except Exception:
            pass
    _turntable = None
    _tt_enabled = False

atexit.register(_kill_robot_procs)
atexit.register(_close_turntable)

async def _turntable_scan_task():
    """Scan for Arduino by VID every 2 s; set _tt_pending_port when found."""
    global _tt_pending_port
    while True:
        if serial is not None and not (_turntable and _turntable.is_open):
            try:
                ports = serial.tools.list_ports.comports()
                port_devices = {p.device for p in ports}
                if _tt_pending_port and _tt_pending_port not in port_devices:
                    _tt_pending_port = None  # port disappeared while modal open
                if _tt_pending_port is None:
                    for p in ports:
                        if p.vid in ARDUINO_VIDS and p.device not in _tt_rejected_ports:
                            _tt_pending_port = p.device
                            break
            except Exception:
                pass
        await asyncio.sleep(2)

async def _turntable_watchdog_task():
    """Detect Arduino unplug by polling in_waiting every 2 s."""
    while True:
        if _turntable and _turntable.is_open:
            try:
                _ = _turntable.in_waiting
            except Exception:
                _close_turntable()
        await asyncio.sleep(2)

async def _schedule_safety_shutdown():
    """Grace-period watchdog: if no browser client reconnects within 8 s, stop the robot."""
    await asyncio.sleep(8)
    global _connected, _active_plan, _build_proc
    if _ws_clients:
        return
    if _build_proc is not None:
        try:
            _build_proc.kill()
        except (ProcessLookupError, OSError):
            pass
    _kill_robot_procs()
    _connected = False
    _active_plan = None

def _plan_path(name: str) -> Path:
    return PLANS_DIR / f"{name}.json"

def _stats_path(name: str) -> Path:
    return STATS_DIR / f"{name}.json"

def _coerce_step_floats(step: dict) -> dict:
    """Ensure all numeric fields in a step are stored as the correct Python types."""
    if step.get("type") == "Turntable":
        if "speed_us" in step:
            step["speed_us"] = int(step["speed_us"])
        if "duration" in step:
            step["duration"] = float(step["duration"])
        return step
    if "pos" in step:
        step["pos"] = [float(v) for v in step["pos"]]
    for key in ("vel", "acc"):
        if key in step:
            v = step[key]
            step[key] = [float(x) for x in v] if isinstance(v, list) else float(v)
    if "time" in step:
        step["time"] = float(step["time"])
    return step

def _load_stats(name: str) -> dict:
    p = _stats_path(name)
    if p.exists():
        return json.loads(p.read_text())
    return {"total_runs": 0, "success": 0, "fail": 0, "unknown": 0, "history": []}

def _save_stats(name: str, stats: dict):
    _stats_path(name).write_text(json.dumps(stats, indent=2))

def _record_stat(name: str, result: str):
    stats = _load_stats(name)
    stats["total_runs"] += 1
    stats[result] = stats.get(result, 0) + 1
    stats["history"].append({"timestamp": datetime.now().isoformat(timespec="seconds"), "result": result})
    _save_stats(name, stats)

async def _broadcast(msg: str):
    dead = []
    for ws in _ws_clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _ws_clients.remove(ws)

async def _stream_proc(proc: subprocess.Popen):
    loop = asyncio.get_event_loop()
    while True:
        line = await loop.run_in_executor(None, proc.stdout.readline)
        if not line:
            break
        await _broadcast(line.decode(errors="replace"))


# ── routes ─────────────────────────────────────────────────────────────────

app.mount("/ui", StaticFiles(directory=BASE / "ui"), name="ui")

@app.get("/")
async def index():
    return FileResponse(BASE / "ui" / "index.html")

@app.get("/api/plans")
async def list_plans():
    result = []
    for p in sorted(PLANS_DIR.glob("*.json")):
        plan = json.loads(p.read_text())
        stats = _load_stats(plan["name"])
        result.append({**plan, "stats": stats})
    return result

class PlanBody(BaseModel):
    name: str
    steps: list

@app.post("/api/plans")
async def create_plan(body: PlanBody):
    p = _plan_path(body.name)
    if p.exists():
        raise HTTPException(400, "Plan already exists")
    steps = [_coerce_step_floats(s) for s in body.steps]
    data = {"name": body.name, "created_at": datetime.now().isoformat(timespec="seconds"), "steps": steps}
    p.write_text(json.dumps(data, indent=2))
    return data

class ImportBody(BaseModel):
    name: str
    steps: list
    created_at: Optional[str] = None

@app.post("/api/plans/import")
async def import_plan(body: ImportBody):
    data = body.model_dump()
    if not data["created_at"]:
        data["created_at"] = datetime.now().isoformat(timespec="seconds")
    data["steps"] = [_coerce_step_floats(s) for s in data["steps"]]
    _plan_path(data["name"]).write_text(json.dumps(data, indent=2))
    await _broadcast(f"[PLAN_IMPORTED] {data['name']}\n")
    return {"ok": True, "name": data["name"]}

@app.get("/api/plans/{name}")
async def get_plan(name: str):
    p = _plan_path(name)
    if not p.exists():
        raise HTTPException(404, "Not found")
    return json.loads(p.read_text())

class UpdateBody(BaseModel):
    steps: list

@app.put("/api/plans/{name}")
async def update_plan(name: str, body: UpdateBody):
    p = _plan_path(name)
    if not p.exists():
        raise HTTPException(404, "Not found")
    data = json.loads(p.read_text())
    data["steps"] = [_coerce_step_floats(s) for s in body.steps]
    p.write_text(json.dumps(data, indent=2))
    return data

@app.delete("/api/plans/{name}")
async def delete_plan(name: str):
    p = _plan_path(name)
    if not p.exists():
        raise HTTPException(404, "Not found")
    p.unlink()
    sp = _stats_path(name)
    if sp.exists():
        sp.unlink()
    return {"ok": True}

# ── robot control ──────────────────────────────────────────────────────────

class ConnectBody(BaseModel):
    sudo_password: str
    interface: str = "enp2s0"

@app.post("/api/robot/connect")
async def robot_connect(body: ConnectBody):
    global _connected, _rviz_proc, _capture_proc
    pw = body.sudo_password
    iface = body.interface

    async def run_step(cmd: str, label: str):
        await _broadcast(f"\n[STEP] {label}\n")
        proc = await asyncio.create_subprocess_shell(
            cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        async for chunk in proc.stdout:
            await _broadcast(chunk.decode(errors="replace"))
        rc = await proc.wait()
        if rc != 0:
            raise RuntimeError(f"{label} failed (exit {rc})")

    try:
        # Step 0: ensure the ROS workspace is built
        await _broadcast("\n[STEP] Checking robot workspace...\n")
        ws_setup = Path.home() / "ros2_ws" / "install" / "setup.bash"
        if not ws_setup.exists():
            await _broadcast("[STEP] Building robot workspace...\n")
            build = await asyncio.create_subprocess_shell(
                "source /opt/ros/humble/setup.bash && "
                "cd ~/ros2_ws && "
                "colcon build --packages-select lux_dsr_control --symlink-install 2>&1",
                executable="/bin/bash",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            async for chunk in build.stdout:
                await _broadcast(chunk.decode(errors="replace"))
            build_rc = await build.wait()
            if build_rc != 0:
                raise RuntimeError(f"Workspace build failed (exit {build_rc})")
        await _broadcast("[INFO] Workspace ready\n")

        await run_step(
            f"echo '{pw}' | sudo -S ip addr flush dev {iface} && "
            f"echo '{pw}' | sudo -S ip link set {iface} up && "
            f"echo '{pw}' | sudo -S ip addr add 192.168.0.50/24 dev {iface}",
            "Configuring PC IP address"
        )
        await run_step("ping -c 4 192.168.0.20", "Pinging robot at 192.168.0.20")
        # Step 3 runs in background - launch RViz
        await _broadcast("\n[STEP] Launching RViz in real mode...\n")
        _rviz_proc = subprocess.Popen(
            "source /opt/ros/humble/setup.bash && "
            "source ~/ros2_ws/install/setup.bash && "
            "ros2 launch dsr_bringup2 dsr_bringup2_rviz.launch.py "
            "mode:=real host:=192.168.0.20 port:=12345 model:=a0912",
            shell=True, executable="/bin/bash",
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid
        )
        await _broadcast("[INFO] RViz launching in background\n")
        _capture_proc = subprocess.Popen(
            "source /opt/ros/humble/setup.bash && "
            "source ~/ros2_ws/install/setup.bash && "
            "ros2 run lux_dsr_control pose_capture_node",
            shell=True, executable="/bin/bash",
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid
        )
        await _broadcast("[INFO] pose_capture_node launched\n")
        _connected = True
        await _broadcast("[CONNECTED]\n")
        return {"ok": True}
    except RuntimeError as e:
        await _broadcast(f"[ERROR] {e}\n")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

class StartBody(BaseModel):
    plan_name: str

@app.post("/api/robot/start")
async def robot_start(body: StartBody):
    global _active_plan
    if (_active_proc and _active_proc.poll() is None) or _active_plan is not None:
        raise HTTPException(409, "A plan is already running")
    p = _plan_path(body.plan_name)
    if not p.exists():
        raise HTTPException(404, "Plan not found")
    _active_plan = body.plan_name
    asyncio.get_event_loop().create_task(_run_plan_task(body.plan_name, p.resolve()))
    return {"ok": True}

async def _run_plan_task(plan_name: str, plan_path: Path):
    global _active_proc, _active_plan, _build_proc, _stop_requested

    # Check if the package is already built
    await _broadcast("[STEP] Checking lux_dsr_control package...\n")
    check = await asyncio.create_subprocess_shell(
        "source /opt/ros/humble/setup.bash && "
        "source ~/ros2_ws/install/setup.bash && "
        "ros2 pkg list 2>/dev/null | grep -q lux_dsr_control",
        executable="/bin/bash",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    pkg_found = (await check.wait()) == 0

    if not pkg_found:
        await _broadcast("[STEP] Building lux_dsr_control...\n")
        build = await asyncio.create_subprocess_shell(
            "source /opt/ros/humble/setup.bash && "
            "cd ~/ros2_ws && "
            "colcon build --packages-select lux_dsr_control --symlink-install 2>&1",
            executable="/bin/bash",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        _build_proc = build
        async for chunk in build.stdout:
            await _broadcast(chunk.decode(errors="replace"))
        build_rc = await build.wait()
        _build_proc = None

        if build_rc != 0:
            label = "Build cancelled" if build_rc < 0 else f"Build failed (exit {build_rc})"
            await _broadcast(f"[ERROR] {label}\n")
            await _broadcast(f"[DONE] Plan '{plan_name}' aborted — build error\n")
            _active_plan = None
            return

        await _broadcast("[INFO] Build succeeded\n")

    # Split plan into robot/turntable segments
    plan_data = json.loads(plan_path.read_text())
    steps = plan_data.get("steps", [])

    segments: list = []
    robot_buf: list = []
    for step in steps:
        if step.get("type") == "Turntable":
            if robot_buf:
                segments.append(("robot", list(robot_buf)))
                robot_buf = []
            segments.append(("turntable", step))
        else:
            robot_buf.append(step)
    if robot_buf:
        segments.append(("robot", robot_buf))

    t_start = time.monotonic()
    loop = asyncio.get_event_loop()
    last_rc = 0

    for seg_type, seg_data in segments:
        if _stop_requested:
            break

        if seg_type == "robot":
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False, dir=str(PLANS_DIR)
            ) as tf:
                json.dump({
                    "name": plan_data["name"],
                    "created_at": plan_data.get("created_at", ""),
                    "steps": seg_data,
                }, tf)
                tmp_path = tf.name
            try:
                _active_proc = subprocess.Popen(
                    "source /opt/ros/humble/setup.bash && "
                    "source ~/ros2_ws/install/setup.bash && "
                    f"ros2 run lux_dsr_control move_joint_node --plan-file {tmp_path}",
                    shell=True, executable="/bin/bash",
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    preexec_fn=os.setsid,
                )
                await _stream_proc(_active_proc)
                last_rc = await loop.run_in_executor(None, _active_proc.wait)
            finally:
                _active_proc = None
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        else:  # turntable
            direction = seg_data.get("direction", "CW")
            speed_us = int(seg_data.get("speed_us", 500))
            duration = float(seg_data.get("duration", 1.0))

            if _turntable is None or not _turntable.is_open:
                await _broadcast("[WARN] Turntable not connected — skipping step\n")
                continue

            try:
                _turntable.write(b"ENABLE\n")
                await asyncio.sleep(0.05)
                _turntable.write(f"DIR:{direction}\n".encode())
                await asyncio.sleep(0.05)
                _turntable.write(f"SPEED:{speed_us}\n".encode())
                elapsed = 0.0
                while elapsed < duration and not _stop_requested:
                    await asyncio.sleep(0.1)
                    elapsed += 0.1
                _turntable.write(b"DISABLE\n")
            except Exception as e:
                await _broadcast(f"[WARN] Turntable error: {e}\n")

    elapsed_total = time.monotonic() - t_start
    await _broadcast(f"[STAT] Finished in {elapsed_total:.1f}s\n")

    if _stop_requested:
        result = "success"
        _stop_requested = False
    else:
        result = "unknown" if last_rc < 0 else "fail"

    _record_stat(plan_name, result)
    await _broadcast(f"[DONE] Plan '{plan_name}' finished — {result}\n")
    _active_proc = None
    _active_plan = None

@app.post("/api/robot/stop")
async def robot_stop():
    global _active_proc, _active_plan, _stop_requested, _build_proc
    # If still in the build phase, kill the build process
    if _build_proc is not None and _build_proc.returncode is None:
        try:
            _build_proc.kill()
        except ProcessLookupError:
            pass
        return {"ok": True}
    # During turntable step: plan active but no robot process
    if _active_plan is not None and (_active_proc is None or _active_proc.poll() is not None):
        _stop_requested = True
        return {"ok": True}
    if not _active_proc or _active_proc.poll() is not None:
        raise HTTPException(409, "No plan running")
    _stop_requested = True
    try:
        os.killpg(os.getpgid(_active_proc.pid), signal.SIGINT)
    except ProcessLookupError:
        pass
    return {"ok": True}

@app.post("/api/robot/disconnect")
async def robot_disconnect():
    global _rviz_proc, _capture_proc, _connected, _active_proc, _active_plan
    # Stop any running plan first
    if _active_proc and _active_proc.poll() is None:
        try:
            os.killpg(os.getpgid(_active_proc.pid), signal.SIGINT)
        except ProcessLookupError:
            pass

    for proc in filter(None, [_rviz_proc, _capture_proc]):
        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGINT)
            except ProcessLookupError:
                pass
    _rviz_proc = None
    _capture_proc = None
    _captured_points.clear()
    _connected = False
    await _broadcast("[DISCONNECTED]\n")
    return {"ok": True}

@app.get("/api/robot/status")
async def robot_status():
    proc_running = _active_proc is not None and _active_proc.poll() is None
    running = proc_running or _active_plan is not None
    return {"connected": _connected, "running": running, "active_plan": _active_plan}

# ── hand-teach ─────────────────────────────────────────────────────────────

ROS_ENV = (
    "source /opt/ros/humble/setup.bash && "
    "source ~/ros2_ws/install/setup.bash && "
)

async def _ros_call(cmd: str) -> bool:
    proc = await asyncio.create_subprocess_shell(
        ROS_ENV + cmd,
        executable="/bin/bash",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    async for chunk in proc.stdout:
        await _broadcast(chunk.decode(errors="replace"))
    return (await proc.wait()) == 0

SRV_TRIGGER = "std_srvs/srv/Trigger {}"
CAPTURE_NODE = "/pose_capture_client"

@app.post("/api/robot/hand_guide/enable")
async def hand_guide_enable():
    ok = await _ros_call(f"ros2 service call {CAPTURE_NODE}/enable_hand_guide {SRV_TRIGGER}")
    return {"ok": ok}

@app.post("/api/robot/hand_guide/disable")
async def hand_guide_disable():
    ok = await _ros_call(f"ros2 service call {CAPTURE_NODE}/disable_hand_guide {SRV_TRIGGER}")
    return {"ok": ok}

@app.post("/api/robot/hand_guide/record")
async def hand_guide_record():
    # Run silently — capture notification arrives via POST /api/robot/hand_guide/captured
    # Broadcasting ros2 service call stdout causes duplicate [CAPTURE] if node includes
    # the sentinel in its Trigger response message field.
    proc = await asyncio.create_subprocess_shell(
        ROS_ENV + f"ros2 service call {CAPTURE_NODE}/record_point {SRV_TRIGGER}",
        executable="/bin/bash",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    ok = (await proc.wait()) == 0
    return {"ok": ok}

@app.post("/api/robot/hand_guide/clear")
async def hand_guide_clear():
    global _captured_points
    ok = await _ros_call(f"ros2 service call {CAPTURE_NODE}/clear_plan {SRV_TRIGGER}")
    if ok:
        _captured_points.clear()
    return {"ok": ok}

@app.get("/api/robot/hand_guide/points")
async def hand_guide_points():
    return {"points": _captured_points}

@app.delete("/api/robot/hand_guide/points")
async def hand_guide_clear_points():
    global _captured_points
    _captured_points.clear()
    return {"ok": True}

@app.post("/api/robot/hand_guide/captured")
async def hand_guide_captured(request: Request):
    """Called by pose_capture_node after each record_point to push step data."""
    point = await request.json()
    _captured_points.append(point)
    await _broadcast(f"[CAPTURE] {json.dumps(point)}\n")
    return {"ok": True, "count": len(_captured_points)}

@app.post("/api/robot/hand_guide/save")
async def hand_guide_save():
    ok = await _ros_call(f"ros2 service call {CAPTURE_NODE}/save_plan {SRV_TRIGGER}")
    return {"ok": ok}

class HandGuideTypeBody(BaseModel):
    move_type: str

@app.post("/api/robot/hand_guide/type")
async def hand_guide_type(body: HandGuideTypeBody):
    if body.move_type not in ("MoveJ", "MoveL"):
        raise HTTPException(400, "move_type must be MoveJ or MoveL")
    ok = await _ros_call(f"ros2 param set {CAPTURE_NODE} current_type {body.move_type}")
    return {"ok": ok}

# ── turntable control ──────────────────────────────────────────────────────

class TurntableConnectBody(BaseModel):
    port: str = "/dev/ttyACM0"
    baud: int = 9600

@app.post("/api/turntable/connect")
async def turntable_connect(body: TurntableConnectBody):
    global _turntable
    global _tt_pending_port
    if serial is None:
        raise HTTPException(500, "pyserial not installed — run: pip install pyserial")
    _close_turntable()
    try:
        _turntable = serial.Serial(body.port, body.baud, timeout=1)
        asyncio.create_task(_tt_sync_state())
        return {"ok": True}
    except Exception as e:
        _turntable = None
        if isinstance(e, PermissionError) or getattr(e, "errno", None) == 13:
            _tt_pending_port = body.port
            raise HTTPException(403, "Permission denied — enter sudo password or add user to dialout group")
        raise HTTPException(500, str(e))

@app.post("/api/turntable/disconnect")
async def turntable_disconnect():
    _close_turntable()
    return {"ok": True}

@app.get("/api/turntable/status")
async def turntable_status():
    connected = _turntable is not None and _turntable.is_open
    return {
        "connected": connected,
        "enabled": _tt_enabled,
        "direction": _tt_direction,
        "speed": _tt_speed,
        "pending_port": _tt_pending_port,
        "rejected_ports": sorted(_tt_rejected_ports),
    }

class TurntableConfirmBody(BaseModel):
    port: str
    sudo_password: str = ""

@app.post("/api/turntable/confirm")
async def turntable_confirm(body: TurntableConfirmBody):
    global _turntable, _tt_pending_port
    if serial is None:
        raise HTTPException(500, "pyserial not installed")
    if body.port != _tt_pending_port:
        raise HTTPException(400, "Port is not pending confirmation")
    _close_turntable()
    def _is_permission_err(e: Exception) -> bool:
        return isinstance(e, PermissionError) or getattr(e, "errno", None) == 13

    try:
        _turntable = serial.Serial(body.port, 9600, timeout=1)
        _tt_pending_port = None
        asyncio.create_task(_tt_sync_state())
        return {"ok": True}
    except Exception as e:
        _turntable = None
        if not _is_permission_err(e):
            raise HTTPException(500, str(e))
        if not body.sudo_password:
            raise HTTPException(403, "Permission denied — enter sudo password or add user to dialout group")
        result = subprocess.run(
            ["sudo", "-S", "chmod", "a+rw", body.port],
            input=(body.sudo_password + "\n").encode(),
            capture_output=True,
            timeout=10,
        )
        if result.returncode != 0:
            raise HTTPException(500, "chmod failed — wrong sudo password?")
        try:
            _turntable = serial.Serial(body.port, 9600, timeout=1)
            _tt_pending_port = None
            asyncio.create_task(_tt_sync_state())
            return {"ok": True}
        except Exception as e2:
            _turntable = None
            raise HTTPException(500, str(e2))

class TurntableRejectBody(BaseModel):
    port: str

@app.post("/api/turntable/reject")
async def turntable_reject(body: TurntableRejectBody):
    global _tt_pending_port
    _tt_rejected_ports.add(body.port)
    if _tt_pending_port == body.port:
        _tt_pending_port = None
    return {"ok": True}

def _tt_send(cmd: str):
    if _turntable is None or not _turntable.is_open:
        raise HTTPException(409, "Turntable not connected")
    _turntable.write((cmd + "\n").encode())

async def _tt_sync_state():
    await asyncio.sleep(0.3)
    try:
        _tt_send(f"DIR:{_tt_direction}")
        _tt_send(f"SPEED:{_tt_speed}")
    except Exception:
        pass

@app.post("/api/turntable/enable")
async def turntable_enable():
    global _tt_enabled
    _tt_send(f"DIR:{_tt_direction}")
    _tt_send(f"SPEED:{_tt_speed}")
    _tt_send("ENABLE")
    _tt_enabled = True
    return {"ok": True}

@app.post("/api/turntable/disable")
async def turntable_disable():
    global _tt_enabled
    _tt_send("DISABLE")
    _tt_enabled = False
    return {"ok": True}

class TurntableDirectionBody(BaseModel):
    direction: str

@app.post("/api/turntable/direction")
async def turntable_direction(body: TurntableDirectionBody):
    global _tt_direction
    if body.direction not in ("CW", "CCW"):
        raise HTTPException(400, "direction must be CW or CCW")
    _tt_send(f"DIR:{body.direction}")
    _tt_direction = body.direction
    return {"ok": True}

class TurntableSpeedBody(BaseModel):
    delay_us: int

@app.post("/api/turntable/speed")
async def turntable_speed(body: TurntableSpeedBody):
    global _tt_speed
    if body.delay_us < 3:
        raise HTTPException(400, "delay_us must be >= 3")
    _tt_send(f"SPEED:{body.delay_us}")
    _tt_speed = body.delay_us
    return {"ok": True}

# ── WebSocket ──────────────────────────────────────────────────────────────

@app.websocket("/ws/terminal")
async def ws_terminal(ws: WebSocket):
    global _disconnect_task
    await ws.accept()
    # Purge stale connections before adding new one
    dead = []
    for existing in list(_ws_clients):
        try:
            await existing.send_text("")
        except Exception:
            dead.append(existing)
    for d in dead:
        _ws_clients.remove(d)
    _ws_clients.append(ws)
    # Cancel any pending safety-shutdown watchdog — client is back
    if _disconnect_task and not _disconnect_task.done():
        _disconnect_task.cancel()
        _disconnect_task = None
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        if ws in _ws_clients:
            _ws_clients.remove(ws)
        # Start watchdog only when the last client drops
        if not _ws_clients:
            _disconnect_task = asyncio.get_event_loop().create_task(
                _schedule_safety_shutdown()
            )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
