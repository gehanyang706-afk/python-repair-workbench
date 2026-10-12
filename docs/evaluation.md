# Agent 评测

## 范围与数据来源

`benchmarks/real-bugs-v1/manifest.json` 固定 4 个 boltons 历史 Bug 的修复前和修复后完整 Git SHA、上游链接、允许修改路径及每个文件的 SHA-256。复制的是完整原始模块，保留 BSD 许可证；包初始化为空，测试由本项目独立编写。没有复制整个上游仓库或运行完整上游测试，因此这是**真实 Bug 的模块级评测**，不是完整仓库修复榜单。

| 任务 | 上游修复 | 主要验收范围 |
| --- | --- | --- |
| bits-negative-index | [#450](https://github.com/mahmoud/boltons/pull/450) | 多组负索引、越界、空序列 |
| bits-empty-conversion | [#468](https://github.com/mahmoud/boltons/pull/468) | 空二进制、十六进制、字节转换及往返 |
| named-empty-name | [#462](https://github.com/mahmoud/boltons/pull/462) | 两种工厂、类型名/字段名、自动重命名 |
| jsonl-end-seek | [#465](https://github.com/mahmoud/boltons/pull/465) | 二进制流末尾定位、顺逆序、空输入、非法范围 |

4 题来自同一项目，且两题共享模块，不能当作独立广泛样本。公开修复可能已在模型训练语料中，独立验收只保证运行时不泄露用例，不保证无训练污染。任务说明描述完整业务行为，不提供上游补丁或修复提交。

## 执行流程

`evaluation.main → evaluate → qualify → RepairSession/run_agent → holdout verification → reports`

1. 校验清单、文件哈希、路径和符号链接。固定本地 Docker 镜像 ID；不拉取镜像。
2. 每题建立确定性合成 Git 提交，记录原上游提交与合成提交的区别。Agent 副本只含原始源文件、许可证和公开测试。
3. 先核验公开集与包含独立用例的验收集：旧代码必须产生真实失败，官方修复必须通过，既有通过项不能回归，测试身份一致，容器清理完成。独立用例自身必须至少有一项复现 Bug。无效任务不会调用模型。
4. `validate` 在核验完成后结束。`run` 创建独立修复会话，按显式 `--profile` 使用固定机制；默认 `observe`。Agent 仅能读取公开快照，不能读取 oracle/holdout。
5. 公开测试通过后，从受控候选提取批准文件的内容，在新副本中与原始公开测试、独立测试组合，再开隔离容器验收。独立失败不反馈给 Agent 重试，避免评测集泄露。
6. 每题前后原子保存 JSON，生成 CSV 和 Markdown。既不覆盖旧输出，也不自动重跑中断任务；剩余题可以用 `--case` 在新目录中单独运行。

官方修复只由评测器读取。Agent 的文件工具以公开快照清单为边界；框架虚拟文件系统不会映射评测宿主机目录。Docker 挂载每次测试副本，不挂载整个数据集或凭据。

## 命令

在项目根目录、现有虚拟环境中运行：

```bash
# 费用场景估算，不连接模型或 Docker
.venv/bin/python -m repair_workbench.evaluation estimate

# 任务集核验：4 题 × 两套测试 × 原版/官方修复 = 16 次容器执行
.venv/bin/python -m repair_workbench.evaluation validate --output runtime/eval-validation-new

# 付费对照：执行前确认调用规模；明确记录机制配置
.venv/bin/python -m repair_workbench.evaluation run --profile full --output runtime/eval-full-new --env-file /path/to/model.env

# 单题运行；--case 可以重复指定，未知题号和重复选择会拒绝
.venv/bin/python -m repair_workbench.evaluation run --case bits-negative-index --output runtime/eval-one-new --env-file /path/to/model.env
```

默认每题最多 12 次模型调用、3 次候选、240 秒 Agent 软截止时间、每次测试 30 秒。供应商单次请求超时 30 秒；软截止不等于强制总耗时上限。4 题最多 48 次模型调用。每个 live 批次重新执行资格核验，不依赖未经验证的旧报告。

`--profile` 可取 `v2`、`observe`、`context`、`review`、`full`；功能和可比性边界见 [v3 优化](requirements-context-v3.md)。`--image` 指定已存在镜像；默认开发镜像不是可分发的独立生产镜像。代码、数据、镜像和依赖版本留档支持复现配置与评分；采样和服务端模型更新使模型输出不能保证逐字复现。`qwen-plus` 是可能变化的别名，如需比较固定模型快照应显式指定双方相同版本。当前保留供应商 temperature/thinking 默认行为，不将其宣称为确定性配置。

## 报告与统计

- `results.json`：任务集完整清单及哈希、Python/依赖/工作台代码指纹、镜像 ID、模型名称、资源限制、逐题资格检查、Agent 结果、独立验收结论。
- `results.csv`：逐题状态、结果原因、公开状态、模型调用次数和耗时。
- `report.md`：便于审阅的摘要；原始测试日志、候选 diff、会话事件保留于各题目录。
- 有效运行的成功率 = 独立验收通过题数 / 全部选定题数。普通失败、执行异常、无效任务和未完成题不从分母静默剔除；若供应商账号拒绝调用，整批评测中断且成功率设为空值，不能算 Agent 0/4。资格核验模式没有 Agent 成功率。
- 单列公开通过数、独立通过数、首个候选独立通过数、失败原因分布、模型/业务工具调用、候选次数、补丁总行数、输入输出 token 和总耗时。补丁行数含 diff 头部，不等于语义修改量。
- 首个候选通过率不是随机多采样的 pass@k。每题当前仅一个会话，不计算统计置信度，不以 4 题推广整体能力。
- `usage_complete=false` 表示用量报告未覆盖所有调用或仍有未完成题。不可将缺失 token 当作真实零消耗。异常只记录类型，避免供应商原始异常泄露凭据。
- 强制终止可能留下 running/pending；不会自动续跑、自动重复计费。需人工核对任务事件和供应商账单。

## 费用估算

2026-10-11 查询[阿里云官方 qwen-plus 定价](https://help.aliyun.com/zh/model-studio/qwen-plus)：北京地域、非思考、单次输入不超过 128K 时，每百万输入/输出 token 为 ¥0.8/¥2。不假设缓存优惠或免费额度。

4 题、每题 4–12 次调用、每次输入 8K–32K、输出 1K–4096 token 的规划场景约 ¥0.13–¥1.62。该区间是场景估算，不是费用硬上限；上下文增长、地域、思考模式和模型变化会改变金额。当前程序限制调用次数，未实施基于实际金额的熔断；供应商账单是费用依据。更换模型时 `estimate` 的 qwen-plus 价格不自动适用。

## 已知限制与下一步

当前既支持原文精确匹配的局部编辑，也兼容整文件提交；仍未实现语义补丁最小化、新文件创建、自动依赖安装、故障断点恢复、恶意仓库安全证明或大量多项目数据集。多文件编辑只限应用预先批准的已有 Python 源文件。

已留存原始基线与 v2 真实模型评测。下一次对照须在相同数据集、镜像、模型配置、预算与评分下单独切换 v3 配置，并分别报告原题和跨项目新题。

## 基础模型实测

[2026-10-11 qwen-plus 四题基线](baseline-20261011.md)：0/4 修复成功，37 次调用，输入 286439 / 输出 22083 token。两题无候选、两题候选存在语法或缩进错误；独立验收未触发。保留负结果，后续优化以该记录作为对照，不回写原始成绩。
