# 机器人 Gallery：Franka Panda 与 SO101

当前支持 MuJoCo 下的归位、受限基座摆动和夹爪开合。尚无抓取任务、IK 或真机支持。Go2/Microduck 和其他平台仍在计划中。

## 准备与安装

在仓库根目录执行：

```bash
python3 -m venv .venv-mujoco
.venv-mujoco/bin/pip install -r python/requirements-mujoco-tested.txt
export ROBO_ARCHON_PYTHON="$PWD/.venv-mujoco/bin/python"
cargo build -p robo-archon-cli

# 固定官方版本，只获取对应机器人目录
./target/debug/robo-archon --install-robot franka_panda
./target/debug/robo-archon --install-robot so101
./target/debug/robo-archon --doctor-robot franka_panda
./target/debug/robo-archon --doctor-robot so101
```

也可直接运行 python3 scripts/install_robot_assets.py ID。安装器不执行上游脚本；已有未知/被改动目录会报错，请先保留你的修改后再处理该目录。

## 玩起来

```bash
# 加 viewer 可看运动；单轮结束窗口保留到你关闭
./target/debug/robo-archon --robot franka_panda \
  --backend mujoco --policy instruction --instruction "挥手然后关闭夹爪" --viewer

./target/debug/robo-archon --robot so101 \
  --backend mujoco --policy instruction --instruction "打开夹爪" --viewer

# 多轮 TUI；/estop 停止，/quit 退出
./target/debug/robo-archon --robot franka_panda \
  --backend mujoco --policy instruction --viewer --tui

# CPU 无窗口验证，关闭离屏图片采集
./target/debug/robo-archon --robot so101 \
  --backend mujoco --policy instruction --instruction "归位" --save-frames false
```

支持 home/归位、wave/挥手、demo、open_gripper/打开夹爪、close_gripper/关闭夹爪及顺序组合。wave 当前是有限幅度的基座关节摆动演示，不是末端手势规划。当前 --robot 必须选择 mujoco + instruction，暂不接 LLM 策略。

Franka 使用真实 7 臂关节+独立夹爪；SO101 使用 new calibration 的 5 臂关节+夹爪。机器人限位从对应已编译模型记录，worker 验证实际模型一致性。夹爪 [0,1] 是本绑定规范，不能直接作为真机 LeRobot 标定值。

## 状态与验证

--list-robots 显示绑定维护状态，不表示本机已安装；--list-models 查看模型文件存在状态；--doctor-robot 检查版本、内容指纹和控制映射，不测试图形环境。

```bash
cargo test --workspace --offline
python3 -m unittest discover -s scripts/tests -p test_robot_assets.py
.venv-mujoco/bin/python -m unittest discover -s scripts/tests -p test_arm_bindings.py
```

模型由官方固定版本下载，mesh 不提交本仓库。源版本及控制 profile 会记录在动作提案的 Episode metadata 中。详见 [M2f.2a spec](specs/m2f-2-arm-bindings.md)。
