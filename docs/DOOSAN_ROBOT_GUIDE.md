# Doosan A0912 Robot - PC to Robot Control Guide (ROS2 Humble)

- Full manual: https://doosanrobotics.github.io/doosan-robotics-ros-manual/humble/index.html
- Doosan repo: https://github.com/DoosanRobotics/doosan-robot2?tab=readme-ov-file

---

## Step 1: Install ROS2 Humble Desktop

Full guide: https://docs.ros.org/en/humble/Installation.html  
Use Ubuntu Deb Packages method.

**Set locale:**

```bash
locale
sudo apt update && sudo apt install locales
sudo locale-gen en_US en_US.UTF-8
sudo update-locale LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8
export LANG=en_US.UTF-8
locale
```

**Setup sources:**

```bash
sudo apt install software-properties-common
sudo add-apt-repository universe
sudo apt update && sudo apt install curl -y
export ROS_APT_SOURCE_VERSION=$(curl -s https://api.github.com/repos/ros-infrastructure/ros-apt-source/releases/latest | grep -F "tag_name" | awk -F'"' '{print $4}')
curl -L -o /tmp/ros2-apt-source.deb "https://github.com/ros-infrastructure/ros-apt-source/releases/download/${ROS_APT_SOURCE_VERSION}/ros2-apt-source_${ROS_APT_SOURCE_VERSION}.$(. /etc/os-release && echo ${UBUNTU_CODENAME:-${VERSION_CODENAME}})_all.deb"
sudo dpkg -i /tmp/ros2-apt-source.deb
```

**Install ROS2:**

```bash
sudo apt update
sudo apt upgrade
sudo apt install ros-humble-desktop
```

**Source ROS2 environment:**

```bash
source /opt/ros/humble/setup.bash
```

Add to `.bashrc` so it loads automatically in every new terminal:

```bash
echo "source /opt/ros/humble/setup.bash" >> ~/.bashrc
source ~/.bashrc
```

---

## Step 2: Install Docker

```bash
sudo apt install -y docker.io
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
newgrp docker
```

---

## Step 3: Install Required Dependencies

From Doosan official repo:

```bash
sudo apt-get update
sudo apt-get install -y libpoco-dev libyaml-cpp-dev wget \
    ros-humble-control-msgs ros-humble-realtime-tools ros-humble-xacro \
    ros-humble-joint-state-publisher-gui ros-humble-ros2-control \
    ros-humble-ros2-controllers ros-humble-gazebo-msgs ros-humble-moveit-msgs \
    dbus-x11 ros-humble-moveit-configs-utils ros-humble-moveit-ros-move-group \
    ros-humble-gazebo-ros-pkgs ros-humble-ros-gz-sim ros-humble-ign-ros2-control
```

---

## Step 4: Install Gazebo Simulation

```bash
sudo sh -c 'echo "deb http://packages.osrfoundation.org/gazebo/ubuntu-stable `lsb_release -cs` main" > /etc/apt/sources.list.d/gazebo-stable.list'
wget http://packages.osrfoundation.org/gazebo.key -O - | sudo apt-key add -
sudo apt-get update
sudo apt-get install -y libignition-gazebo6-dev ros-humble-gazebo-ros-pkgs ros-humble-ros-gz-sim ros-humble-ros-gz
```

---

## Step 5: Create Workspace and Clone Doosan Repo

```bash
mkdir -p ~/ros2_ws/src
cd ~/ros2_ws/src
git clone -b humble https://github.com/doosan-robotics/doosan-robot2.git
```

---

## Step 6: Install Dependencies

```bash
cd ~/ros2_ws/src
rosdep install -r --from-paths . --ignore-src --rosdistro $ROS_DISTRO -y
```

---

## Step 7: Run Emulator Installation Script

```bash
cd ~/ros2_ws/src/doosan-robot2
chmod +x ./install_emulator.sh
sudo ./install_emulator.sh
```

---

## Step 9: Build the Package

```bash
cd ~/ros2_ws
colcon build
. install/setup.bash
```

Add to `.bashrc` so it persists across terminals:

```bash
echo "source /opt/ros/humble/setup.bash" >> ~/.bashrc
echo "source ~/ros2_ws/install/setup.bash" >> ~/.bashrc
source ~/.bashrc
```

---

## Step 10: Physical Connection (Ethernet)

1. Use a standard RJ45 ethernet cable.
2. Plug one end into PC LAN port (`enp2s0`).
3. Plug the other end into the Doosan Robot port
   - Do NOT use the teach pendant port or USB ports.

> Note: Replace `enp2s0` with actual interface name. Check with: `ip link show`  
> Note: Use `192.168.0.50` because `192.168.0.20` is already taken by the robot.

**Set PC IP:**

```bash
sudo ip addr flush dev enp2s0
sudo ip link set enp2s0 up
sudo ip addr add 192.168.0.50/24 dev enp2s0
```

---

## Step 11: Teach Pendant Setup

1. Power on the DRCF controller box, wait 30-60 seconds for full boot.
2. Check robot status shows **STANDBY** on main screen.
   - If **SAFE OFF**: press Servo On button to activate.
   - If **ALARM/ERROR**: clear alarms first, then press Servo On.
3. Set mode to **AUTO** or **REMOTE** (not MANUAL).

**Default robot network settings:**
| Setting | Value |
|---|---|
| IP Address | 192.168.0.20 |
| Subnet Mask | 255.255.255.0 |
| Port | 12345 |

To check or change: Settings > System > Network on teach pendant.  
Default settings password: `admin`

---

## Step 12: Verify Network Connection

```bash
ping -c 4 192.168.0.20
```

Expected: 4 packets received with low latency.  
If 100% packet loss: check cable, confirm robot is fully booted, confirm LAN1 port is used.

---

## Step 13: Launch Real Robot with RViz2

```bash
source /opt/ros/humble/setup.bash
source ~/ros2_ws/install/setup.bash

ros2 launch dsr_bringup2 dsr_bringup2_rviz.launch.py \
  mode:=real host:=192.168.0.20 port:=12345 model:=a0912
```

---

## Step 14: Control Robot from a New Terminal

After RViz is launched in Step 13, open a **new terminal** and source the built package:

```bash
source /opt/ros/humble/setup.bash
source ~/ros2_ws/install/setup.bash
```

### Option A: Run simple example command

```bash
ros2 run dsr_example single_robot_simple
```

### Option B: Directly move robot by calling service

```bash
ros2 service call /dsr01/motion/move_joint dsr_msgs2/srv/MoveJoint "{
   pos: [0.0, 0.0, 90.0, 0.0, 90.0, 0.0],
   vel: 100.0,
   acc: 100.0,
   time: 2.0,
   mode: 0,
   radius: 0.0,
   blend_type: 0,
   sync_type: 0
}"
```

> **Safety:** Keep hand near E-Stop on teach pendant before executing any command.

---

## Step 15: Run Shipped Package `lux_dsr_control`

Use this section when the package is already in your repo and you only need run commands.

### 0) Clone package into `~/ros2_ws/src`

```bash
cd ~/ros2_ws/src
git clone <YOUR_LUX_DSR_CONTROL_REPO_URL> lux_dsr_control
```

### 1) Build package

```bash
cd ~/ros2_ws
colcon build --packages-select lux_dsr_control --symlink-install
```

### 2) Source environment (every new terminal)

```bash
source /opt/ros/humble/setup.bash
source ~/ros2_ws/install/setup.bash
```

### 3) Launch robot stack (Terminal A)

```bash
ros2 launch dsr_bringup2 dsr_bringup2_rviz.launch.py \
   mode:=real host:=192.168.0.20 port:=12345 model:=a0912
```

### 4) Run node (Terminal B)

```bash
source /opt/ros/humble/setup.bash
source ~/ros2_ws/install/setup.bash
ros2 run lux_dsr_control move_joint_node
```

### 5) If node is not found

Ensure `setup.py` has the entry under `entry_points -> console_scripts`:

```python
entry_points={
      'console_scripts': [
            'move_joint_node = lux_dsr_control.move_joint_node:main',
      ],
},
```

Then rebuild and source again.

### 6) If module import fails

Error example:

- `ModuleNotFoundError: No module named lux_dsr_control.move_joint_node`

Check file exists at:

- `~/ros2_ws/src/lux_dsr_control/lux_dsr_control/move_joint_node.py`

Rebuild and source again.
