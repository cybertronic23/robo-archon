# M2f.2a：固定资产安装与机械臂基础控制

日期：2026-10-01。M2f.2 的第一个交付切片；不代表抓取及第二平台已完成。

## 目标

Franka Panda 与 SO101 能从固定官方 Git commit 安装，在 MuJoCo 中用显式控制配置运行归位、受限基座摆动（wave/demo）、夹爪开合。无需 API Key。保留通用本体根节点与现有 demo 入口。

## 安装与完整性

robots/assets.lock.json 固定仓库、40 位 commit、子目录和模型入口。安装器 sparse checkout 指定版本，验证入口与 Git LFS 实体文件，保留模型及仓库 LICENSE，临时目录完成后原子发布。不覆盖已有未知/不同版本内容；重复安装校验本地内容 SHA256。指纹用于检测意外修改，不是发布者签名。

制造文件仍只保留链接。下载 mesh 和安装清单在 external/ 下被 Git 忽略。

## 控制配置

robots/profiles/*.json 包含 schema、robot_id、source_revision、显式关节/执行器、home、lower/upper、夹爪执行器及开合映射、可选 keyframe。

- Panda：7 个臂关节与独立 tendon 夹爪执行器，夹爪 ctrl 0..255，两手指各 0..0.04m。
- SO101：5 个臂关节与独立夹爪角度；使用官方 new calibration。归位为臂关节零角，开度演示目标 1.5rad，闭合演示目标 0rad；不是 LeRobot 校准后的 0..100 映射，也不是实际夹持力控制。
- 臂动作单位 rad；夹爪对上层暴露 [0,1]。观测开度来自实际关节位置。
- 连接验证安装 identity/revision/content hash，编译后的关节限位、执行器映射和夹爪端点；失败即拒绝连接。
- 命令执行前拒绝错误维度/关节顺序、NaN、越界和非法夹爪开度。
- reset 恢复 profile 初始姿态；停止持有当前臂与夹爪目标，不把 Panda 夹爪 tendon 索引当关节索引。
- 不沿用简化机械臂的 reach/nod 轨迹；未知能力报错。当前没有自碰撞规划、IK 或抓取控制。

## CLI

--install-robot ID / --doctor-robot ID：独立操作，退出后不进入任务循环。
--robot ID --backend mujoco --policy instruction：根据本体 binding 选模型，校验策略契约，传输 profile 到 worker，并加载对应安全限位。TUI 同样使用 profile 安全门。
--save-frames false：关闭无关离屏渲染；旧 --save-frames 无值调用仍兼容。

可通过 ROBO_ARCHON_PYTHON 指定 worker/doctor 的 Python。目录默认要求仓库运行；自定义本体目录通过 --body-catalog 指定。命名 Franka/SO101 模型不能绕过 --robot 退回通用六关节策略。

## 追溯与成熟度

策略提案 metadata 保存固定模型版本和完整控制配置，随 Episode 持久化。Franka/SO101 的 MuJoCo binding 为 controlled；没有 task_verified 抓取声明。ManiSkill/Isaac Sim/real 以及 Go2/Microduck 保持 planned。

## 验收与结果

- 本地 Git fixture 验证固定 commit（即使 HEAD 已变）、许可证、重复安装校验、篡改拒绝、失败不发布半成品。
- Rust 验证 profile 维度、初始限位、动作轨迹约束、拒绝通用 reach、非有限值和限位维度。
- CPU MuJoCo 分别验证臂关节实际运动与归位、夹爪实际开合、reset/stop、错版本拒绝、非法命令不改变 ctrl。
- 两款机械臂 CLI 挥动+关闭夹爪，均 Completed，生成 Episode。
- GitHub 工作流覆盖 Rust、离线安装器及下载官方固定模型后的 CPU 物理测试。

viewer/TUI 交互、离屏图像与视频未做本机视觉验收。CI 定义已提交，远端运行结果需在推送后确认。

## M2f.2 剩余工作

末端目标与 IK、碰撞/接触、抓取物体与成功条件、演示媒体、第二平台的同任务实现与验证、其他平台依赖版本锁定。真机标定和部署另行验收。

CPU 验收环境：MuJoCo 3.14.0、NumPy 2.4.6，记录于 python/requirements-mujoco-tested.txt。原 requirements-mujoco.txt 保留宽版本入口；其他版本需要重新验证。
