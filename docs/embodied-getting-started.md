# Embodied Agent OS — Getting Started

面向使用者的快速入口。完整设计决策见开发者笔记：[`../notes/design/embodied-agent-os-design.md`](../notes/design/embodied-agent-os-design.md)。

## 它是什么

RoboArchon 具身平面是一个 **模型无关** 的 Agent OS 运行时：把 VLA / WAM / World Model / 经典策略等产出的动作提案，经确定性安全层后下发到仿真或真机（ROS2 话题契约一致）。

核心闭环：

```
Observation → Policy → Safety → Chronos → RobotBackend → Episode
```

单机器人只有一个 **Executive（执行权威）**；急停与抢占由 Runtime 仲裁，不由多 LLM 协商控制。

## 本地配置与 Microduck LLM 对话

在仓库根目录复制配置模板（`-n` 保留已有的本地配置）：

```bash
cp -n .env.example .env
```

编辑 `.env`，填写自己的 DeepSeek Key，并保存：

```dotenv
DEEPSEEK_API_KEY=你的真实Key
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-chat
```

CLI 在解析参数前自动加载项目根目录的 `.env`，不需要 `export` 或 `source`。终端已有的同名环境变量优先于 `.env`；如果要改用文件中的值，可先在当前终端执行 `unset DEEPSEEK_API_KEY LLM_BASE_URL LLM_MODEL`。`.env` 和 `.env.*` 本地配置被 Git 忽略，仅 `.env.example` 模板纳入版本控制。模板中不要填写真实凭据。

使用其他 OpenAI 兼容服务时，删除 `DEEPSEEK_API_KEY` 行，配置 `OPENAI_API_KEY`，并修改对应的 `LLM_BASE_URL` 和 `LLM_MODEL`。Python 验收脚本目前不自动读取 `.env`，直接使用脚本时仍需终端环境变量；以上自动加载适用于 `robo-archon` CLI。

安装好 [Microduck 仿真环境与资产](specs/m2g-4-microduck-composition.md) 后，启动持续对话：

```bash
cargo build -p robo-archon-cli
target/debug/robo-archon --robot microduck --backend mujoco \
  --skill-chat --viewer \
  --skill-report tmp-episodes/manual-chat.json
```

在终端中输入“向前走两秒”，按回车，等待动作结束后继续下一条。`/stop` 取消动作，`/reset` 重置仿真，`/quit` 退出。直接执行 `--run-skill` 无需 API Key。

Microduck 默认使用项目的 `.venv-microduck/bin/python`（Python 3.12）。模板中的 `ROBO_ARCHON_PYTHON` 是可选覆盖项，通常保留注释即可；如果终端曾设置为其他环境，请执行 `unset ROBO_ARCHON_PYTHON` 恢复默认解释器。

## 运行仿真 MVP（M0）

```bash
# 完整路点演示（Episode 默认写入 ~/.robo-archon/episodes）
cargo run -p robo-archon-cli -- --task-id demo_waypoints --step-ms 0

# 中途抢占
cargo run -p robo-archon-cli -- --step-ms 5 --auto-stop-ms 200

# 交互式 stop / estop（stdin）
cargo run -p robo-archon-cli -- --stdin-stop --step-ms 10
```

## 视觉门控闭环（M1）

Observation 为多模态容器：`proprio` + `modalities` + `annotations`。M1 填充 `images.primary`（合成 RGB）并由 `ColorBlobPolicy` 依赖 detection 再运动。

```bash
# 合成红块 → 检测 → 抓放原语 → Episode 含 media/images.primary/
cargo run -p robo-archon-cli -- \
  --task-id pick_red_blob_sim \
  --policy color_blob \
  --camera synthetic \
  --step-ms 0

# 无目标：Safety 拒绝空路点，系统不崩溃
cargo run -p robo-archon-cli -- \
  --task-id pick_red_blob_sim \
  --policy color_blob \
  --camera synthetic_blank \
  --step-ms 0
```

常用参数：

| 参数 | 含义 |
|------|------|
| `--backend sim` | 当前仅支持仿真后端；真机接入后切换配置即可 |
| `--policy` | `mock`（M0）或 `color_blob`（M1） |
| `--camera` | `none` / `synthetic` / `synthetic_blank`；`color_blob` 默认 synthetic |
| `--save-frames` | 是否把 RGB 写入 episode bundle 的 `media/`（默认 true） |
| `--rate-hz` | Chronos 插值频率（默认 50） |
| `--step-ms` | 下发间隔；CI / 快速跑可用 `0` |
| `--episode-dir` | Episode / media bundle 输出目录 |

## 外接仿真（M2a MuJoCo 最小完备集）

拿到仓库后，可在 MuJoCo 里：**选资产 → 语言指令 → 驱动本体 → 写 Episode**。

```bash
# 1) Python 依赖（建议 venv）
python3 -m venv .venv-mujoco
.venv-mujoco/bin/pip install -r python/requirements-mujoco.txt

# 2) 查看内置资产
cargo run -p robo-archon-cli -- --list-models

# 3) 内置臂 + 语言指令「挥手」（加 --viewer 可弹 MuJoCo 窗口）
cargo run -p robo-archon-cli -- \
  --backend mujoco \
  --model builtin:desktop_arm \
  --policy instruction \
  --instruction "挥手" \
  --viewer \
  --step-ms 20
```

| 参数 | 说明 |
|------|------|
| `--backend mujoco` | NDJSON 桥接 MuJoCo worker |
| `--model builtin:desktop_arm` | 内置资产；也可本地 `.xml`/目录，或 `https://…xml\|zip`（缓存到 `~/.robo-archon/assets/cache`） |
| `--policy instruction` | 短语→运动原语（挥手/回零/伸出/点头/开合夹爪…） |
| `--instruction` | 中英文指令文本 |
| `--viewer` | 弹出 MuJoCo 交互窗口（macOS 会自动用 `mjpython`；建议 `--step-ms 20`） |
| `--worker PATH` | 覆盖默认 worker 脚本 |
| `ROBO_ARCHON_PYTHON` | 指定 Python 解释器（可选） |

语言策略为**确定性原语路由**（不是完整 VLA）；自由文本需命中已知短语。远程资产需为 **MJCF（.xml）或含 MJCF 的 zip**（公开 Menagerie 等请下载后 `--model` 指向 `scene.xml`）。

### 添加著名 / 自定义资产

完整说明：[`python/models/README.md`](../python/models/README.md)。

```bash
# 查看内置 + 需拉取的社区模型
cargo run -p robo-archon-cli -- --list-models

# 拉取 Menagerie（Franka / Go2 / UR5e / SO-ARM100 …）到 python/models/external/
./scripts/fetch-menagerie-robot.sh --list
./scripts/fetch-menagerie-robot.sh franka

cargo run -p robo-archon-cli -- \
  --backend mujoco --model builtin:franka_panda --viewer --step-ms 0

# 自制 / Microduck：直接指路径，或拷到 external/ 并改 catalog.json
--model /path/to/my_robot/scene.xml
```

设计：`notes/design/m2a-mujoco-complete.md`。

## LLM 自然语言（M2c，DeepSeek 等）

规则策略之外，可用云端 LLM 把自然语言编译成运动原语（仍经 Safety，不直连电机）：

```bash
export DEEPSEEK_API_KEY=sk-...

# A：桌面臂
cargo run -p robo-archon-cli -- \
  --backend mujoco --model builtin:desktop_arm \
  --policy llm --instruction "向右边挥一下手" --viewer --step-ms 0

# B：平面小车
cargo run -p robo-archon-cli -- \
  --backend mujoco --model builtin:diff_car \
  --policy llm --instruction "向前走一点再左转" --viewer --step-ms 0

# 录一段演示 MP4（需 ffmpeg；可不开 viewer）
brew install ffmpeg   # 若尚未安装
cargo run -p robo-archon-cli -- \
  --backend mujoco --model builtin:diff_car \
  --policy instruction --instruction "向前走一点再左转180度" \
  --record-video ./tmp-episodes/car-demo.mp4 --step-ms 0
# 结束后看 stderr 里的 video saved 路径；也可用 --record-video 不带路径（写入 episode 目录 demo.mp4）
```

带 **Viewer 截图** 的逐步说明见示例文档：[`examples/diff-car-mujoco/`](../examples/diff-car-mujoco/)。

无 Key 时可用规则策略：`--policy instruction`（短语表，不调用云端）。录屏也可用 macOS「Cmd+Shift+5」对准 viewer 窗口。

## 多轮 TUI 指挥（M2d）

单次 `--instruction` 跑完即退出。若要**连续下达指令**且保持 MuJoCo 会话/本体状态：

```bash
export ROBO_ARCHON_PYTHON="$(pwd)/.venv-mujoco/bin/python"

cargo run -p robo-archon-cli -- \
  --backend mujoco --model builtin:diff_car \
  --policy instruction \
  --viewer --tui --step-ms 0
```

- 终端进入 ratatui Chat：输入自然语言，`Enter` 发送  
- 快捷命令：`/help` · `/estop` · `/quit`（或 Ctrl+C）  
- Busy 时禁止新指令，但 `/estop` 可即时抢占当前轮（含 `mj_step` 中途）  
- Viewer 与 worker 跨多轮保持；`/quit` 立即关窗退出（无需先关 MuJoCo）  
- 每轮独立 Episode；`--save-frames` 时帧写入该轮 `media/images.primary/`  
- 会话复用同一 backend  
- 可选：启动时加 `--instruction "..."` 作为第一轮自动发送  
- LLM：`--policy llm`（需 `DEEPSEEK_API_KEY`）同样支持 `--tui`

| 变量 / 参数 | 说明 |
|-------------|------|
| `DEEPSEEK_API_KEY` / `--llm-api-key` | API Key |
| `LLM_BASE_URL` / `--llm-base-url` | 默认 `https://api.deepseek.com` |
| `LLM_MODEL` / `--llm-model` | 默认 `deepseek-chat` |

## Headless MuJoCo smoke（本机可选）

开发阶段不做自动 CI；需要时本机手动跑：

```bash
python3 -m venv .venv-mujoco
.venv-mujoco/bin/pip install -r python/requirements-mujoco.txt
export ROBO_ARCHON_PYTHON="$(pwd)/.venv-mujoco/bin/python"
# Linux 无显示器时：
# export MUJOCO_GL=egl
bash scripts/mujoco_smoke.sh
```

## ROS2 话题契约（sim 与 real 共用）

默认命名空间：`/robo_archon/arm`

| 话题 | 用途 |
|------|------|
| `joint_states` | 关节状态观测 |
| `joint_command` | 关节位置指令 |
| `gripper_command` | 夹爪 |
| `estop` | 急停 |
| `camera/image_raw` | 相机（M1 合成相机；真机后续订阅） |
| `user_stop` | 用户停止 |

切换仿真 → 真机：实现同一 `RobotBackend` trait、遵守上述话题名，CLI / Executive / Safety 无需改逻辑。

## 相关 crates

`robo-archon-embodied` · `robo-archon-runtime` · `robo-archon-kinetic` · `robo-archon-policy` · `robo-archon-perception` · `robo-archon-sim` · `robo-archon-sim-bridge` · `robo-archon-ros2` · `robo-archon-cli`
