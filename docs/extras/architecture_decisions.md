# Architecture Decisions

## 1. Why ROS2

Doosan DSR robots have no standalone Python SDK. Official control options:

| Path | Language | ROS2 needed | Cross-platform |
|---|---|---|---|
| `doosan-robot2` ROS2 packages | Python | Yes | Linux only |
| `API-DRFL` | C++ | No | Windows + Linux |
| `DRL / Dart Studio` | Proprietary | No | Windows only |
| Raw TCP/IP sockets | Any | No | Any (undocumented) |

ROS2 is the only officially supported Python path. `API-DRFL` requires writing Python bindings around a C++ library. Raw TCP/IP requires reverse-engineering an undocumented protocol. Neither is a viable baseline for a new project.

This project also builds on `lux_dsr_control` — an existing ROS2 package. Using ROS2 as the control layer was the correct, supported, and pragmatic decision given what Doosan provides.

---

## 2. Why Ubuntu is required

### Dependency chain

```
Robot control → lux_dsr_control (ROS2 package)
    → ROS2 Humble
        → Ubuntu 22.04
            → Linux host machine
```

ROS2 Humble officially supports Ubuntu 22.04 only. Every constraint traces back to this.

### Why Docker doesn't solve it

Docker packages software. It cannot abstract away hardware interfaces.

On Linux host: container shares the real Linux kernel. Hardware is directly accessible.

On Mac/Windows host: Docker runs inside a VM. The VM sits between the container and the real hardware.

Three things break on non-Linux hosts:

**`--net=host` networking** — robot sits at `192.168.0.20`. `--net=host` on Mac/Windows connects to the VM's virtual network, not the real ethernet port. Robot unreachable.

**`--cap-add=NET_ADMIN`** — needed to configure network interface for robot communication. Kernel capability on the real machine, meaningless inside a VM.

**Serial passthrough** — Arduino turntable on `/dev/ttyACM0`. Clean on Linux, unreliable through Mac/Windows VM layer.

### Why a Python GUI wouldn't change this

A single-file Python GUI (PyQt6/tkinter) hits the same Ubuntu requirement the moment it tries to control the robot:
- Still needs `ros2 run` → still needs ROS2 → still needs Ubuntu
- `rclpy` + Qt event loop conflict is an additional problem on top
- Docker is still required to package ROS2 for client delivery

Ubuntu is not a UI choice — it is a Doosan + ROS2 constraint inherited before any code was written.

---

## 3. Why a web app instead of Python GUI

The feature set requires three capabilities that Python GUI handles poorly:

**Real-time subprocess streaming**
Robot control spawns subprocesses and must stream stdout live. In PyQt6/tkinter this requires worker threads, signal/slot wiring, and manual widget updates. WebSocket + browser terminal handles this natively.

**Concurrent operations**
App must simultaneously manage a robot subprocess, Arduino serial, and live UI. FastAPI asyncio handles all three natively. Qt requires QThread or a fragile `rclpy` + Qt event loop bridge.

**`rclpy` + GUI event loop conflict**
ROS2 Python client (`rclpy`) runs its own executor loop. Qt runs its own. Two loops in one process = you must poll `rclpy.spin_once()` on a QTimer every 100ms or run `rclpy` in a separate thread with Qt signal emission for every update. The web app eliminates this — ROS2 calls happen in FastAPI, browser receives results via WebSocket.

**UI complexity**
Plan editor with step list, modal dialogs with tabs, live turntable control, hand guide capture flow, real-time status badges — this is exactly what browsers and reactive JS frameworks are optimized for.

**Requirements grew iteratively**
Web app scaled with complexity as features were added. Qt with this feature set would have forced rewrites mid-project each time scope expanded.

A simpler UI would have allowed a simpler toolkit. The requirements drove the tool choice, not the reverse.

---

## 4. Jetson deployment — hardware compatibility

### The problem

Jetson Nano maximum JetPack is **4.6** (Ubuntu 18.04). ROS2 Humble requires Ubuntu 22.04. These are incompatible — no workaround exists.

| Jetson | Max JetPack | Ubuntu | ROS2 Humble |
|---|---|---|---|
| Nano | 4.6 | 18.04 | No |
| Xavier NX | 6.x | 22.04 | Yes |
| Orin Nano | 6.x | 22.04 | Yes |
| Orin NX | 6.x | 22.04 | Yes |

JetPack version is tied to hardware. Nano's Tegra chip cannot run JetPack 5/6 — NVIDIA never released it.

### Why recompiling for Foxy won't work

Porting Doosan DSR packages from Humble to Foxy (Ubuntu 20.04 / JetPack 5.x) means:
- ROS2 API differences between Foxy and Humble
- `dsr_msgs2` service definitions may differ
- `dsr_bringup2` launch files use Humble-only syntax
- No official Doosan support — unsupported path

Not practical.

### Solutions ranked

| Option | Cost | Extra work |
|---|---|---|
| Jetson Orin Nano | ~$250 | Rebuild image for ARM64 |
| Mini PC / Intel NUC | ~$150-300 | Zero — same x86 `.tar.gz` |
| Raspberry Pi 5 | ~$80 | Rebuild for ARM64, weaker hardware |

**Mini PC is most practical** — x86, Ubuntu 22.04, same Docker image already built, connects to robot over ethernet. No ARM cross-compile, no new build needed.

**Orin Nano** if Jetson form factor is required. Build image natively on Orin (ARM64) using same Dockerfile — takes ~25 min first time, same process as Ubuntu PC build.
