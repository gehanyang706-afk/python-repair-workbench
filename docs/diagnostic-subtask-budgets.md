# 诊断子任务配额与主流程额度预留

只读 `investigator` 现在同时受根任务预算、单次委派配额和主流程预留约束。子任务不能获得额外的模型请求、工具或时间额度。局部额度不足时返回可恢复回执，让主 Agent 保留继续读取、提交和复验的机会；根取消、根截止及根预算耗尽仍保留原停止语义。

这是已实现的执行与停机机制。当前证据来自真实 Deep Agents 工具循环接确定性模型、本地请求进程及模拟验收器，没有真实模型 API 结果，也没有 Token 或费用硬上限。预留资源不保证所有主任务都能完成；具体任务所需调用数仍须预估。

## 接口与默认值

`DiagnosticSession` 接受以下参数，`diagnose` 已接入对应六个 CLI 选项：

| 会话参数 | CLI 选项 | 默认 | 含义 |
| --- | --- | ---: | --- |
| `investigator_max_model_calls` | `--investigator-max-model-calls` | 3 | 单次委派最多接纳的子模型调用，含参数恢复与框架模型调用 |
| `investigator_max_tools` | `--investigator-max-tools` | 12 | 单次委派最多接纳的子工具执行尝试，错误参数也消耗局部次数 |
| `investigator_timeout_seconds` | `--investigator-timeout` | 30 | 单次子任务时间窗口，仍受根剩余时间进一步收紧 |
| `main_model_reserve` | `--main-model-reserve` | 2 | 子任务不得使用的根剩余模型调用额度 |
| `main_tool_reserve` | `--main-tool-reserve` | 4 | 子任务不得使用的根剩余工具调用额度 |
| `main_time_reserve_seconds` | `--main-time-reserve` | 10 | 分配子时间窗口时为主流程保留的根剩余秒数 |

主任务原有的 `max_model_calls`、`deadline_seconds`、工具总上限60次及候选上限没有增加。CLI 的任务总截止默认300秒，直接创建 `DiagnosticSession` 时沿用根会话默认180秒；六个新增选项的默认值相同。

次数参数拒绝布尔值、数字字符串和范围外值；时间参数必须为有限正数。开启 investigator 时，主模型调用预留不能超过任务总调用上限，主时间预留不能超过总截止。子配额可以高于当前实际剩余量，分配时会向下收紧。关闭 investigator 时，新默认预留不会阻止旧的单 Agent 小预算配置，例如1次调用、1秒总截止。

新增预算参数只影响已经开启的 `--investigator`，不会自动开启子 Agent。以下命令只展示参数；没有真实启用开关，应在模型加载前被默认关闭策略拒绝：

```bash
.venv/bin/python -m repair_workbench diagnose --baseline /absolute/path/to/baseline --output /absolute/path/to/new-run --allow map_exporter.py --task '调查导出热点，保留原有行为。' --investigator --max-model-calls 12 --deadline 300 --investigator-max-model-calls 3 --investigator-max-tools 12 --investigator-timeout 30 --main-model-reserve 2 --main-tool-reserve 4 --main-time-reserve 10
```

示例不是离线推理入口，也不是实际 API 批次授权。

## 分配与执行

主 Agent 发出 `task` 后，应用先按原规则计入根工具次数及委派次数，再在 `reserve_investigator_budget(record)` 中计算：

```text
可用子模型次数 = 根模型上限 - 根已接纳次数 - 主模型预留
可用子工具次数 = 60 - 根已接纳工具次数 - 主工具预留
可用子时间 = 根剩余时间 - 主时间预留

本次子配额 = min(配置的子上限, 当前可用子额度)
```

任一可用额度不大于零时，不进入子图，返回 `delegation_declined` 和对应预留原因。已接纳的委派名额不退还；该拒绝不关闭主会话。已经真正耗尽的根预算仍按根异常处理。

已分配的预算通过 `ContextVar` 绑定当前同步委派，不挂到主 Agent 的工具轮状态。`_ChildSessionView` 的模型预留、子工具中间件及受控请求客户端使用同一局部预算：

- 每次进入子模型请求前检查根预算、子请求上限及主模型预留。
- 每次子工具前检查局部接纳上限及主工具预留，执行后再次检查取消与时间。
- 模型调用前后均检查局部时间；最后一次获准请求正常返回有效结论可以结束，不为已经用满次数而丢弃该结论。下一次请求才会因次数不足被拒绝。
- 受控模型请求客户端读取子视图的 `started`、`deadline_seconds`，并轮询其 `check_budget()`。这些读取投影为子期限，不改写根会话的开始时间或总截止。

局部 `ChildQuotaExceeded` 继承根预算异常类型，但由 `task` 调度边界先行处理，转换为 `delegation_limited`。该回执标记 `main_can_continue=true`，仍是未完成调查，不代表优化通过。调度边界恢复主上下文，主 Agent 可自行继续源码读取、精确编辑、提交和独立复验。

根取消、根截止、全局请求或工具上限、请求进程清理失败等异常不转成局部成功。若局部时间与根取消同时发生，根取消优先。委派总次数仍由 `max_delegations` 限制；达到该上限后再次要求委派仍沿用 `delegation_limit` 停止行为。

## 请求进程、用量与可追踪记录

每次委派在 `delegation_history` 及 `delegation_finished` 中保留：

- 本次实际分配的请求、工具、时间上限及分配后主预留边界。
- `model_calls_used`：本次已接纳的模型请求尝试，失败或没有回执的尝试不退回。
- `tools_admitted`：局部工具接纳次数；`tool_calls_used`：这些操作实际增加的根工具次数。参数验证失败可能只消耗前者。
- 根模型/工具前后计数、`delegation_elapsed_seconds` 委派耗时、结束状态、局部或根停止原因，以及返回引用和摘要哈希。

停滞保护另记录父模型轮次与具体 `task` 调用ID哈希，用来精确关联同一轮的多个委派；资源消耗前后计数继续保留，两种标识不互相替代。

事件流的 `elapsed_seconds` 始终表示根任务开始后的时间，单次委派耗时使用独立字段，避免覆盖事件时钟。真实子图回归同时检查根事件时间单调、两种时间均保留、完整审计通过，以及人为回退根时间仍被原审计规则拒绝。

`result.json` 另保存 `investigator_budget_policy`，明确 `token_hard_limit=false`。主子 Token 用量仍汇入同一个根统计；统计不是 Token 配额或费用预留。没有有效 usage 的已接纳请求保留在 `usage_missing_calls`，不按零消耗处理。

本地受控请求验证使用一个只读取测试输入、等待并尝试延迟写文件的 Python 子进程。子任务0.2秒期限触发后，既有请求执行器终止并回收该进程；测试确认进程不存在、延迟文件未生成，并保留 `model_worker_finished.cleanup_ok=true`。主 Agent 随后在原总预算内继续动态读取源码引用、提交候选，并收到模拟验收回执。该过程没有联网或模型 API 请求。

这个证据证明当前受控请求进程路径的停止与清理机制，不证明任意第三方阻塞客户端都能被同样抢占，也不构成时延 SLA。无法确认清理时仍停止根任务，不能以局部配额耗尽为理由忽略清理失败。

## 已执行验证与比较边界

本次实现交接时，`test_diagnostic_delegation.py` 的14项与 `test_diagnostic_subbudgets.py` 的12项在同一进程中合计26项通过。这个数字只描述该次选定测试子集，不是全项目回归总数，后续新增测试应重新记录。

随后字段碰撞审查发现委派耗时曾覆盖根事件时间；已改用上述独立字段，并增加真实图与审计时序回归。该新增用例不包含在初次26项记录中，不回写旧验证口径。

验证覆盖：根剩余额度动态收紧、子请求/工具循环不能占用主预留、额度不足不进入子图、时间前后边界、受控请求进程回收、未知 usage、根取消优先、全局上限传播，以及关闭扩展时的小预算兼容。

```bash
PYTHONPATH=tests .venv/bin/python -m unittest test_diagnostic_delegation test_diagnostic_subbudgets -v
```

其中主流程提交后的性能验收器是测试替身，候选只是工具链路标记，不是模型发现的优化补丁。测试通过不能写成真实模型成功率、性能收益或多 Agent 质量优势。

新默认预留改变了开启委派后的资源分配和停机行为。比较旧版本或不同配置时，应重新冻结源码、全部子配额/主预留、根预算、任务、工具、传输方式及停止策略；主子合计总预算保持相同，不能沿用旧运行批准或回写历史结果。`adaptive`/`fixed` 的离线反馈脚本只验证控制流，真实效果与请求/Token/费用收益仍待获批批次验证。
