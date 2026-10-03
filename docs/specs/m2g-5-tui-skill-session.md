# M2g.5：TUI 本体、平台与技能会话

状态：已实现，本地 Rust / PTY / MuJoCo 与原生 viewer 验收通过；真实云 API 单独验收。

## 产品流程

`robo-archon --tui` 打开本地会话选择器：本体资料包 → 仿真平台 → 就绪检查 → Skills 多选 → 对话。选择器读取 BodyCatalog 和 SkillRegistry，展示未适配本体/平台的原因；本阶段实际会话 Runner 支持 Microduck/MuJoCo。已有显式模型的旧 TUI 保留。

上下键选择、Enter 确认、Space 勾选技能、Esc 返回/退出。就绪检查不启动物理动作；进入聊天才启动一次仿真 Worker，之后保持同一会话。Skills 默认选中就绪项，用户可以取消；至少选择一个。组合技能仅在其所有展开叶子也已选中时可加载。加载不执行动作。

MuJoCo 窗口默认开启，平台选择页按 `v` 切换。新 Skill 会话使用 `--tui`；已有显式模型/策略的旧 TUI 保留。默认详细报告为 `tmp-episodes/microduck-tui-chat.json`，Worker 日志为 `tmp-episodes/microduck-tui-worker.log`。

## 分层设计

- TUI 只负责选择与交互，CLI 行输入与 TUI 通过相同的会话命令通道驱动 Skill 对话引擎。
- 就绪能力列表是模型与主机校验共同使用的白名单；未加载技能即使被组合引用也不能执行。
- 模型调用、计划验证、Executive 执行、停车、报告保存由同一引擎负责，不复制运动逻辑。
- 输入控制在 UI 线程立即触发取消；中断代次丢弃旧指令。取消覆盖规划等待，防止模型返回后继续运动。
- `/stop` 取消、`/reset` 显式重置仿真、`/quit` 和 Ctrl-C 停车退出。忙碌时不积压普通动作；UI 保持响应。终端在正常退出和错误时恢复。
- Worker 诊断写入本地日志，防止破坏 TUI；详细 JSON 留在报告，界面显示模型、工具参数、状态和反馈。

## 模型可观测性

显示有效请求服务的域名、配置模型、规划/反馈阶段及响应 ID、返回模型与 token usage（服务返回时）。报告保存同样元数据和原始工具调用，不记录 Authorization、Key、完整含凭据 URL。元数据来自实际 HTTP 响应；模型字段只表示服务返回声明，不能独立证明供应商身份或账单归属。拒绝和请求失败也可追踪。

## 验收

1. 真终端/PTY 操作完整选择流程，未适配选项无法进入会话，缺环境显示具体原因。
2. 模拟 HTTP 服务验证模型仅看到加载技能，组合依赖缺失被拒；未经授权技能不能产生物理动作。
3. 实际 MuJoCo 多轮动作保持同一 episode；规划与反馈记录响应标识/usage，且日志不包含测试密钥。
4. Busy 时 stop/quit 可抢占；规划期间取消后不开始动作；显式 reset 增加 episode；Ctrl-C 恢复终端。
5. 旧 CLI 持续聊天及固定 Skill 验收回归；真实云 API 另行标注，不把本地 mock 当作 DeepSeek 验收。

## 后续 M2g.6

本地包管理与会话配置：安装/卸载/更新入口、来源与版本展示、常用选择保存、第二种 Runner 适配接口；设计 Skill/MCP 市场统一发现界面，但运动 Skill 与 MCP 外部工具保留不同执行约束。远程市场、MCP 传输、Go2 和其他仿真平台不计入 M2g.5 验收。

## 验收记录与复现

- `cargo test --workspace`：60 项测试通过。
- `.venv-microduck/bin/python scripts/verify_microduck_tui.py --viewer`：真实 PTY、实际 MuJoCo、原生 viewer；仅使用本地 mock 模型 API。覆盖未适配平台、只加载行走技能、模型越过白名单被拒、HTTP 错误后继续对话、取消规划不运动、重置后再次行走、运动中 Ctrl-C 停车退出和终端恢复。
- `.venv-microduck/bin/python scripts/verify_microduck_composition.py --mock-only --report tmp-episodes/m2g5-cli-regression.json`：原 CLI 有序组合、持续对话、旧指令丢弃和 SIGINT 回归通过。
- [验收摘要](../validation/m2g-5-tui.json) 保存无密钥响应证据。服务端返回的响应模型与请求模型分开记录，规划和反馈各有独立响应 ID。`usage` 缺失时记为 null，不推算用量。
- 原生 viewer 已启动并通过退出验收；PTY 不等同于 iTerm2 人工视觉评审。远程市场、第二种 Skill 会话 Runner、真机与真实 DeepSeek 账单尚未验收。
