# ── Stage 1: Build ROS2 + Doosan DSR + lux_dsr_control ───────────────────
FROM osrf/ros:humble-desktop AS ros2-deps

RUN apt-get update && apt-get install -y \
    python3-colcon-common-extensions \
    python3-rosdep \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /ros2_ws/src

RUN git clone --depth 1 --branch humble https://github.com/doosan-robotics/doosan-robot2.git

COPY lux_dsr_control ./lux_dsr_control

WORKDIR /ros2_ws

RUN . /opt/ros/humble/setup.sh && \
    rosdep update && \
    rosdep install --from-paths src --ignore-src -r -y && \
    colcon build --packages-select lux_dsr_control && \
    rm -rf build log src

# ── Stage 2: Compile server.py with Nuitka ───────────────────────────────
FROM python:3.11-slim AS compiler

RUN apt-get update && apt-get install -y \
    gcc \
    patchelf \
    && rm -rf /var/lib/apt/lists/*

RUN pip install nuitka zstandard

WORKDIR /build

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY server.py .

RUN python -m nuitka \
    --onefile \
    --output-filename=robot_ui \
    --include-package=fastapi \
    --include-package=uvicorn \
    --include-package=pydantic \
    --include-package=serial \
    server.py

# ── Stage 3: Runtime image ────────────────────────────────────────────────
FROM osrf/ros:humble-desktop

COPY --from=ros2-deps /ros2_ws/install /ros2_ws/install
COPY --from=compiler /build/robot_ui /app/robot_ui

WORKDIR /app

COPY ui_dist/ ./ui/

RUN mkdir -p /app/plans /app/stats

ENV ROS2_WS_INSTALL=/ros2_ws/install
ENV ROBOT_UI_SKIP_BUILD=1

EXPOSE 8000

CMD ["/bin/bash", "-c", \
    "source /opt/ros/humble/setup.bash && \
     source /ros2_ws/install/setup.bash && \
     /app/robot_ui"]
