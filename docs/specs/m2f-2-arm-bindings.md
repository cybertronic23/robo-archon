# M2f.2：机械臂物理抓放与第二平台验证

日期：2026-10-01。M2f.2a 的固定资产安装和基础控制保留；M2f.2 交付 Franka Panda、SO101 的 MuJoCo 抓放，以及 Franka 的 ManiSkill 同任务绑定。Go2/Microduck 控制属于 M2f.3；Isaac Sim 和真机绑定继续为 planned。

## 资产与运行环境

- `robots/assets.lock.json`：MuJoCo 官方来源、40 位 commit、模型入口、子目录。安装器固定版本、验证 LFS 实体、保留许可证，临时目录完成后原子发布；已有未知或被修改内容拒绝覆盖。
- 安装清单的 SHA256 检测意外内容修改；不是发布者签名。下载 mesh 保留在被忽略的 `python/models/external/`。
- `robots/runtime-assets.lock.json`：ManiSkill 3.0.1 自带 Panda URDF/mesh 的内容指纹，以及 ManiSkill/SAPIEN/Torch/MuJoCo/NumPy 的精确版本。连接时检查版本和指纹。
- `python/requirements-*-tested.txt`：Python 3.11 的验收依赖固定版本。ManiSkill 在 Linux 先安装 Torch CPU wheel，防止默认拉取 CUDA 运行时；详见 Gallery。
- 本体制造文件只保留可选链接，运行不要求 CAD/STL/3MF 制造原件。

## 控制与 IK

`robots/profiles/*.json` 固定 source_revision、关节/执行器顺序、限位、初始姿态和夹爪映射。连接时验证实际模型、安装身份与映射一致性；错误维度、关节顺序、非有限值、越界和非法开度均拒绝。

Franka 为 7 臂关节及独立 tendon 夹爪。SO101 为 new calibration 的 5 臂关节及独立夹爪，物理闭合目标更新为 -0.17rad（在上游限位内）。上层关节单位 rad、夹爪开度 [0,1]；它不是 LeRobot 真机校准或夹持力接口。

新增 `RobotBackend::solve_ik` 与 NDJSON `solve_ik/ik_solution`，旧后端默认明确拒绝。求解基于绑定的 TCP 与雅可比，采用阻尼最小二乘，限制关节范围，最大 400 次迭代。位置误差小于 1mm；工具轴朝 -Z，轴误差小于 0.015。只约束工具方向、保留绕工具轴的自由度，以支持 SO101。目标不可达或非法时，不推进场景或修改实际 qpos/ctrl。

每个任务阶段为 Policy 提案，IK 返回关节目标后仍经过 `SafetyGate → Chronos → Executive → RobotBackend`。执行频率固定 50Hz，物理频率 500Hz。路径为受检的关节插值；不是任意障碍环境的绕障规划器。保持物体时持续保留闭合目标，不把被物体撑开的测量开度当作新的松爪目标。

## 场景、接触与碰撞

`python/robo_archon_sim_workers/pick_place.py` 生成机器人、地面、动态红色方块和绿色托盘。seed 0/1/2 改变方块初始 Y 位置，其他 seed 按模 3 复用这三种布局。Franka 方块边长 4cm/质量 40g，SO101 方块边长 2.4cm/质量 8g；场景按机器人工作空间分别配置。不同机器人不宣称相同物体尺度。

SO101 上游凸包碰撞网格填满了夹爪空腔。任务场景保留外观，将两侧指尖碰撞改为显式盒形 contact pads；这属于任务绑定的物理适配，不能宣称已验证制造模型的真实接触几何。

方块移动完全来自引擎接触、摩擦和重力。运行中不焊接夹爪与方块、不瞬移物体。路径预检在独立 scratch 数据中预测已夹持物体跟随末端的位姿；不会修改执行场景。

接触白名单允许方块与两指/地面/托盘、固定基座与地面、相邻连杆及两指接触。其他深于 0.5mm 的穿透拒绝，包含非相邻自碰撞和手臂/夹爪与地面/托盘碰撞。预检关节采样间隔不大于 0.02rad，MuJoCo 运行中每 2ms 再检查；ManiSkill 运行中每 20ms 检查实际 SAPIEN 接触并检查参考几何。非法轨迹与运行中碰撞使任务停止并保存失败。

## 任务与成功判定

CLI：`--robot ID --backend mujoco|maniskill --demo pick-place --seed N`，无需 API Key；MuJoCo 保留基础 instruction/TUI 控制。

11 个阶段：approach → descend → close → grasp_settle → lift → lift_settle → transfer → place → release → retract → settle。关闭后检查实际双指接触，抬起后检查物体高度；失败不进入后续阶段。

两平台共享 `pick_place.v1` 成功条件，必须全部成立：

1. 曾出现双指抓持接触（ManiSkill 使用 Panda 的双指接触力/方向判定）。
2. 物体峰值高度 > 初始中心高度 + 5cm。
3. 最终 XY 与目标误差 < 2.5cm，Z 误差 < 1.5cm。
4. 测得夹爪开度 > 0.8，物体速度 < 0.05m/s。

命令发送完成不等于任务成功。失败、碰撞、IK 不可达、停止或媒体导出失败均使任务 CLI 非零退出。Episode 聚合所有阶段、提案中的完整 profile、物理反馈、最终评估和结果。

任务级停止锁存覆盖阶段边界，不会因 Executive 重置单轮 token 而恢复下一阶段。超时/故障释放资源锁、停止机器人、关闭工作进程；桥接有握手/响应超时，并使用取消安全的 NDJSON 读取。视频请求无渲染帧或编码失败会报错。

## 第二平台

Franka 的 ManiSkill 绑定实际使用包内 `panda_v2.urdf`、SAPIEN CPU 物理和 `pd_joint_pos`。显式映射 `joint1..7 → panda_joint1..7`；归一化夹爪映射为控制器的物理目标，观测由实际关节读取。

共享锁定 MJCF 仅作为 IK/保守几何参考，执行、物体位置、接触和成功反馈全部来自 SAPIEN；启动比较 URDF 与参考模型 TCP，位置偏差大于 3mm 拒绝连接。不是把 MuJoCo 改名成 ManiSkill。

ManiSkill 3.0.1 在 macOS 会强制启用 renderer 且 `can_render(None)` 返回 true；无渲染模式有局部兼容修复。原生 Vulkan/PhysX 日志送 stderr，NDJSON 独占 stdout。macOS 下本次验收为 CPU headless；ManiSkill 图像输出需要可用 Vulkan 环境。SO101 的第二平台和 Isaac Sim 本次不宣称已接入。

## 验收与证据

- Rust：目录/契约、关节限位、profile、NDJSON annotation 保留，以及 proposal 超时/执行故障的停止、关闭和资源释放。
- 安装器：固定 commit、许可证、重复安装、篡改拒绝和失败原子性。
- MuJoCo 基础控制：两臂实际运动/归位、开合、reset/stop、非法命令和错版本拒绝。
- 完整 CLI 物理矩阵：Franka/MuJoCo、SO101/MuJoCo、Franka/ManiSkill，各 seed 0/1/2。
- 负向验收：不可达/非法目标、地面碰撞拒绝且不修改场景、夹爪 stuck-open 的真实物理抓取失败、两平台停止不恢复下一阶段、成功条件逐项缺失不判成功。
- 高清真实 MuJoCo MP4 与 GIF，检查抓取、抬起和最终放置画面。CI 自动生成视频和 Episode artifacts。

证据：[验证报告](../validation/m2f-2-report.json)、[Gallery 与运行命令](../robot-gallery.md)、[工作流](../../.github/workflows/embodied.yml)。本体中三条验证过的绑定标记 task_verified；成熟度仅覆盖这个任务与固定环境，不是通用安全保证或真机部署授权。
