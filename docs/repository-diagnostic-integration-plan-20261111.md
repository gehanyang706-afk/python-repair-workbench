**真实仓库诊断主图接线方案（待实施，2026-11-11）**

本文是下一阶段开发方案，不代表真实仓库诊断主图、公开 CPU 探针、隐藏评分接线或真实模型效果已经完成。当前独立仓库验收器与 SWE 公开复现链路的工程交付见 [阶段记录](repository-performance-and-public-probes-20261111.md)。本文不批准新的模型调用、镜像下载或评测补跑，不修改历史结果及原始证据。

目标是让 sqlparse、more-itertools 等完整 Python 仓库使用同一条可审计的 Agent 工具循环：理解现象与行为契约，读取源码，记录假设，按需选择公开实验，必要时构造受控复现，最后封存一个候选或审核提议。隐藏验收在 Agent 结束后独立执行，不将失败反馈用于本次再次提交。

**一、复用现有框架，分别处理公开探索和最终评分。**

继续使用已锁定的 Deep Agents 0.7.20、LangGraph 1.2.12 与 LangChain 1.4.3。现有 `create_deep_agent`、`StateBackend`、`FilesystemMiddleware`、`TodoListMiddleware`、`BudgetedModel` 和项目中间件已经提供所需循环与状态能力；不升级框架，不另写 ReAct 调度器。

```text
可信准备器：完整基线、原测试资格、公开任务与输入、源码修改范围
    ↓ 只交付公开投影与受限资格回执
RepositoryDiagnosticSession → 现有 build_diagnostic_agent / 原生工具循环
    ├─ 源码 / 引用 / 假设
    ├─ 登记的公开 CPU、内存、基准实验
    ├─ 独立复现程序（输出仍不可信）
    └─ 首次有效候选或结论封存 → 关闭 Agent
                                        ↓
图外 Evaluator：完整 WorkloadSpec → 从原基线重建 → 原测试与最终输入 ABBA → 审计结果
```

Session 不持有完整 `WorkloadSpec`、参考修复、最终输入、隐藏结果路径或可读取它们的工具。模型编写的程序、断言和自报指标不升级为可信契约或最终测量回执。已有 `ugc-export-v1` 的默认工具、提示词及验收行为保留；不借用 `SWEbenchSession`，不制造 SWE `issue_only` 或 UGC 工作区来绕过基线条件。

**二、最小 Session 与准备接口。**

拟新增 `repository_diagnosis.py`，提供以下应用侧入口；名称和签名在实施前仍可调整，不属于现有可调用接口。

```python
RepositoryDiagnosticSession(
    baseline,
    output,
    allowed_paths,
    development_workload,
    baseline_qualification,
    *,
    strategy="adaptive",       # fixed / adaptive
    enable_reproduction=False,
    max_probe_runs=6,
    **existing_budget_options,
)
```

`development_workload` 由可信准备器从 `WorkloadSpec.development_projection()` 构造并冻结，包含公开任务身份、目标、行为契约和 development inputs。公开 API 与可用实验由应用注册，例如 `sqlparse.format`、`more_itertools.islice_extended`；不根据标准修复额外提示故障辅助函数。模型不能通过工作负载配置上传新的评分器、worker、宿主路径、任意 callable 或可执行命令。

`baseline_qualification` 绑定基线快照、原测试套件散列、镜像、原始测试报告与接受结果。准备器核对 `WorkloadSpec.original_test_result(...)`，Session 再核对受限回执和原文件身份。SQL 原有 XFAIL/XPASS 保留精确身份及真实状态，不能改成全部 `passed`。源码修改范围继续从完整基线与操作方策略生成，不按参考补丁挑文件。

继承 `DiagnosticSession` 的证据登记、源码引用、假设更新、预算、停滞检查、只读调查、ToolRuntime 及受控复现；覆盖任务边界。拟涉及以下接口：

| 位置 | 拟修改或新增内容 | 默认 UGC 行为 |
|---|---|---|
| `diagnosis.py` | 把 workload ID 与相关阈值验证抽为 `_validate_workload_configuration(...)` | 仍仅接受原 UGC 配置 |
| `repository_diagnosis.py` | 新增 `RepositoryDiagnosticSession(DiagnosticSession)` | 独立入口显式启用 |
| 子类 `_validate_workload_configuration` | 仅接受可信注册的真实 workload，按实际 speed/memory 目标验证阈值 | 不放宽父类校验 |
| 子类 `_validate_baseline` | 验证完整原套件资格、精确 XFAIL/XPASS、清理及环境绑定 | 不要求或伪造 `map_exporter.py` |
| 子类 `initial_context` | 仅公开投影、原测试摘要、真实入口与输入约束 | 不改变 UGC 上下文 |
| 子类 `run_diagnostic` / `read_probe_report` | 真实仓库公开实验与按 case 引用回查 | 不复用 UGC 数据形状 |
| 子类 `_verify_candidate` | 只独立重建并封存提交 | 不调用隐藏评分 |
| 子类 `check_budget` / `save` | 最终封存后关闭，保存提交身份与真实执行状态 | 不改变原修复默认流程 |

现有父类的 `minimum_speedup > 0` 条件不能直接用于内存目标；内存任务的 `min_speedup=0` 由新配置 hook 按目标验证。不能传一个虚假的 UGC ID 或耗时阈值来绕过构造器，再覆盖字段。

**三、登记的公开实验与受控复现。**

拟新增 `repository_probes.py`，只把 development inputs 转为可信执行 job。模型侧工具可采用 `run_probe(probe, hypothesis_id=None, case_id=None)`；`case_id` 必须来自已冻结的公开输入，省略时使用冻结的代表输入。第一版仅实验原始基线，不开放正数候选 `attempt`。同一任务两组具有相同工具及输入权限。

当前 `repository_measure_worker.py` 只支持 benchmark 与 Python tracemalloc 内存测量，公开 CPU 探针仍需实施。可新增公开探针 worker，复用已审查的注册业务操作，增加 cProfile 与有界热点输出；CPU 报告只用于定位，不进入无 profiler 的最终耗时判定。公开和最终执行使用同一业务入口，但分别接收 development 与 final 输入，并使用不同回执及消费者。

公开热点记录规范的仓库相对路径或受限外部库名称，完整报告落盘，模型默认接收小摘要。所有样本、输入身份、环境、版本、超时及错误保留。原始报告通过可信校验后登记为工具观察，不称独立验收。原生分配/RSS 仍不在当前执行器范围内，不能仅改字段名扩大指标含义。

固定组预诊断按冻结次序执行 CPU → 内存 → 基准，预跑成本全部计入。CPU 支持未完成时，不得宣称这个真实仓库对照已经可执行；应先返回明确不支持，或另行冻结实际支持的实验定义。自适应组从相同公开起点选择动作，不同时启用 Skills、委派或并发来混淆主对照。

公开实验缓存至少绑定 `snapshot_ref + public_workload_hash + input_sha256 + adapter_sha256 + probe + sampling_protocol`。每次工具请求计根工具预算，每个物理派发计实验预算；回查与缓存命中分别计数。失败、截断、输入漂移和清理未知不缓存为有效证据。探针执行串行，避免污染性能测量；高频存活检查不重新全量哈希，关键操作边界仍完整核验身份。

受控复现复用 `ReproductionWorkspace` 与当前真实状态管理，不重写一套执行器。sqlparse/more-itertools 使用已准备的独立执行镜像及 `standalone-v1`；不把 SWE 的固定 conda profile 误用于这两个仓库。模型提出代码，应用仅在资源受限、无网络、只读源码的容器中运行；程序只能产生 `model_program_output` 观察，不能修改原测试、候选、评分端或工作负载注册表。

继承时必须读取 `workspace.blocked_reason` 与实际 `workspace.cleanup_ok`。启动前源码、程序或 wrapper 漂移已由 `_verify_inputs` 持久化封锁；不能把异常仅转换为普通参数拒绝后继续调用模型。运行获准时清理状态先置为未确认，只有实际清理回执才能恢复为已确认；即使取消、中断或清理函数异常，也不能仅凭没有特定停止原因推断已经清理。公开结果、最终 Session 结果和图外批次三处都必须保留这个事实。

**四、本地报告先接通，MCP 使用显式适配。**

当前 `ReportStore`、`freeze_probe`、stdio 报告工具及其参数白名单绑定 UGC `small/large`。真实仓库报告应按公开 `case_id` 管理，不能把任意 case 硬改名后冒用旧协议。

第一步可新增本地仓库报告存储或版本化的报告适配，提供 summary、hotspots、samples、environment 的受限分页；绑定完整原报告散列及 snapshot、input、worker、image 身份。复用 ToolRuntime 的版本化查询缓存和相同请求合并，证据引用只说明来源，不证明根因判断正确。

本地链路未完成 MCP 适配前，真实仓库选择 MCP 应在首次模型调用前明确拒绝，不静默退回本地。后续新增以 `case_id` 为维度的版本化 MCP 参数和相同校验器，检验本地/stdio 结果一致、取消与断连清理、来源身份和分页边界。最终输入与隐藏报告始终不进入公开 MCP 目录。

**五、单次提交与图外评分。**

`diagnostic_agent.py` 只增加显式真实仓库模式分支：使用相应实验及报告 schema，并说明提交只是封存。继续复用同一个原生 Deep Agents 图、虚拟文件和中间件。当前 UGC 提示词中的“提交即独立复验、失败后继续调整”不适用于这一模式，必须替换，不能只改 Session 后仍向模型承诺旧行为。

第一版的 `submit_ref_edits` 继续校验来源、版本、唯一匹配、权限和语法，并从基线独立重建。首次有效提交生成不可覆盖的提交回执，绑定候选、补丁与公开任务身份，返回 `submitted` 后阻断下一次模型请求和工具调用。预检拒绝可在原候选预算内纠正；不能获得隐藏结果后再修补。`conclude_diagnosis` 同样封存提议并关闭会话。

拟新增 `repository_live_evaluation.py`：可信批次对象持有完整 `WorkloadSpec` 与最终环境；Agent 结束后检查冻结身份及清理，调用 `ctx.begin_evaluation(submission_hash)`，再调用现有 `verify_repository_candidate(...)`。最终评分从原始基线独立重建候选，执行原测试与 final 输入的完整 ABBA，保留失败、不可判定、未运行项及原始样本；只写入图外审计，不交还本次 Agent。

第一版不支持公开候选反复试验；后续若增加 staging/preview，必须与最终提交分开，公开试验只使用 development 输入。当前 `WorkloadSpec` 只定义 speed/memory，`create_candidate` 拒绝空补丁，因此 `no_change`、`insufficient_evidence` 暂只能封存为审核提议，不自动计为联合通过。健康任务需要独立契约与图外评分扩展后才能给出验收成绩。

**六、实施批次与验收门槛。**

先完成公开投影/基线资格和 Session hook，再实现公开实验与本地报告，随后接单次提交和图外评分。各步骤分别用确定性脚本及已知候选验证机制；不在当前物理控制或真实批次进行中改动其冻结实现。完整离线门槛通过后再冻结真实模型计划，重新确定模型、任务、输入、源码、工具协议、预算及停止条件；历史调用授权和剩余预算不复用。

| 门槛 | 必须验证的结果 |
|---|---|
| 原生主图 | 真实调用链完成源码→假设→公开实验→更新假设→受控复现→提交；无需新 ReAct 循环 |
| 任务投影 | final 输入、checker、参考补丁和隐藏路径哨兵不进入消息、证据、虚拟文件、复现挂载或 MCP 目录 |
| 原测试资格 | 精确登记的原有 XFAIL/XPASS 被接受，新增或缺失身份被拒绝，原始状态保留 |
| 目标配置 | speed/memory 按各自契约校验；旧 UGC 的配置拒绝与默认行为不放宽 |
| 公开探针 | CPU/内存/基准绑定同一公开输入；CPU 指标不充当最终耗时，源码只在容器中导入 |
| 资源约束 | 主流程、框架工具、探针与复现共享预算；物理派发、缓存、拒绝和回查分母清楚 |
| 复现失效 | 启动前漂移永久封锁；取消或清理中断后 Session 与批次均保留未确认状态 |
| 最终封存 | 补丁、不改与证据不足均阻断下一次模型请求；预检拒绝不暴露隐藏反馈 |
| 独立验收 | 模型自报观察/指标不能进入 scorer；篡改候选、原测试或冻结绑定被拒绝；隐藏失败不触发第二候选 |
| 固定/自适应 | 固定组真实执行登记顺序并收费；自适应组能选不同动作；共同工具、起点和预算可核对 |
| 噪声与健康 | 噪声保留 inconclusive；健康提议未具备评分契约时不记通过，不改阈值或重复运行追求有利结果 |

本方案通过后仍只能声称“真实仓库诊断与独立评分链路可执行”。自主实验选择是否提升联合达标率、压缩无效探索或节省 Token，需要新的同模型、同预算、完整保留失败的真实任务对照，不能由工程测试或已知参考候选推导。
