# 本体资料包

本目录是跨平台机器人资料根入口，不是 MuJoCo 模型下载目录。Franka/SO101 的 MuJoCo binding 已为 controlled（归位、摆动和夹爪）；其他 binding 与 Go2/Microduck 保持 planned。制造文件只保留可选链接。

```bash
cargo run -p robo-archon-cli -- --list-robots
cargo run -p robo-archon-cli -- --inspect-robot so101
cargo run -p robo-archon-cli -- --body-catalog robots/catalog.json --list-robots
```

查询校验所有引用与契约，不启动仿真或访问网络。其他工作目录请显式传入 --body-catalog。旧 --list-models 仍展示 MuJoCo 模型文件状态。

设计见 [设计文档](../docs/body-packages-design.md)，验收规则见 [M2f.1 spec](../docs/specs/m2f-1-body-packages.md)。

安装与运行见 [Robot Gallery](../docs/robot-gallery.md)，M2f.2 当前进展见 [spec](../docs/specs/m2f-2-arm-bindings.md)。
