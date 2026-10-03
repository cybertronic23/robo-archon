# RoboArchon 品牌标志

<p align="center">
  <img src="roboarchon-logo.png" alt="RoboArchon：机械臂拱门与三角核心" width="640" />
</p>

此图是项目维护者选定的正式品牌 Logo。左右机械臂构成 A 形拱门，橙色三角核心表达 Agent 的协调与决策；品牌口号为 **Physical Agent for a More Real World**。

仓库首页和文档首页使用同一份 [PNG 原图](roboarchon-logo.png)，保留用户提供的图像，不重新生成。原图包含白色背景，深色页面也按完整白底品牌图展示。

## 终端标志

TUI 使用纯 ASCII 的简化版本，保留双机械臂、关节与三角核心，终端橙色对应原图的强调色：

```text
         ____        ____
        /   /        \   \
       /   /    /\    \   \    RoboArchon
      (o)==\   /__\   /==(o)
      / /  \_      _/  \ \    PHYSICAL AGENT
     (O)                (O)    FOR A MORE REAL WORLD
    _/ \_              _/ \_
```

推荐入口：`cargo run -p robo-archon-cli -- --tui`。窗口至少 72 列、30 行时显示完整标志，较小窗口显示紧凑版 `/\ RoboArchon`。旧版显式模型 TUI 在顶部可用空间中展示标志，底部会话标题保留紧凑品牌标识。

ASCII 是终端适配图；对外文档使用 PNG。共享渲染实现位于 `crates/robo-archon-cli/src/brand.rs`。
