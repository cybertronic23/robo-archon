# RoboArchon 品牌标志

<p align="center">
  <img src="roboarchon-logo.png" alt="RoboArchon：机械臂拱门与三角核心" width="640" />
</p>

此图是项目维护者选定的正式品牌 Logo。左右机械臂构成 A 形拱门，橙色三角核心表达 Agent 的协调与决策；品牌口号为 **Physical Agent for a More Real World**。

仓库首页和文档首页使用同一份 [PNG 原图](roboarchon-logo.png)，保留用户提供的图像，不重新生成。原图包含白色背景，深色页面也按完整白底品牌图展示。

## 终端标志

TUI 使用 Unicode 半块字符（`▀`、`▄`），以两个垂直色块表达一个字符单元，比细线 ASCII 更接近原图。保留机械臂拱门、内侧夹爪、圆形关节和实心三角核心；深色终端使用浅灰机械臂与橙色强调。

推荐入口：`cargo run -p robo-archon-cli -- --tui`。

| 终端尺寸 | 标志 |
| --- | --- |
| 至少 104 列、44 行 | 64 × 34 色块，17 行完整标志 |
| 至少 88 列、34 行 | 48 × 26 色块，13 行完整标志 |
| 更小的窗口 | `▲ RoboArchon` 紧凑标识，优先保留操作空间 |

旧版显式模型 TUI 在顶部可用空间中展示标志。终端应使用等宽字体；建议深色背景以获得与预览一致的对比度。

终端轮廓从品牌 PNG 的图标部分（像素矩形 `250,150–1285,685`，不含文字）取样。使用面积重采样，再区分背景、机械臂、橙色强调；两份静态色块矩阵编译进程序，运行时无需图片解析库。共享渲染实现位于 `crates/robo-archon-cli/src/brand.rs`，矩阵位于相邻的 `brand-compact.txt` 和 `brand-large.txt`。

终端标志是适配版本，对外文档继续使用 PNG 原图。
