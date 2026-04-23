import asyncio
import json
import os
import signal
import subprocess
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

app = FastAPI()

# ── state ──────────────────────────────────────────────────────────────────
_active_proc: Optional[subprocess.Popen] = None
_active_plan: Optional[str] = None
_rviz_proc: Optional[subprocess.Popen] = None
_connected: bool = False
_ws_clients: list[WebSocket] = []


# ── helpers ────────────────────────────────────────────────────────────────

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
    global _active_proc, _active_plan
    if _active_proc and _active_proc.poll() is None:
        raise HTTPException(409, "A plan is already running")
    p = _plan_path(body.plan_name)
    if not p.exists():
        raise HTTPException(404, "Plan not found")
    _active_plan = body.plan_name
    _active_proc = subprocess.Popen(
        ["ros2", "run", "lux_dsr_control", "move_joint_node", "--plan-file", str(p.resolve())],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        preexec_fn=os.setsid
    )
    asyncio.get_event_loop().create_task(_watch_proc(_active_proc, body.plan_name))
    return {"ok": True}

async def _watch_proc(proc: subprocess.Popen, plan_name: str):
    global _active_proc, _active_plan
    loop = asyncio.get_event_loop()
    await _stream_proc(proc)
    rc = await loop.run_in_executor(None, proc.wait)
    result = "success" if rc == 0 else "unknown" if rc < 0 else "fail"
    _record_stat(plan_name, result)
    await _broadcast(f"[DONE] Plan '{plan_name}' finished — {result}\n")
    _active_proc = None
    _active_plan = None

@app.post("/api/robot/stop")
async def robot_stop():
    global _active_proc, _active_plan
    if not _active_proc or _active_proc.poll() is not None:
        raise HTTPException(409, "No plan running")
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
    running = _active_proc is not None and _active_proc.poll() is None
    return {"connected": _connected, "running": running, "active_plan": _active_plan}

# ── WebSocket ──────────────────────────────────────────────────────────────

@app.websocket("/ws/terminal")
async def ws_terminal(ws: WebSocket):
    await ws.accept()
    _ws_clients.append(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        if ws in _ws_clients:
            _ws_clients.remove(ws)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
