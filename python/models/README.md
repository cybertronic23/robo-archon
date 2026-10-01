# MuJoCo 资产说明

Franka/SO101 请优先使用固定版本安装器及 `--robot` 控制入口，见 [Robot Gallery](../../docs/robot-gallery.md)。旧的通用六关节策略不适用这两款模型。

RoboArchon **不绑定** MuJoCo 安装包自带模型。资产分三类，均可由 `--model` 指定。

## 目录约定

```
python/
├── assets/catalog.json          # 登记表（builtin 名 → mjcf 相对路径）
└── models/
    ├── desktop_arm_red_blob.xml # 仓库自带的轻量 demo（可提交 git）
    ├── diff_car.xml
    ├── README.md                # 本文
    └── external/                # 社区大模型（Menagerie 等，默认不入库）
        └── franka_emika_panda/  # 拉取后出现
            └── scene.xml
```

| 类型 | 放哪 | 体积 | 是否入库 |
|------|------|------|----------|
| **仓库 demo** | `python/models/*.xml` | 小 | ✅ 提交 |
| **社区 / 自研** | `python/models/external/<name>/` | 常很大（mesh） | ❌ gitignore |
| **任意本地** | 你电脑任意路径 | — | 不进本仓库 |
| **URL** | 自动缓存到 `~/.robo-archon/assets/cache/` | — | 不进本仓库 |

## 怎么用（三种方式）

### 1）内置名（catalog）

```bash
cargo run -p robo-archon-cli -- --list-models

cargo run -p robo-archon-cli -- \
  --backend mujoco --model builtin:desktop_arm \
  --policy instruction --instruction "挥手" --viewer --step-ms 0
```

### 2）本地路径（最灵活，推荐加自制 / Microduck / 已下载的 Menagerie）

```bash
# 指向 scene.xml 或任意入口 MJCF
cargo run -p robo-archon-cli -- \
  --backend mujoco \
  --model /path/to/my_robot/scene.xml \
  --viewer --step-ms 0

# 或指向目录（自动找 scene.xml / *.xml）
--model /path/to/my_robot/
```

### 3）拉一份著名 Menagerie 机器人到 `external/`

```bash
# 仓库根目录执行
python3 scripts/install_robot_assets.py franka_panda
./scripts/fetch-menagerie-robot.sh unitree_go2
./scripts/fetch-menagerie-robot.sh --list   # 看常用短名

# 拉完后可用 catalog 名或路径
cargo run -p robo-archon-cli -- \
  --backend mujoco --robot franka_panda --policy instruction --instruction "归位" --viewer --step-ms 0

# 等价
--model python/models/external/franka_emika_panda/scene.xml
```

来源：[MuJoCo Menagerie](https://github.com/google-deepmind/mujoco_menagerie)（DeepMind 维护的常用机器人 MJCF 集合）。

## 把自定义资产登记成 builtin（可选）

1. 把模型放到 `python/models/external/my_bot/`（或 `python/models/my_bot.xml` 若很轻）  
2. 编辑 `python/assets/catalog.json`，增加：

```json
"my_bot": {
  "description": "My custom arm (local)",
  "mjcf": "models/external/my_bot/scene.xml",
  "camera": "scene",
  "optional": true
}
```

3. `--model builtin:my_bot`  

`optional: true` 表示未拉取时 `--list-models` 会标成「需 fetch」，解析失败时提示运行拉取脚本。

## 注意

- 需要的是 **MJCF（.xml）**，不是裸 URDF（除非你先转成 MJCF）。  
- 自定义模型仍使用通用执行器发现；Franka/SO101 必须通过 `--robot` 使用经过验证的显式映射。LLM 不会自动适配新机器人。
- 大 mesh **不要** 提交进 git；只提交 catalog 条目与轻量 demo。
