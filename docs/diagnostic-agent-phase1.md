# 注册工作负载诊断与性能优化 Agent：第一阶段实现

本阶段为现有修复工作台增加独立的 `performance_optimization` 任务类型。它面向固定的 Python 地图导出接口，通过单个 Deep Agent 选择源码调查和诊断工具，再由应用独立核验候选的功能、耗时和 Python 分配。原 `bug_repair` 路径仍要求先复现原始断言失败，性能任务没有放宽原修复判定。

本文描述当前代码、运行契约和验证方法，不提供尚未完成的测试总数、模型成功率或性能收益。新增真实模型入口默认关闭；本阶段没有获得真实模型 API 调用授权。脚本模型及已知候选的容器检查只证明执行链路，不能记为模型自主诊断或优化成绩。

## 实现范围

新增三个 CLI 入口：

| 入口 | 职责 | 模型调用 |
| --- | --- | --- |
| `probe` | 对已有快照执行注册的 `cpu`、`memory` 或 `benchmark` 诊断 | 无 |
| `diagnose` | 单个 Deep Agent 按工具观察选择调查、假设、实验、修改或停止 | 只有显式启用真实入口时才可能发生；本阶段未授权 |
| `verify-performance` | 从可信基线独立重建候选，重新运行原测试并进行配对性能验收 | 无 |

Python 接口分别为 `diagnostics.run_probe`、`diagnosis.DiagnosticSession` 配合 `agent.run_agent`、`performance.verify_performance_candidate`。Agent 工具定义在 `diagnostic_agent.py`；CLI 参数和开关在 `__main__.py`。

当前唯一注册工作负载为 `ugc-export-v1`，入口固定为 `map_exporter.py:export_map`。它覆盖 Python 内容处理工具链，不运行游戏引擎，不衡量渲染、物理、GPU 或玩法体验。不能把文件名相同但业务契约不同的陌生仓库直接视为已适配。

## 调用链与责任

```text
可信 Git 提交
  → create_snapshot：独立源码快照、文件哈希和内容身份
  → run_tests：固定 tests/、本机不可变镜像、原语义测试全部通过
  → DiagnosticSession：批准源码范围、固定工作负载、资源上限
  → 单个 Deep Agent
      ├─ 读取基线源码 / AST 符号 / 字面搜索
      ├─ 登记短假设与已发放的 evidence_ref
      ├─ 选择 CPU / Python 分配 / 无 profiler 基准
      └─ 提交 source_ref + old_text + new_text，或提出无需修改/证据不足
  → 精确编辑校验、语法预检、重复候选识别
  → 从原基线独立重建，仅复制批准的源码变化
  → 原测试身份与结果核验
  → 同一不可变镜像上的配对 benchmark + memory
  → 应用验收回执、补丁和完整测量证据
```

Agent 负责选择行动和提出解释。应用负责文件权限、版本、执行、停止和最终评分。`supported`、`rejected` 假设状态是模型判断；真实证据引用证明其来源存在，不能证明该因果解释成立。

候选不覆盖原仓库、原快照或历史候选。每次提交都从原基线派生，后续提交必须包含仍需保留的全部变化，不是在上一候选上隐式累积。模型不能批准新的可改文件，不能修改测试、基准脚本或评分规则，也不能用自己提交的测试报告代替独立验证。

## 单 Agent 自主选择与固定顺序对照

`adaptive` 将源码导航、假设登记、三种诊断、证据回查、精确编辑和结论工具暴露给单个 Agent。应用不强制每题都先运行 CPU、内存、基准。Agent 可以根据已有证据选择下一实验，也可以提出 `no_change` 或 `insufficient_evidence`。

`fixed` 在首个模型调用前由应用依次运行 `cpu → memory → benchmark`，再让同一类 Agent 使用已有证据继续。两种策略具有同样的工具目录、批准范围及资源上限，固定顺序预运行也消耗共享工具和探针额度。它比较的是诊断调度策略，固定策略仍包含模型后续决策，并非整个任务由规则脚本完成。

当前离线检查使用可依据真实 `ToolMessage` 分支的确定性模型替身，验证证据引用、反馈分支和共享额度连接。它不能证明真实 LLM 会选择正确工具。尚无真实模型的 adaptive/fixed 成对效果结论。

## 证据与信任边界

- `source_ref` 是应用发放的精确编辑句柄，绑定已读原文和基线版本；它不是任意文件权限。
- `evidence_ref` 对应 `evidence/E*.json` 中的工具观察，带快照身份和内容摘要哈希。Agent 只能引用本次会话实际发放的引用，不能把猜测编号变为证据。
- 假设和最终无需修改结论分别标记为模型判断、待审核提议。没有通过候选时，文字结论不转为性能验收成功。
- 同一不可变快照、探针、工作负载的重复诊断返回已有成功观察，明确标记 `reused`。它不是新的实验或新的独立重复；失败或清理未确认的运行不缓存成成功。
- 原始诊断结果保存为 `result.json`、`probe.json` 和 `probe.log`，CPU 还保存各规模 `.pstats`。模型上下文接收有界摘要和引用；完整样本在文件中供操作方复核，并未开放任意主机报告路径读取。
- `events.jsonl`、会话 `result.json`、`model_response.txt`、候选及独立验收目录保留过程。模型叙述与 `state` 正常结束都不能替代候选的 `status` 和实际验收回执。

注册探针只在 Docker 内导入目标 `map_exporter.py`。宿主机的快照、导航和候选构造不导入待修代码。公共夹具资格入口仅允许哈希固定、经审查的四个内置夹具，不提供任意候选的宿主机加载器。

容器使用本机镜像、`--pull never`、无网络、只读根目录、只读源码、受限临时目录、1 CPU、256 MiB、进程数限制和独立结果挂载。运行绑定不可变 `image_id`，退出后确认本次唯一容器被清理。这是受控项目执行边界，不能视为面向恶意项目的完备操作系统安全证明。

## 三类诊断的含义

| 探针 | 实际测量 | 可用于什么 | 不能推出什么 |
| --- | --- | --- | --- |
| `cpu` | `cProfile` / `pstats`，函数调用次数与累计时间，摘要最多15项热点 | 判断重复工作和热点所在 | 最终无分析器条件下的速度收益 |
| `memory` | `tracemalloc` 在导出调用期间的峰值 Python 可追踪分配 | 比较 Python 分配及速度/内存权衡 | 进程 RSS、全部原生内存、GPU 或整机内存 |
| `benchmark` | 关闭 profiler 的 `perf_counter_ns` 调用耗时 | 注册输入上的耗时比较 | 任意地图、任意硬件或生产 SLA |

输入构造、期望输出构造和输出核验位于计时区间之外。内存测量不包括输入构造，报告明确其口径。每个探针仍核验输出顺序、第一项重复资源获胜、缺失资源抛 `ValueError`、空输入和输入不被修改，避免用少做工作换速度。

工作负载固定两种规模：small 为300个对象/80个基础资源，large 为6000个对象/600个基础资源；每组另含用于第一项语义检查的重复资源。注册探针使用整数 ID，公共可见测试也覆盖字符串 ID。资源 ID 与对象的资源引用应可哈希；对象 ID 在输出中保持原值。

## 独立性能验收

验收前，基线必须有与快照身份一致、清理完成且所有原测试通过的记录；候选只允许既有文件中的批准 Python 源码变化。独立重建不采信候选自报的变化文件或测试结果，随后重跑原 `tests/`，核对测试身份集合和全部结果。

通过原测试后，应用执行两轮配对：第一轮 baseline/candidate，第二轮 candidate/baseline。`benchmark` 和 `memory` 分别形成 ABBA 顺序，总共 **8次测量探针**；它们与探索阶段探针分开计数。每次 benchmark 先预热2次，再保存7个无 profiler 原始计时；每个角色、每个规模合计14个计时。每次 memory 保留一个峰值，每个角色、每个规模合计两个峰值。不选最好样本，不用 CPU profiler 耗时评分。

所有测量须拥有匹配的不可变镜像、工作负载指纹、工具脚本哈希和运行环境；每个报告还须与对应快照身份一致。各规模输出指纹也必须一致。

默认验收规则：

1. 原测试和注册语义契约全部通过。
2. large 中位耗时相对基线至少减少15%。
3. small 中位耗时不超过基线的1.10倍。
4. 每种规模的候选最高分配峰值，不超过基线最低峰值的1.25倍。
5. 每种规模、每个角色的计时相对噪声，即总体标准差/均值，不超过0.20。

高噪声、缺失或损坏报告、身份/环境混杂、超时和清理未确认是 `inconclusive`；有效测量下未达到收益或资源阈值是 `failed`；语义失败也不能由更快的计时补偿。完整原始报告和计算规则保存，不因首次结果不确定或失败而自动重跑或放宽门槛。

CLI 的阈值参数由可信操作方在实验前固定；它们没有作为 Agent 工具参数开放。同一次比较不得在看到结果后修改门槛。该协议是有限工作负载的验收方法，不是通用性能显著性检验。

主验收文件位于验证目录的 `performance-verification.json`；8次探针位于其同级 `<验证目录名>-measurements/`。原测试报告仍在验证目录的 `reports/`。

## 停止与资源边界

`diagnose` 默认上限为3个候选、12次模型请求、6次探索探针、60次应用工具调用和300秒会话总期限；单次容器超时默认30秒。模型参数恢复使用共享请求额度，最多两次恢复，不建立额外调用预算。每个候选的独立验收最多另执行8次测量探针及一次原测试；不能把6次探索上限误解为任务所有容器运行的总量。

探针和候选验收共用会话期限、取消信号及执行控制，串行执行，不并行争用测量资源。已通过候选不再接受新修改；无需修改/证据不足结论会封闭后续实验和提交。清理未确认会硬停止诊断会话。用尽次数、会话期限、取消、连续无效工具错误及模型连接/读取/墙钟超时均保留停止原因。

当前诊断路径只有上述次数/时间限制和可报告 Token 用量统计，**尚未接入持久 Worker 的 Token 预留账本、批次 Token 硬上限或费用硬上限**。`DiagnosticSession` 明确拒绝持久 Worker 恢复参数。缺失 usage 不得解释为零费用；请求次数上限也不等同于费用保证。

`diagnose` 缺省拒绝真实入口，只有显式 `--allow-real-model` 才可能加载模型。该开关是本地操作门槛，不能替代用户授权，也不能自行核验某批次已获批准。真实运行前仍须单独冻结精确模型/地域、角色、任务、请求/输入输出 Token、并发、超时、费用或免费额度、停止规则，并落实所需费用保护。不得沿用历史剩余额度，也不得因为环境已有密钥而试跑。模型配置中的默认名称不代表获批模型。

## 数据集与效果边界

`benchmarks/ugc-diagnostics-dev-v1` 是四个公开、合成的开发夹具，包含可见语义测试及固定哈希。资格检查验证已审查夹具的基础语义与不可变内容，不证明诊断准确率或速度收益。四题不是独立评测集，不是未知真实上游缺陷，也没有隐藏任务答案。

当前测试区分：纯应用与工具契约、真实 Deep Agents 循环接确定性模型替身、已知候选的真实 Docker 验收。后两者仍不是实际 LLM 自主发现补丁。真实模型成功率、adaptive/fixed 效果、陌生仓库泛化和正式独立集结果均尚无本阶段证据。

## 本机运行与验证

以下命令从项目根目录执行，使用项目 `.venv`。本机既有开发镜像名为 `study-agent-deepagents:0.1`，可通过 `REPAIR_TEST_IMAGE` 指向另一个已存在的兼容镜像。程序不自动下载镜像，不安装新依赖。运行前可以只读检查镜像；没有本地镜像时停止容器验收，不自动拉取。

```bash
cd /Users/gehanyang/编程/python-repair-workbench
docker image inspect study-agent-deepagents:0.1 --format '{{.Id}}'
.venv/bin/python benchmarks/ugc-diagnostics-dev-v1/qualification.py
.venv/bin/python -m unittest discover -s tests -v
```

针对新增机制的检查：

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_diagnosis.py' -v
.venv/bin/python -m unittest discover -s tests -p 'test_diagnostics.py' -v
.venv/bin/python -m unittest discover -s tests -p 'test_performance.py' -v
.venv/bin/python -m unittest discover -s tests -p 'test_diagnostic_cli.py' -v
.venv/bin/python -m unittest discover -s tests -p 'test_ugc_workloads.py' -v
```

真实容器检查必须串行执行，避免同时运行其他基准造成噪声；这些命令不调用真实模型：

```bash
RUN_DOCKER_TESTS=1 REPAIR_TEST_IMAGE=study-agent-deepagents:0.1 .venv/bin/python -m unittest discover -s tests -p 'test_diagnostics.py' -v
RUN_DOCKER_TESTS=1 REPAIR_TEST_IMAGE=study-agent-deepagents:0.1 .venv/bin/python -m unittest discover -s tests -p 'test_performance_docker.py' -v
RUN_DOCKER_TESTS=1 REPAIR_TEST_IMAGE=study-agent-deepagents:0.1 .venv/bin/python -m unittest discover -s tests -p 'test_diagnostic_docker.py' -v
RUN_DOCKER_TESTS=1 REPAIR_TEST_IMAGE=study-agent-deepagents:0.1 .venv/bin/python -m unittest discover -s tests -v
```

专项或全量容器命令按验收需要选择，不必为了同一结论反复测量。`test_performance_docker.py` 使用已知候选，验证原测试、ABBA 报告身份/样本/清理和接受规则；合法的性能失败或高噪声不强制改判为成功。每次尝试在 `runtime/performance-verifier-docker/` 新建记录，不覆盖首次结果。`test_diagnostic_docker.py` 验证确定性模型替身、真实工具回执和候选验收的完整连接。

### 使用三个新 CLI

先从可信公共开发夹具建立独立 Git 仓库与通过的基线；示例不修改内置夹具：

```bash
mkdir -p runtime
DIAGNOSTIC_RUN_ROOT=$(mktemp -d "$PWD/runtime/diagnostic-phase1.XXXXXX")
cp -R benchmarks/ugc-diagnostics-dev-v1/case-01/source "$DIAGNOSTIC_RUN_ROOT/source"
git -C "$DIAGNOSTIC_RUN_ROOT/source" init -q
git -C "$DIAGNOSTIC_RUN_ROOT/source" add .
git -c core.hooksPath=/dev/null -c commit.gpgsign=false -c user.name=Fixture -c user.email=fixture@example.invalid -C "$DIAGNOSTIC_RUN_ROOT/source" commit -qm 'Trusted public diagnostic fixture'
.venv/bin/python -m repair_workbench snapshot --repo "$DIAGNOSTIC_RUN_ROOT/source" --output "$DIAGNOSTIC_RUN_ROOT/baseline"
.venv/bin/python -m repair_workbench test --snapshot "$DIAGNOSTIC_RUN_ROOT/baseline" --image study-agent-deepagents:0.1 --timeout 30
.venv/bin/python -m repair_workbench probe --snapshot "$DIAGNOSTIC_RUN_ROOT/baseline" --output "$DIAGNOSTIC_RUN_ROOT/probe-cpu" --image study-agent-deepagents:0.1 --kind cpu
.venv/bin/python -m repair_workbench probe --snapshot "$DIAGNOSTIC_RUN_ROOT/baseline" --output "$DIAGNOSTIC_RUN_ROOT/probe-memory" --image study-agent-deepagents:0.1 --kind memory
.venv/bin/python -m repair_workbench probe --snapshot "$DIAGNOSTIC_RUN_ROOT/baseline" --output "$DIAGNOSTIC_RUN_ROOT/probe-benchmark" --image study-agent-deepagents:0.1 --kind benchmark
```

输出目录不得已存在，不得写在源码、基线或被测快照内部。`test` 的原始记录同样不覆盖重跑；需要新的尝试时建立新快照。

以下 `diagnose` 命令展示参数及默认拒绝行为，**没有**真实启用开关，因此应在加载密钥/模型前退出；它不是离线模型运行命令：

```bash
.venv/bin/python -m repair_workbench diagnose --baseline "$DIAGNOSTIC_RUN_ROOT/baseline" --output "$DIAGNOSTIC_RUN_ROOT/agent-run" --allow map_exporter.py --task '诊断地图导出性能，保持资源引用、顺序与输入不变；证据不足时停止。' --strategy adaptive --max-model-calls 12 --max-attempts 3 --max-probe-runs 6 --deadline 300 --timeout 30
```

`--strategy fixed` 选择固定顺序对照。CLI 目前没有脚本模型 provider 参数；离线模型替身通过测试中的 Python 注入路径运行。真实调用未获批时，不补上 `--allow-real-model`。

已有由 `candidate` 命令或诊断会话生成的候选时，可在新的独立目录复验；先把下例路径替换为实际候选快照目录。该命令只运行容器，不调用模型：

```bash
DIAGNOSTIC_CANDIDATE_DIR=/absolute/path/to/candidate
.venv/bin/python -m repair_workbench verify-performance --baseline "$DIAGNOSTIC_RUN_ROOT/baseline" --candidate "$DIAGNOSTIC_CANDIDATE_DIR" --output "$DIAGNOSTIC_RUN_ROOT/independent-verification" --allow map_exporter.py --workload ugc-export-v1 --timeout 30 --minimum-speedup 0.15 --max-memory-ratio 1.25
```

退出码：`verify-performance` 的通过/失败/无法判断分别为0/1/2；`probe` 只有运行成功且清理确认返回0，其余返回2。`diagnose` 还须结合会话状态、停止原因与候选回执解释，不能只看模型最后一句话。

## 当前未实现与扩展条件

默认仍为单主 Agent、StateBackend、有限框架只读文件工具和待办。后续已接入默认关闭的[候选 Skills 与只读 investigator 扩展](diagnostic-agent-phase2.md)，可分别启用；其框架链路验证不代表真实效果提升。通用子 Agent 继续关闭，未启用 investigator 时 `subagents=[]`。另有可选[本地stdio MCP诊断接入](diagnostic-agent-mcp.md)。诊断 HTTP API、网页诊断流程、MySQL Worker 恢复、任意 Shell、编辑器协议、游戏引擎适配、专职 reviewer、跨任务自动经验晋升、持久策略记忆和 CompositeBackend 证据工作区仍未实现。

迁移到陌生仓库需要新的受审查适配器：固定目标接口与工作负载、输入和语义契约、允许源码范围、兼容且已存在的镜像、原测试身份、诊断报告格式、测量规模/噪声和接受规则。适配器必须在可信应用侧注册，模型不能临时生成新入口、评分脚本或放宽阈值。之后再以未参与适配与提示词调整的任务验证泛化，分别报告真实上游任务和自建缺陷。

扩展到 Godot 或其他编辑器还需单独设计场景/资源格式、修改权限、回滚和目标设备测量；不能仅放开文件后缀或把 headless 逻辑测量解释成渲染收益。
