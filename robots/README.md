# 本体资料包

本目录是跨平台机器人资料根入口，不是 MuJoCo 模型下载目录。当前四款机器人均为 planned，未声明模型可运行。制造文件只保留可选链接。

```bash
cargo run -p robo-archon-cli -- --list-robots
cargo run -p robo-archon-cli -- --inspect-robot so101
cargo run -p robo-archon-cli -- --body-catalog robots/catalog.json --list-robots
```

查询校验所有引用与契约，不启动仿真或访问网络。其他工作目录请显式传入 --body-catalog。旧 --list-models 仍展示 MuJoCo 模型文件状态。

设计见 [设计文档](../docs/body-packages-design.md)，验收规则见 [M2f.1 spec](../docs/specs/m2f-1-body-packages.md)。
