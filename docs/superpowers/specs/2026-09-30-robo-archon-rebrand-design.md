# RoboArchon 全量品牌重命名设计

**日期：** 2026-09-30  
**状态：** 已实施  

## 目标

将具身主线从 Archon 品牌全面切换为 RoboArchon；早期仓库，允许破坏性变更。

## 命名规则

| 用途 | 形式 | 例 |
|------|------|-----|
| 文案 | RoboArchon | README |
| Crate / 目录 / CLI 包 | `robo-archon-*` | `robo-archon-embodied` |
| Rust/Python 标识符 | `robo_archon_*` | `robo_archon_runtime` |
| 环境变量 | `ROBO_ARCHON_*` | `ROBO_ARCHON_PYTHON`（临时兼容旧 `ARCHON_*`） |
| ROS 命名空间 | `/robo_archon/...` | `/robo_archon/arm` |
| 具身 CLI | `robo-archon-cli`，二进制 `robo-archon` | |

## 范围

**改：** 全部具身 crates、Python workers、scripts、docs/examples、ROS 契约、日志前缀、本地配置目录 `~/.robo-archon`。  
**不改：** `crates/archon-core|llm|tools|cli`（数字平面，后续迁回 archon 仓）。

## 验收

- [x] `cargo test --workspace` 通过  
- [x] 文档/脚本命令使用新包名与二进制名  
- [x] 数字平面目录仍存在且保持 `archon-*` 命名  
EOF
