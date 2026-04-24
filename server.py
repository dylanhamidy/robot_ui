import atexit
import asyncio
import json
import os
import signal
import subprocess
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
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
    yield
    _kill_robot_procs()  # resolved at call time — defined below


app = FastAPI(lifespan=lifespan)

# ── state ──────────────────────────────────────────────────────────────────
_active_proc: Optional[subprocess.Popen] = None
_active_plan: Optional[str] = None
_build_proc = None  # asyncio.subprocess.Process during colcon build
_rviz_proc: Optional[subprocess.Popen] = None
_connected: bool = False
_stop_requested: bool = False
_ws_clients: list[WebSocket] = []
_disconnect_task: Optional[asyncio.Task] = None


# ── helpers ────────────────────────────────────────────────────────────────

def _kill_robot_procs():
    """Kill all robot subprocesses. Synchronous and idempotent — safe to call from atexit."""
    global _active_proc, _rviz_proc
    for proc in filter(None, [_active_proc, _rviz_proc]):
        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGINT)
                proc.wait(timeout=3)
            except (ProcessLookupError, subprocess.TimeoutExpired, OSError):
                pass

atexit.register(_kill_robot_procs)

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
    """Ensure all numeric fields in a step are stored as Python floats."""
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

@app.post("/api/plans/import")
async def import_plan(file: UploadFile):
    raw = await file.read()
    try:
        data = json.loads(raw)
        assert "name" in data and "steps" in data
    except Exception:
        raise HTTPException(400, "Invalid plan JSON")
    p = _plan_path(data["name"])
    if "created_at" not in data:
        data["created_at"] = datetime.now().isoformat(timespec="seconds")
    data["steps"] = [_coerce_step_floats(s) for s in data["steps"]]
    p.write_text(json.dumps(data, indent=2))
    return data

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
    global _connected, _rviz_proc
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
    global _active_proc, _active_plan, _build_proc

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

    # Source ROS env and launch the plan node
    _active_proc = subprocess.Popen(
        "source /opt/ros/humble/setup.bash && "
        "source ~/ros2_ws/install/setup.bash && "
        f"ros2 run lux_dsr_control move_joint_node --plan-file {plan_path}",
        shell=True, executable="/bin/bash",
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        preexec_fn=os.setsid,
    )
    await _watch_proc(_active_proc, plan_name)

async def _watch_proc(proc: subprocess.Popen, plan_name: str):
    global _active_proc, _active_plan, _stop_requested
    loop = asyncio.get_event_loop()
    t_start = time.monotonic()
    await _stream_proc(proc)
    rc = await loop.run_in_executor(None, proc.wait)
    elapsed = time.monotonic() - t_start
    await _broadcast(f"[STAT] Finished in {elapsed:.1f}s\n")
    if _stop_requested:
        result = "success"
        _stop_requested = False
    else:
        result = "unknown" if rc < 0 else "fail" # rc==0 spontaneous exit -> also treat as fail since we expect user to stop with SIGINT
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
    global _rviz_proc, _connected, _active_proc, _active_plan
    # Stop any running plan first
    if _active_proc and _active_proc.poll() is None:
        try:
            os.killpg(os.getpgid(_active_proc.pid), signal.SIGINT)
        except ProcessLookupError:
            pass

    # Kill RViz process gorup
    if _rviz_proc and _rviz_proc.poll() is None:
        try:
            os.killpg(os.getpgid(_rviz_proc.pid), signal.SIGINT)
        except ProcessLookupError:
            pass
    _rviz_proc = None
    _connected = False
    await _broadcast("[DISCONNECTED]\n")
    return {"ok": True}

@app.get("/api/robot/status")
async def robot_status():
    proc_running = _active_proc is not None and _active_proc.poll() is None
    running = proc_running or _active_plan is not None
    return {"connected": _connected, "running": running, "active_plan": _active_plan}

# ── WebSocket ──────────────────────────────────────────────────────────────

@app.websocket("/ws/terminal")
async def ws_terminal(ws: WebSocket):
    global _disconnect_task
    await ws.accept()
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
