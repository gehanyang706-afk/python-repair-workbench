# Python 代码诊断与修复工作台

稳定发布为 `v1.0.0`；`main` 为 `1.1.0.dev0` 开发版本，包含后续可靠性修复与可选实验机制。面向本机受控 Python 仓库，复现测试失败、构建独立候选修改，并用原始测试验证修复结果，交付标准 Git 补丁与可追踪的执行记录。部署与使用见[本机部署](docs/deployment.md)，v1 历史验收与局限见[交付记录](docs/release-v1.md)。

## 功能状态

本地收尾见[交付报告](docs/delivery-20261016.md)，最短安装与演示见[演示路径](docs/demo.md)。本轮未推送或发布新稳定版。默认保守固定 `v2`，并非四配置比较的获胜者；真实客户端预检因一次无工具的异常文本响应暂停，正式矩阵未启动。项目定位为有限范围、供人工审核的修复工作台。

已实现：固定 Git 提交快照、Docker pytest 执行、结果分类、源文件修改范围控制、候选补丁导出、原始测试重建验证和命令行入口；Deep Agents 单 Agent 诊断、受控工具、候选重试、资源预算及运行记录。

已接入 DashScope 模型客户端。候选修改既可由调用方提供，也可通过 `repair` 命令由模型生成。已提供本机 Web 界面、SQLite 任务队列、SSE 进度、协作式取消和重启后的中断标记；任务断点续跑尚未实现。`repair` 与 `serve` 默认使用 v2 流程，v3 `full` 仅在显式指定 `--profile full` 时启用。

## Agent 评测

已提供固定上游提交的真实 Bug 模块级任务（原四题、后续两道 boltons 题及两道 more-itertools 跨项目题）、官方修复核验、独立验收测试及 JSON/CSV/Markdown 报告。评测不会向 Agent 暴露官方修复或独立验收用例。操作与统计口径见 [评测流程](docs/evaluation.md)。任务集核验通过不等于 Agent 修复成功。

## 修复可靠性

当前已按编辑、读取/搜索等类别及具体工具分别统计 Schema/JSON 拒绝和提议调用数，保留兼容总数及用量缺失计数。全新虚拟环境与固定参考镜像的完整回归 152 项通过。真实预检使用 4 次调用，不构成修复成功率，见[预检结果](docs/evaluation-client-preflight-20261014.md)。

Agent 已支持带文件版本和唯一原文匹配的局部修改、语法预检、重复候选识别及模型结束原因记录。完整文件提交保持兼容，成功仍须经过 Docker 原始测试和评测独立验收。实现与对照方案见 [可靠性优化](docs/reliability-v2.md)；[真实模型对照](docs/evaluation-v2-20261011.md)：原四题独立通过从 0/4 到 1/4，新增两题 2/2；小样本结果不代表普遍有效，语义覆盖和读取效率仍有限。

可选 v3 机制包含需求引用与修改范围审查、源码读取观测和按符号定位。`full` 的[真实模型评测](docs/evaluation-v3-20261012.md)中，原四题独立通过 0/4、跨项目新题 0/2；计划与提交工具的结构化调用成为主要瓶颈，故不作为交付默认。机制与适用边界见[需求约束与上下文优化](docs/requirements-context-v3.md)。

`simple_edit` 是简化编辑接口实验配置：模型只提交源码引用与原文替换，应用负责版本、位置和权限。[两题成对真实模型评测](docs/evaluation-simple-edit-20261012.md)中，两次简化接口运行均提交可执行候选；boltons 空转换题独立漏修，more-itertools validator 题独立通过。本次 v2 对照两题均无候选；小样本不足以证明稳定提升，默认配置仍为 `v2`。契约、计数口径和限制见[简化编辑接口](docs/simple-edit-interface.md)。

2026-10-13 工程批次修复混合工具错误漏计、SDK 超时参数及本地请求取消，提供具体读取反馈。新增 `simple_context` / `simple_review` / `simple_full`，独立控制失败驱动上下文与非阻断 diff 风险；不恢复强制需求表单。148 项离线测试全部通过，包含 Docker 回归；新机制尚未取得真实模型成绩。严格 30 秒调用墙钟限制改变停止行为，须用新基础配置重新对照。见[工程验收](docs/reliability-context-20261013.md)及[评测计划](docs/light-context-evaluation-plan.md)。

## 环境

冻结默认后新增 cachetools 仓库级和 itsdangerous 模块级任务，均已通过参考修复资格核验。获批真实验证中，cachetools 在第 11 次调用触发墙钟超时、没有候选；itsdangerous 按停止条件未运行。见[任务说明](benchmarks/delivery-heldout-v1/README.md)、[资格证据](docs/new-task-qualification-20261016.json)与[执行结果](docs/evaluation-delivery-new-v2-20261016.md)，不合并报告为“0/2”。

需要 Python 3.13、Git、运行中的 Docker，以及本地已存在、包含 Python 和 pytest 及目标项目依赖的镜像。快照和验证核心仅使用 Python 标准库；Agent 和网页依赖由 `requirements-agent.lock` 固定当前版本。运行命令不会自动安装依赖或拉取镜像。

目标仓库必须包含 `tests/`，目前支持自有受控项目。未提交修改不进入快照；所有输出目录必须位于原仓库之外，且不能已存在。生产入口不包含预置演示项目。

## Web 工作台

任务提交、修改范围选择、前后测试对比、补丁预览和日志查看均可在网页完成。启动与数据流见 [本机任务工作台](docs/web-workbench.md)。

## 命令行

在本项目根目录执行。将仓库、输出路径和镜像名替换为实际值：

```bash
python3 -m repair_workbench snapshot --repo /path/to/repository --revision HEAD --output /path/to/run/baseline
python3 -m repair_workbench test --snapshot /path/to/run/baseline --image local-python-test:latest
python3 -m repair_workbench candidate --baseline /path/to/run/baseline --changes /path/to/changes.json --allow src/pricing.py --output /path/to/run/candidate
python3 -m repair_workbench verify --baseline /path/to/run/baseline --candidate /path/to/run/candidate --allow src/pricing.py --output /path/to/run/verification
```

`changes.json` 是相对路径到完整新文件内容的 JSON 对象。`--allow` 可重复指定，来自调用方批准范围；即使批准，也不允许改测试、配置或非 Python 文件。当前仅支持修改已有文件。

标准输出为 JSON；异常写入标准错误。退出码 0 表示操作成功或测试通过，1 表示测试未通过或修复被否决，2 表示执行错误或无法确定。建立失败基线时 `test` 返回 1 是预期结果，不应以 `&&` 将其与后续步骤串接。重复执行需要新建记录目录。

## 交付物

- `manifest.json`：原提交、文件哈希、排除清单及候选关联信息。
- `workspace/`：实际参与测试的独立文件副本。
- `patch.diff`：候选与原始提交之间的标准 Git 补丁，不自动应用到原仓库。
- `reports/pytest.log`、`pytest.xml`、`result.json`：日志、测试身份和结果、镜像 ID、耗时和清理状态。
- `verification.json`：通过、否决或无法确定，以及原因与前后统计。

## 开发与验证

`Dockerfile.tests` 提供纯 Python 夹具的参考镜像，固定基础摘要及 `requirements-test-image.lock`；工作台依赖独立使用 `requirements-agent.lock`。无密钥 CI 已准备，覆盖核心、Docker、工具契约、数据集、打包及新任务资格核验；本地等价检查通过，尚无远端运行结果。不承诺该镜像适配任意目标项目。

```bash
python3 -m unittest discover -s tests -v
RUN_DOCKER_TESTS=1 REPAIR_TEST_IMAGE=local-python-test:latest python3 -m unittest discover -s tests -v
```

Docker 测试默认跳过；未指定 `REPAIR_TEST_IMAGE` 时本地测试使用 `study-agent-deepagents:0.1`，这是现有开发环境镜像，并非项目发布物。已提供本机依赖版本锁定文件，但未提供适用于任意目标项目的通用测试镜像。完整修复集成测试的证据保留在 `runtime/acceptance-*`，运行数据不应提交 Git。故意有缺陷的仓库位于 `tests/fixtures/`，不属于工作台自身测试。

自动修复的安装、配置与运行方式见 [Agent 运行与权限边界](docs/agent-runtime.md)。完整框架测试请使用 `.venv/bin/python` 执行上述测试命令；未安装 Agent 依赖时相应测试会明确跳过。

详细说明见 [架构与验证规则](docs/architecture.md)、[本机部署与演示](docs/deployment.md)、[交付记录](docs/release-v1.md)及 [验证记录](docs/validation.md)。
