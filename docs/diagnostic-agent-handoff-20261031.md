# 诊断与优化 Agent 开发交接：2026-10-31

后续已新增[公开开发集诊断评测执行器](diagnostic-evaluation.md)，其配置冻结、批次执行与最新验证记录见[评测交付清单](diagnostic-evaluation-delivery-20261031.json)。下述367项为上一工程阶段记录，保留历史口径；公开开发集与脚本运行仍不构成独立集或真实模型效果。

评测阶段于2026-11-01完成：410项完整回归通过、0跳过；4题×2配置的8次脚本运行及4组成对报告已逐项审计，真实模型API为0，临时数据库已清理。

后续已完成本地MCP、子任务配额及重复动作保护，最新状态见[MCP交接](diagnostic-agent-mcp.md)、[预算交接](diagnostic-subtask-budgets.md)及[停滞保护](diagnostic-progress.md)。原287项回归与噪声记录保留；后续回归、独立审计及打包结果集中记录在[最新交付清单](diagnostic-mcp-delivery-20261031.json)，没有真实模型API调用。

本轮最终完整回归367项全部通过，0跳过，包含实际MCP、Docker与临时MySQL服务，耗时131.105秒。脚本已知候选的独立复验与档案审计通过；安装后的MCP调用也已验证，临时数据库容器已清理。这些是工程与机制验证，不是367道任务或真实模型成功率。

## 当前交付范围

本轮从 `007ded3` 开始，在原工作台上完成第一阶段诊断闭环，并继续完成默认关闭的 Skills 与只读调查委派。普通 Bug 修复的失败基线、源文件授权、独立重建和原测试判定保留；新增性能任务使用独立验收契约。

新增代码、文档与测试保留在当前本地工作区。当前没有真实模型 API 调用，没有真实 LLM 的修复率、调度收益或泛化结论。

| 能力 | 当前实现 | 证据边界 |
| --- | --- | --- |
| 自主选择诊断动作 | 单 Deep Agent 可选源码导航、CPU、Python分配、无profiler基准、修改或停止 | 实际SDK工具循环与反馈分支已离线验证；真实模型选择质量未测 |
| 假设与证据 | 短假设、来源引用、支持/否定修订、原始观察与模型判断分开 | 引用存在不代表因果解释正确 |
| 性能复验 | 功能保持、固定工作负载、ABBA重复测量、噪声和内存门槛 | 仅注册工作负载范围；不是游戏渲染、物理或任意仓库优化 |
| 策略Skills | 三份固定版本/哈希的只读候选策略，真实渐进加载 | 无自动学习/晋升；未证明策略改善真实结果 |
| 只读委派 | 主Agent按需使用investigator，最多1次默认/2次上限 | 真实框架与脚本模型接线已测；未证明委派收益 |
| 子任务资源 | 子请求、工具、时间配额与主流程预留；局部耗尽返回主流程 | 没有Token/费用硬上限；预留不保证任意任务完成 |
| MCP诊断 | 官方SDK本地stdio接入三个固定工具，绑定任务与原始报告 | AST保持直连，独立评分不依赖MCP；无远程或编辑器接入 |
| 停滞停止 | 当前可见的重复观察先提示，再于模型请求前停止；主子分别计数 | 不计算信息增益；尚无真实模型降本或成功率收益 |
| 证据审计 | 交叉核对引用、事件、候选、原始测量和用量缺失 | 哈希清单不是签名，不能识别全部记录被一致重写 |

详细规格分别见 [第一阶段](diagnostic-agent-phase1.md) 与 [可选扩展](diagnostic-agent-phase2.md)。

## 一次完整调用链

1. `snapshot.create_snapshot` 固定 Git 提交；`runner.run_tests` 产生全部原测试通过、清理完成的基线。
2. `diagnosis.DiagnosticSession` 固定工作负载、可改文件、时间与次数上限；原 `RepairSession` 仍要求断言失败，性能任务不绕过它。
3. `agent.run_agent → diagnostic_agent.build_diagnostic_agent` 组装单主Agent。它可以先查源码、登记假设，再依据证据选择实验，应用没有要求每题都走同一顺序。
4. `diagnostics.run_probe → probe_worker.execute` 只执行登记入口，在无网络、只读挂载、CPU/内存受限的本地容器中生成真实报告；主机不导入目标仓库代码。
5. 工具观察绑定快照和 `evidence_ref`；源码同时返回精确编辑的 `source_ref`。模型只需提交已读原文替换，应用负责版本、位置与权限。
6. `session._submit → performance.verify_performance_candidate` 从原基线独立重建候选，重跑原测试，再执行8次配对测量。模型不能改评分、阈值或测试，也不能自己宣告成功。
7. 失败、无法确定和通过都保留真实回执。文字的 `no_change` 仅是待审核决定，不自动变成验收成功。

原始大报告保存在文件，模型接收摘要和可回查引用；初始上下文做结构化裁剪，文件目录可以分页。相同版本的相同探针可以复用已有成功观察，但清楚标记为复用，不算新实验。

## Deep Agents 的实际使用

### Skills

`DiagnosticSession(use_skills=True)` 或诊断CLI的 `--skills` 才启用。启动仅给策略元数据；需要时通过虚拟 `read_file` 读取正文。三类策略为CPU热点、Python分配、行为保持，均不包含具体案例补丁。

实现不仅向 `create_deep_agent` 传 `skills` 路径，也在 `graph.invoke` 的 `files` 状态中装入 `initial_skill_files()`。真实框架测试确认：只给路径而不装入文件时，框架实际发现的策略列表为空。资产随安装包交付，读取前校验固定内容哈希。

### 只读调查

`use_investigator=True` 或 `--investigator` 才增加真实 `subagents/task`。简单任务可以不委派。调查子Agent只能列文件、读源码、符号/字面搜索、回查已有证据；执行层拒绝编辑、探针、任意文件工具或递归委派。

子Agent通过独立视图保存工具轮次与错误恢复状态，避免覆盖主Agent的待处理工具调用；模型次数、用量统计、工具额度、取消与总截止时间则归主任务共同控制。返回的是带引用的模型提议，不是新的工具事实或独立验收。

## 验证与原始证据

- 四个任务是公开合成开发项目：三个预设资源使用缺陷和一个健康项目；不是未见独立集，也不是网易代码。
- `runtime/diagnostic-workload-qualification-20261031-first/qualification.json` 保留4个项目的原测试与12次真实诊断，均完成并清理。每个项目原测试7项。
- `runtime/diagnostic-acceptance-20261031-first/acceptance.json` 保留首次完整链路：脚本模型使用真实工具返回的引用，提交已知候选，容器性能验收通过。
- 同目录 `audit.json` 为只读交叉审计：4份证据、1个假设的2次修订、1次探索与8次复验探针、10个容器清理记录一致。6次模型包装调用没有供应商用量，审计将总Token保留为 `null`；不能写成6次真实API或0 Token。
- 冻结版本最新记录在 `runtime/diagnostic-acceptance-20261031-frozen/`：链路与审计完成，但性能验收为 `inconclusive/measurement_noise_exceeded`。large基线计时的相对噪声约0.480，超过固定0.20门槛；此结果保留，没有重跑挑选较好成绩或放宽评分。
- 安装包构建与校验使用项目锁定的构建依赖，构建依赖仅放在 `runtime/build-tools-20261031/`，没有改全局环境或运行依赖锁。
- 最终回归为287项，其中271通过、16项MySQL专用测试跳过；安装包哈希与文件指纹见[机器可读交付清单](diagnostic-agent-delivery-20261031.json)。早期回归日志与首次运行均保留，不用较好重复结果覆盖它们。

本轮修复了诊断超期仍可能返回成功、模型上下文可能装入完整大报告、清理未确认后仍可继续、以及框架工具漏计共享额度等问题。工具“提议次数”和“实际获准执行次数”分开统计；SDK在执行前拒绝的并行待办调用不能记成已执行实验。

## 最短离线复核

在项目根目录运行，Docker命令只用已有本地镜像：

```bash
.venv/bin/python -m unittest discover -s tests -v
RUN_DOCKER_TESTS=1 REPAIR_TEST_IMAGE=study-agent-deepagents:0.1 .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/audit_diagnostic_run.py runtime/diagnostic-acceptance-20261031-first/agent --provenance runtime/diagnostic-acceptance-20261031-first/acceptance.json
.venv/bin/python scripts/check_delivery.py
.venv/bin/python -m pip check
```

需要新增完整资格记录时，使用新的、尚不存在的输出目录，避免覆盖首轮证据：

```bash
.venv/bin/python scripts/qualify_diagnostic_probes.py --image study-agent-deepagents:0.1 --output runtime/your-new-qualification-run
```

## 当前限制与下一步

1. 已验证的是应用契约、真实框架接线、隔离执行和已知候选复验。下一项效果工作是冻结模型和配置后，对相同任务/工具/总预算做真实成对比较；不能从当前脚本测试推出Agent优化收益。
2. `diagnose` 真实入口默认关闭。本轮明确只有Codex聊天额度授权，没有API批次授权。未来真实运行前需要另列精确模型/地域、角色、任务、请求与Token、并发、超时、停止及费用边界；当前CLI尚无此入口的持久Token/费用预留总账，不能以次数上限代替完整费用治理。
3. 当前仅适配 `map_exporter.py:export_map` 注册契约。适配陌生工具链要增加受审查工作负载；游戏引擎、场景文件修改、HTTP诊断界面与Worker恢复尚未接入。后续已完成的MCP诊断工具接入见[专项交接](diagnostic-agent-mcp.md)，子任务配额见[预算交接](diagnostic-subtask-budgets.md)。
4. 内存是Python可追踪分配峰值，不是RSS/GPU内存；计时不使用profiler。固定小工作负载与单机噪声门槛不代表通用显著性保证。
5. MySQL专用集成需要独立测试服务。本轮另起临时服务补跑，并修复配置准备脚本的 `Path.open(opener=...)` 兼容错误，验证私密文件权限与不覆盖规则；专项与最终全回归结果分别记录在最新清单，原跳过记录不回写。
6. 经验仍是只读候选策略。跨任务记忆自动晋升、正式独立任务和真实模型对照尚待后续验证，不因增加了Skills或子Agent就默认启用全部机制。

## 2026-11-01后续进展

用户随后授权北京区qwen-max公开缺陷修复实测，30万Token、额外付费0元；已执行38次真实请求、277,163 Token，达到保守预留停止条件。它比较按需读取与失败上下文预取，不包含UGC实验调度/MCP的真实模型增益。3组完整配对中预取组通过1/3、基线3/3且Token更多，不晋升默认。已依据首次轨迹离线修复错误帧上下文遗漏，修复版未再调用模型。详见[实测结果](public-qwen-max-results-20261101.md)；上文“本轮无API调用”保留为前一阶段历史记录。
