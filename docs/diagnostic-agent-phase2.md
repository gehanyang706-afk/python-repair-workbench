# 诊断 Agent 可选扩展：候选 Skills 与只读调查委派

本阶段在[注册诊断与独立性能验收](diagnostic-agent-phase1.md)之上接入两项独立开关：候选诊断策略按需加载，以及专职只读 `investigator` 委派。两项默认关闭，默认仍为单主 Agent。原修复配置、注册工作负载及独立性能判定不因启用扩展而改变。

当前证据是固定版本 Deep Agents 的真实工具链路接确定性模型替身，包括两项同时开启的产品图测试；没有调用真实模型 API。不能据此宣称 Skills 提高准确率、委派减少 Token，或多 Agent 优于单 Agent。真实入口仍默认关闭，本阶段没有新的 API 费用授权。

## 配置与源码入口

| 功能 | Python 会话参数 | `diagnose` 参数 | 默认 |
| --- | --- | --- | --- |
| 候选策略 | `use_skills` | `--skills` | `False` |
| 调查委派 | `use_investigator` | `--investigator` | `False` |
| 已接纳的委派次数上限 | `max_delegations` | `--max-delegations` | 1，只接受整数1或2 |
| 子模型请求配额 | `investigator_max_model_calls` | `--investigator-max-model-calls` | 3 |
| 子工具接纳配额 | `investigator_max_tools` | `--investigator-max-tools` | 12 |
| 子时间窗口 | `investigator_timeout_seconds` | `--investigator-timeout` | 30秒 |
| 主模型调用预留 | `main_model_reserve` | `--main-model-reserve` | 2 |
| 主工具调用预留 | `main_tool_reserve` | `--main-tool-reserve` | 4 |
| 主时间预留 | `main_time_reserve_seconds` | `--main-time-reserve` | 10秒 |

两项可以分别启用，也可以一起启用。允许委派不要求必须委派；简单任务可以始终由主 Agent 完成。候选提交权、探针执行权和最终验收权没有交给子 Agent。

后续已加入[子任务配额与主流程额度预留](diagnostic-subtask-budgets.md)。子配额从原根预算动态收紧，不增加总调用量；局部耗尽与根级停止分开处理。六个新参数的默认值、兼容校验、审计字段及本地请求进程证据以该交接为准。

- `diagnostic_skills.py`：受信资源、固定哈希清单和 StateBackend 初始文件。
- `diagnostic_framework.py`：主图虚拟 `read_file` 的次数登记与审计。
- `diagnostic_delegation.py`：只读子图、共享资源会话视图、执行边界和委派回执。
- `diagnostic_agent.py`：按开关组装主图的 Skills、子 Agent 和中间件。
- `diagnosis.py` / `agent.py`：保存选项与清单，在图调用时注入应用准备的文件。

以下命令仅展示新参数及默认拒绝行为，不包含真实启用开关，应在加载模型前退出；它不是离线推理命令：

```bash
.venv/bin/python -m repair_workbench diagnose --baseline /absolute/path/to/baseline --output /absolute/path/to/new-run --allow map_exporter.py --task '根据已有证据调查导出热点，证据不足时停止。' --skills --investigator --max-delegations 1 --max-model-calls 12
```

真实调用须另行冻结并批准精确模型、场景、全角色请求/Token/时间与费用边界，不能因为设置了扩展开关或已有密钥而执行。第一阶段所列 Token/费用硬上限及持久 Worker 接入缺口仍然存在；共享次数不等于完整费用治理。

## Skills：先发现元数据，再读取正文

应用维护三个候选策略：

| 名称 | 适用信号 | 范围 |
| --- | --- | --- |
| `cpu-hotspot` | 需要用函数耗时和调用次数区分计算热点 | 避免把高调用次数或累计时间直接当根因 |
| `python-allocation` | 目标或已有观察提示 Python 分配值得调查 | 解释 traced peak 的范围及速度/内存取舍 |
| `behavior-preservation` | 准备修改，或解释语义复验失败 | 检查有依据的顺序、身份、异常和副作用约束 |

资源位于 `repair_workbench/skills/<名称>/SKILL.md`，包版本为 `diagnostic-strategies-v1`，各策略版本为 `0.1.0-candidate`。`trusted_skill_manifest()` 在发布清单前检查固定 SHA-256 和大小，保存 `validation_status=offline_wiring_only`、`generalization_verified=false`。每次调用生成新的 FileData，固定资源时间戳只描述包资产，不证明某次模型已经加载。

StateBackend 没有宿主机文件系统回退。只向 `create_deep_agent(skills=['/skills/'])` 传路径，不能让策略自动存在于 backend。产品调用链是：

```text
读取包内策略字节并核对固定哈希
  → initial_skill_files() 生成 /skills/<名称>/SKILL.md 的 FileData
  → run_agent 在 graph.invoke 输入中传入 files 状态
  → 框架发现各策略的名称、描述、版本和路径元数据
  → 主 Agent 按需选择 read_file
  → 仅该文件正文进入后续工具观察
```

首个模型请求包含策略元数据，不把三份正文全部注入系统提示词。正文虽然由应用准备在会话文件状态中，仍需真实 `read_file` 工具响应才能进入模型可见消息。离线测试同时覆盖“仅传路径、不注入 files 时没有可用策略”和“读取未注入路径不会转而读取主机文件”，不能以配置字段存在代替加载证据。

主图只开放虚拟只读文件工具，文件读取也计入共享应用工具次数并记录 `diagnostic_file_read`。策略 frontmatter 中的 `allowed-tools` 描述建议适用工具，不能作为越权授权；实际能力由产品工具目录和中间件决定。

这些策略由应用维护，当前没有 Agent 写入、注册新策略、跨任务自动学习、自动晋升、经验投票或永久记忆流程。成功候选不会自动改变策略文件或清单。未来若要晋升为已验证经验，仍需新的不同任务与健康反例、来源/反例审查和独立效果验证；当前候选版本不能预称已完成这些步骤。

## Investigator：隔离工具轮状态，共享资源所有权

主 Agent 启用委派后才有 `task`，仅接纳 `subagent_type='investigator'` 和不超过2400字符的具体调查说明。通用子 Agent 继续关闭，不能通过默认或未知子类型获得额外执行路径。已接纳的错误任务也消耗委派次数，不在失败后退还；达到上限时停止，不通过重建对象绕过计数。

子图使用 Deep Agents 的 `mode='isolated'`。它不自动继承父图整段消息或 Skills 正文；仍通过委派说明、当前基线的只读工具和明确回查的登记证据开展调查。子图的独立请求/工具/时间配额只进一步限制根任务已有额度，没有新增权限或额外总预算。

子图只有六个工具：

```text
list_sources
read_source
source_outline
read_symbol
search_source
read_evidence
```

既在模型请求中筛选工具，又在工具执行时拒绝目录外调用。`submit_ref_edits`、`run_probe`、嵌套 `task`、文件写入、`read_file`、待办和 Shell 都不在子图权限内。即使子模型伪造调用名，也不能运行探针、修改源码或继续委派。框架兼容所需的文件中间件骨架不代表相应工具已获授权。

`_ChildSessionView` 将工具调用轮、已处理结果、无效工具指纹、恢复次数、待处理参数错误和最近模型观察保留在子会话视图。它们不会覆盖主 Agent 正在处理的工具轮。每次进入子图重置这些局部状态，子图结束也不会关闭父会话。

模型次数、Token 用量统计、工具次数、候选状态、取消信号和会话期限仍由同一根会话拥有。子图参数恢复也消耗同一模型请求上限。主子各自的有界参数恢复状态不会产生额外请求预算。

每次串行委派前，应用从当前根剩余额度中扣除主流程预留，再按配置收紧子请求、工具和时间上限。根余量不足时返回可恢复的 `delegation_declined`，不进入子图；子配额耗尽返回 `delegation_limited`，主流程可以继续读取、提交和复验。根取消、根截止、全局预算及清理失败仍硬停止，不能伪装成成功或忽略未知用量。实际请求进程路径已离线验证局部截止后的终止与回收；任意第三方阻塞客户端不在该保证内。Token 仍只有共享统计，没有硬配额。

## 子图结果不是验收结论

子图只返回短 JSON，包含：

- `summary`：不超过1200字符的发现。
- `evidence_refs`：1–8个当前会话实际存在的证据引用。
- `source_refs`：0–8个实际发放的源码引用。
- `limitations`：不超过600字符的限制说明。

总返回文本限制为6000字符。应用检查结构、大小和引用存在性，合法结果标记为 `investigator_proposal`；缺失、伪造或过大的结果被替换为简短的 `unverified_invalid_findings` 回执。引用存在只能证明来源可回查，不能证明发现的语义或因果判断正确。

父图只接收经处理的 `task` 工具消息。子图文件、完整消息和工具轮状态不会整体并入父图；主 Agent 应按引用复核并决定是否继续，最终修改仍走原精确编辑与独立验收。

会话 `result.json` 保存 `diagnostic_options`、`skill_manifest`、`delegation_count`、`delegation_history` 和 `investigator_budget_policy`。事件流记录 `delegation_started` / `delegation_finished`，子模型事件带 `agent_role='investigator'`。委派记录包含分配配额、实际接纳请求/工具及根工具增量、前后共享计数、耗时和停止原因，以及返回状态、引用及返回摘要哈希。一次 `task` 不是一次底层模型请求；已接纳但没有 usage 回执的调用仍记为未知。

## 已有验证与尚未证明的效果

真实框架的离线测试已覆盖：元数据发现与正文按需读取、损坏策略拒绝、无 files 注入的空发现、只读工具集合、伪造写入/探针/嵌套委派拒绝、主子参数恢复状态独立、共享请求限额、子配额与主额度预留、局部耗尽后的主流程继续、取消/期限传播、本地请求进程回收、委派次数和无效发现回执。

两项同时开启的产品图检查由确定性模型先读取一个策略，再调用调查子图，依据子图实际发放的证据提出 `insufficient_evidence`。该路径共计6个受包装模型请求，其中子图2次；没有性能探针、候选或真实 API 请求，最终保持 `inconclusive`。这些数字描述一次固定测试路径，不是推理成本或任务成功率测量。

尚未证明：真实模型能否选择恰当策略、何时委派更划算、子图是否改善根因定位、Skills 是否减少回归、总 Token 或延迟是否下降，以及独立任务上的收益。启用机制、正常返回、或生成带引用建议，都不能替代这些评测。

后续比较应固定相同模型、任务、工具、候选数和主子合计请求/Token预算，并冻结新子配额、主预留及传输参数，比较默认单 Agent、仅 Skills、仅按需委派及两者组合。新预留改变了启用委派时的行为，不能直接复用旧指纹或旧批次批准。只有在已有成本授权和独立任务时再运行；不得将额外总预算当作架构收益。当前不将这些候选机制设为默认，也不承诺正向效果。

## 无 API 的验证命令

在已有项目环境中执行，不需要密钥，不安装依赖，不运行真实模型：

```bash
cd /Users/gehanyang/编程/python-repair-workbench
.venv/bin/python -m unittest discover -s tests -p 'test_diagnostic_skills.py' -v
.venv/bin/python -m unittest discover -s tests -p 'test_diagnostic_delegation.py' -v
.venv/bin/python -m unittest discover -s tests -p 'test_diagnostic_subbudgets.py' -v
.venv/bin/python -m unittest discover -s tests -p 'test_diagnostic_extensions.py' -v
.venv/bin/python -m unittest discover -s tests -p 'test_diagnostic_cli.py' -v
```

这些检查使用固定可选 Agent 依赖；未安装时应明确跳过，不能记为框架验证通过。最终完整回归与打包状态另见阶段交接，不在此预填测试总数。策略资源已纳入包数据和交付字节校验，不能只验证源码目录存在而漏掉 wheel 中的实际资产。

诊断 HTTP API、网页入口、持久 Worker 恢复、任意 Shell、自动经验晋升、专职修改/验收子 Agent 和游戏编辑器适配仍不在本阶段范围。MCP 接入见后续交接，本阶段不讨论其实现与验证。陌生仓库仍须先建立受审查的注册适配器；可选策略和只读委派不会自动扩大第一阶段工作负载边界。
