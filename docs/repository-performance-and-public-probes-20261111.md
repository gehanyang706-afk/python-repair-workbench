**真实仓库独立验收与 SWE 公开复现：2026-11-11**

本轮将原隔离候选中的参数化测量、精确测试回执和性能评分合入主版本，并补齐独立候选重建入口；同时为 SWE Agent 增加默认关闭的公开基线复现实验。新增真实模型请求为0，不改写11月10日的两份失败结果，也不复用该批未使用的Token预算。

**独立性能验收。** `repository_performance.verify_repository_candidate` 接受应用冻结的 WorkloadSpec、原始快照、提交候选及修改白名单。它自行提取实际源文件差异，从原始基线重建新的候选；保留 CRLF 等原始字节，不复制提交目录中的报告、依赖或自报成功状态。测试、配置、新增文件和越界路径不能借白名单绕过。

评测端分别运行基线与重建候选的原测试，收集精确pytest nodeid及状态；按事先登记的XFAIL/XPASS验收，不将跳过或新增XPASS视为正常通过。测试收集错误、退出状态异常、名单缺失、清理未知均不能生成可信通过回执。原套件散列同时覆盖测试及其配置。归档来源的上游提交别名须与操作方提供的完整本地Git树绑定；该检查不替代上游来源认证。

随后每个最终输入分别运行无profiler计时与Python分配测量，各按 baseline→candidate→candidate→baseline 顺序建立独立容器。每个块保存样本、环境、输出指纹和资源回执；所有样本输出一致后才压缩为评分摘要。先验收行为及原测试，再联合判断主收益、另一资源退化与边界输入；噪声超限保持inconclusive。所有计划块预先登记，取消、错误或清理未知后保留未运行项。

当前执行器只适配固定的1CPU/256MiB、无网络、Python可追踪分配、canonical JSON输出与三个已登记构造器：`sql-format-v2`、`sql-comments-v3`、`iterator-slice-v2`。构造器对应sqlparse和more-itertools两个公开API；不接受任意宿主函数、命令或运行时导入路径。工作负载契约中更宽的RSS、exact_bytes、额外语义oracle或超出执行器范围的采样参数在启动前拒绝，不套用未实际执行的标签。

每次物理执行只挂当前快照、该输入job和可信worker；不向候选挂载完整最终输入清单、评分规则或评分目录。测量输出由宿主边读边限制，清理及输入完整性必须确认。执行前后及最终评分后校验快照、manifest和本次实现指纹。JSON来自受控项目进程，producer名称与hash不是恶意代码防作弊认证；候选也必然能看到当前输入。

该验收函数与CLI不在Agent工具目录中，结果不会反馈给本次仍在运行的模型。命令由操作方提供可信契约和权限，例如：

```bash
python -m repair_workbench.repository_performance \
  --baseline /absolute/baseline --candidate /absolute/submitted \
  --output /absolute/new-final-run \
  --contract /absolute/frozen-contract.json --contract-sha256 CONTRACT_SHA256 \
  --allow-source package/module.py --total-seconds 900
```

`--allow-source` 可重复；完整仓库的修改范围应由准备端依据源码树确定，不按标准答案挑文件。`--test-pids-limit` 只由操作方固定，默认64；历史more-itertools原测试需要128的事实继续保留。CLI退出码0仅表示该冻结任务通过，1为候选失败，2为不可判定或中止。

**真实物理控制。** 使用已经暴露于开发过程的完整sqlparse历史仓库，沿用旧v5的输入、采样和阈值，每个代码版本提前固定各一次正负控制，不追加试跑寻找通过。契约版本因新增独立重建、原套件绑定与检查器登记而重新冻结。

| 控制 | 原测试 | 性能探针 | 结论 |
|---|---|---|---|
| 上游参考修复的源文件差异 | 基线与独立重建候选均496 passed、2 XFAIL、1原有XPASS | 计划16、完成16、未运行0；输出一致、清理确认 | 联合验收通过 |
| 故意错误的format实现 | 基线通过；候选98项失败，其余状态保留 | 计划16、执行0、未运行16 | 原测试拒绝，未启动性能评分 |

最终v2正例耗时63.408秒；负例2.942秒。首次正负控制的65.192秒/3.505秒及相同判定也完整保留；首次通过后因修复共享复现执行器的清理/输入边界，在新代码版本按相同条件复验，不混合两版本计算成功率。正例只应用源文件差异，未携带上游新增的benchmark或CHANGELOG，原测试没有修改。负例是验收器拒绝控制，不计入真实缺陷任务数。所有控制均不是Agent尝试，不构成修复率、Token节省或自主实验选择证据。more-itertools此前因耗时波动不可判定的记录继续保留，本轮没有复测到通过，也未放宽门槛。

原始控制位于 `runtime/repository-final-{control,negative}-20261111-{first,v2}`；每次保存契约、候选、原测试、计划块及完整报告。本次11个验收实现文件在运行后逐hash匹配运行前记录再归档，明确标记为postrun verified archive，不宣称运行前保存了整个项目及依赖快照。

**SWE提交前公开复现。** `run_qualified_agent(..., enable_reproduction=True)` 可显式开放 `propose_reproducer` / `run_reproducer`，默认仍关闭。两组必须使用同样的工具和权限，复现提议与运行计共享工具预算、取消与截止时间。程序只运行在原始公开基线，可调用公开API或原有公开测试；不运行官方测试补丁，也不试跑最终候选。

镜像来自已通过环境资格的同一个不可变镜像ID，调用方不能覆盖。固定 `swebench-testbed-v1` 使用 `/opt/miniconda3/envs/testbed/bin/python`，遮蔽镜像 `/testbed`，避免默认base Python及镜像内Git历史干扰。该profile目前针对这一conda布局；其他镜像需要另行资格，不声明普遍兼容。

真实Django只读探针确认：Python路径正确，Django从 `/workspace/django/__init__.py` 导入，遮蔽后的 `/testbed` 为空，两处 `.git` 不可见，输入完整且容器清理确认。该检查仅2次工具、0模型、0官方评分。复现输出始终标为 `model_program_output`，即使内部自称passed或official，也不会升级为可信验收。

批次配置可增加严格的 `swebench.reproduction={enabled,max_programs,max_runs}`；开启时冻结工具清单必须同时包含两工具。Agent无提交、局部截止和异常出口现在使用真实复现清理回执，缺失或清理未知即停止剩余批次；输入漂移也停止，不能沿用旧的“Agent未启动容器”假设。

**验证边界与后续。** 新独立验收专项13项通过；SWE公开复现新增14项，包括原生图提出程序→执行→源码搜索→提交、取消且清理未知、运行前程序漂移与清理本身被中断。共享执行器现在在物理运行前持久化清理待确认，仅实际清理事实才能恢复True；运行前完整性检查失败同样持久封锁会话。相关59项回归50通过、9项未开启Docker而跳过；这些是机制检查。首次辅助测试命名、CRLF与两处停止边界红测、写错兼容测试模块名以及旧mock缺少新状态的首次日志都保留。

最终完整Docker/MCP/Redis回归687项，671通过、16项MySQL服务检查跳过、0失败，147.099秒；先前685项回归记录仍保留。最终wheel逐字节核对67个产品文件，数据集完整性与依赖检查通过。机器可读结果及原始证据散列见[交付摘要](repository-performance-and-public-probes-20261111.json)。

当前仍未完成真实仓库诊断主图的CPU/内存/基准统一接线、公开候选试验与图外最终提交协议、健康对照及正式性能任务资格。旧UGC入口继续使用其既有规则，不能因本模块合入就称任意仓库性能Agent已经完成。下一步复用现有Deep Agents图、证据与假设机制适配真实仓库，随后另行冻结F0固定预诊断与A1按需选择的真实模型对照。
