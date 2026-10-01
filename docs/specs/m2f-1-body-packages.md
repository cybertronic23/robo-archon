# M2f.1 本体资料包实现 Spec

## 范围

实现平台无关 schema v1、严格 JSON 解析、引用与策略契约校验、CLI 发现/查询，以及 Franka Panda、SO101、Go2、Microduck 的计划资料包。无模型下载、执行路径切换、硬件连接或新增安全授权。

## Schema v1

根：schema_version=1、bodies。所有对象拒绝未知字段。

BodyPackage：id/name/revision、definition、sources、manufacturing_links、bindings、policies、tasks、devices、validations。

- definition：category、capabilities、可未知的 coordinate_convention。
- source：url、description、可未知的 revision/license。
- binding：planned/preview/controlled/task_verified、可未知的 model_format/model_spec/controller/observation_contract/action_contract、joint_mapping、notes。
- policy：bindings、observation_contract、action_contract、可选 weights、normalization、正有限 rate_hz。
- task：goal、success_condition、平台 binding → scene 引用。
- device：binding、calibration_ref、stop_behavior。
- validation：binding、policy、task、evidence。

字段的 Option 显式表示未知，不能把计划资料填成已验证。制造链接可为空。

## 约束

1. 不支持的 schema 版本、重复/空身份必须失败。
2. 非 planned 绑定必须提供模型引用；controlled/task_verified 还必须有控制器和非空契约。
3. 策略只关联 controlled/task_verified 绑定，观测与动作契约精确一致，频率正且有限。
4. 任务、设备和验证引用必须存在；设备必须注明标定引用与停止行为。
5. task_verified 必须有验证记录；验证必须关联兼容策略、对应平台场景和非空证据。
6. 结构校验不访问网络、不检查模型文件/硬件、不认证验证证据。

## CLI

- --list-robots：校验全目录，输出机器人及绑定成熟度，退出。
- --inspect-robot ID：校验后输出单机器人 JSON，未知 ID 非零退出。
- --body-catalog PATH：可指定工作目录之外的目录文件。
- --list-robots 与 --inspect-robot 互斥。
- 现有 --list-models、--model 和运行方式保持兼容；查询不会建立后端会话。

## 验收

- 提交目录中的四款机器人全部为 planned，sources 有真实上游链接，不声明可运行。
- 缺少制造资料仍能加载。
- 覆盖版本错误、身份重复、未知字段、无证据成熟度、缺失引用、策略契约不匹配。
- 实际 CLI 查询/错误出口成功，原 workspace 测试继续通过。

## 后续不在本次范围

固定上游 revision 的安装器、模型/权重校验、具体限位和传感器参数、控制器运行、仿真场景、真机校准、Episode 资料包版本快照。这些属于 M2f.2/M2f.3，不能用 planned 条目代替。
