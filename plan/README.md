# RoboArchon 迭代计划与路线图

本文档记录 RoboArchon 项目的长期规划、版本里程碑和各阶段迭代计划。

> 下方里程碑最初面向 **数字平面** Agent Harness（crate 仍为 `archon-*`）。具身主线优先级与已完成项见根 [README.md](../README.md)；架构见 [ARCHITECTURE.md](../ARCHITECTURE.md)。

## 具身主线 M2f：本体资料包与跨平台机器人接入

- M2f.1：已实现平台无关元数据、关系校验、四款计划资料包与 CLI 查询。
- M2f.2：Franka/SO101 的 MuJoCo 抓放与 Franka 的 ManiSkill 同任务绑定已交付。见 [spec](../docs/specs/m2f-2-arm-bindings.md)。
- M2f.3：Microduck 官方策略仿真接入已完成，并在 M2g 持续扩展；Go2 接入独立留在后续迭代。

M2e 保留原网页 Chat 规划。见 [设计](../docs/body-packages-design.md) 与 [spec](../docs/specs/m2f-1-body-packages.md)。下方版本路线属于历史数字平面。

## 具身主线 M2g：可扩展 Skill 与交互会话

- M2g.1–M2g.4：技能注册、连续控制、自定义 ONNX 包、模型工具调用、组合动作与 Microduck 官方行为的本地验收已完成；真实云调用按运行证据单独验收。
- M2g.5（当前）：TUI 本体 → 平台 → 就绪检查 → 加载 Skills → 持续对话，CLI/TUI 共用会话引擎，补齐模型调用证据。见 [设计与验收规格](../docs/specs/m2g-5-tui-skill-session.md)。
- M2g.6（下一迭代）：本地包管理、会话配置保存与第二种 Runner 的扩展接口；Skill/MCP 市场先完成统一发现设计，远程市场与 MCP 执行适配分开验收。见 [迭代计划](m2g-6-local-packages-and-market.md)。

## 版本策略

RoboArchon 使用 [语义化版本](https://semver.org/lang/zh-CN/) (SemVer)：
- **主版本号 (X.y.z)**：不兼容的 API 更改
- **次版本号 (x.Y.z)**：向后兼容的功能添加
- **修订号 (x.y.Z)**：向后兼容的问题修复

## 成熟度评估

当前阶段：**Beta / 早期生产可用**（约 60-70% 成熟度）

与业界成熟的 Agent Harness（如 Claude Code、Cursor Composer）相比，主要差距在于：
- 生态系统集成（MCP）
- 长期记忆（向量存储）
- 高级开发工具（LSP）

## 迭代阶段

| 阶段 | 目标 | 预计时间 | 关键交付物 |
|------|------|----------|-----------|
| [Phase 1: 生产就绪](./phase-1-production-ready.md) | 达到生产环境可用 | 1-2 个月 | MCP 支持、向量存储、增强 Git 集成 |
| [Phase 2: 高级功能](./phase-2-advanced-features.md) | 提升开发体验 | 2-4 个月 | 多 Agent 协作、任务工作流、LSP 集成 |
| [Phase 3: 生态完善](./phase-3-ecosystem.md) | 建立完整生态 | 4-6 个月 | 可观测性、插件系统、TUI/Web 界面 |

## 版本里程碑

| 版本 | 目标日期 | 主要特性 | 状态 |
|------|----------|----------|------|
| [v0.1.0](./milestone-v0.1.0.md) | 2026-04-08 | 基础功能、核心工具、Bug 修复 | ✅ 已发布 |
| [v0.2.0](./milestone-v0.2.0.md) | 2026-05-08 | MCP 客户端支持、向量存储基础 | 🚧 计划中 |
| [v0.3.0](./milestone-v0.3.0.md) | 2026-06-08 | 任务工作流、多 Agent 协作 | 📋 规划中 |
| [v0.4.0](./milestone-v0.4.0.md) | 2026-07-08 | LSP 集成、高级开发工具 | 📋 规划中 |
| [v1.0.0](./milestone-v1.0.0.md) | 2026-09-08 | 生产就绪、完整生态 | 🎯 目标 |

## 优先级指南

### 🔴 高优先级（阻碍发布）
- MCP 客户端支持
- 向量存储集成
- 安全性加固

### 🟡 中优先级（提升体验）
- 任务工作流系统
- LSP 集成
- 多 Agent 协作

### 🟢 低优先级（锦上添花）
- 插件系统
- TUI/Web 界面
- 可观测性仪表板

## 参与贡献

欢迎参与 RoboArchon 的开发！请查看：
- [GitHub Issues](https://github.com/your-repo/robo_archon/issues) - 任务跟踪
- [开发指南](../docs/development.md) - 本地开发设置
- [代码规范](../docs/code-style.md) - 编码规范

---

*最后更新: 2026-04-08*
