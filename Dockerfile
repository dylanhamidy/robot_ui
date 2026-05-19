# ── Stage 1: Build ROS2 + Doosan DSR + lux_dsr_control ───────────────────
FROM osrf/ros:humble-desktop AS ros2-deps

RUN rm -f /etc/apt/sources.list.d/ros2-latest.list \
    && apt-get update && apt-get install -y curl gnupg2 \
    && curl -sSL https://raw.githubusercontent.com/ros/rosdistro/master/ros.asc \
       | gpg --dearmor -o /usr/share/keyrings/ros-archive-keyring.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/ros-archive-keyring.gpg] http://packages.ros.org/ros2/ubuntu jammy main" \
       > /etc/apt/sources.list.d/ros2.list \
    && apt-get update && apt-get install -y \
    python3-colcon-common-extensions \
    python3-rosdep \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /ros2_ws/src

RUN git clone --depth 1 --branch humble https://github.com/doosan-robotics/doosan-robot2.git

COPY lux_dsr_control ./lux_dsr_control

WORKDIR /ros2_ws

RUN apt-get update && \
    . /opt/ros/humble/setup.sh && \
    rosdep update && \
    rosdep install --from-paths src --ignore-src -r -y && \
    colcon build --packages-up-to lux_dsr_control dsr_bringup2 && \
    rm -rf build log src /var/lib/apt/lists/*

# ── Stage 2: Compile server.py with Nuitka ───────────────────────────────
FROM python:3.11-slim-bullseye AS compiler

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

RUN rm -f /etc/apt/sources.list.d/ros2-latest.list \
    && apt-get update && apt-get install -y curl gnupg2 \
    && curl -sSL https://raw.githubusercontent.com/ros/rosdistro/master/ros.asc \
       | gpg --dearmor -o /usr/share/keyrings/ros-archive-keyring.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/ros-archive-keyring.gpg] http://packages.ros.org/ros2/ubuntu jammy main" \
       > /etc/apt/sources.list.d/ros2.list \
    && apt-get update && apt-get install -y \
    iproute2 \
    iputils-ping \
    sudo \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

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
