**真实任务完善进展：2026-11-10**

本轮按[完善计划](/Users/gehanyang/编程/python-repair-workbench/docs/real-task-improvement-plan-20261109.md)推进。阶段0已完成共用评测基础，阶段1已接通真实仓库批次与Issue对照，并取得首道开发题两次一致的官方正负例资格。阶段2完成候选池选择工具与固定数据元信息准备，正式效果子集仍未冻结。真实模型API请求为0。

**阶段0的实际调用链。** `create_manifest → FrozenEvaluationBatch → RunContext.metered_model → EvaluationLedger.reserve → ManagedTongyi → EvaluationLedger.settle → 独立executor回执`。清单绑定显式文件内容、共用配置、模型/工具/预算、任务分组、缺陷簇与配对次序；执行前、模型调用前校验漂移。完整清单留在评测端，`agent_task`只投影公开问题和基线标识。

新账本从旧公开试验中抽离，输入/输出上限由冻结配置传入。先持久化最大用量预留，只有完整且一致的供应商回执才能释放余额；不同角色共用任务和批次Token/请求上限。未知用量、落盘失败会停止后续请求；不支持重开账本、跨进程恢复或分布式计费。原公开试验保留兼容类名和原预算默认值，历史原始记录不变。

批次保存全部预定行，取消、异常、预算停止和未运行不消失。最终评分有单独时限，进入评分后关闭模型请求；容器清理未确认时停止整批。逻辑提议、执行、物理派发、缓存及候选可以关联到同一run的事件记录。工具实际语义与正确性仍由可信执行器检查，通用批次模块不采信模型自报成功。

**阶段1的两组定义。** 新的 `SWEbenchSession(swe_policy=...)` 支持 `on_demand` 与 `issue_evidence`。两组使用相同原生Agent图、系统提示词、工具schema、局部编辑协议与预算。优化组仅从公开Issue提取字面符号，通过真实搜索工具组织最多4次检索、3个片段、6000字符的初始证据；所有读取均计预算，无命中退回按需调查。返回的source_ref可由现有提交工具使用。它是待实测的上下文干预，尚无收益结论。

新入口 `run_swebench_batch` 在首个模型前核对全部任务的资格、完整公开字段、快照、镜像、执行策略和官方harness；每行和模型调用前复查实现/依赖。调用者显式提供密钥和冻结批次，不自动读取旧账号配置或使用历史剩余额度。北京区原生地址在新计量传输中显式固定；其他区域尚未适配。

首次有效提交只返回submitted，随后从实际候选导出预测，由独立官方执行器评分。resolved才转成通过；缺失报告、评分环境漂移、清理失败都不能转成成功。框架与评分stub测试仅证明接线，不计SWE模型成绩。当前修复Session仍有60次工具、30次模型请求的实现上限，新入口明确拒绝宣称更高额度已生效。

**首题环境修复与实际证据。** 原始镜像缺少pytest-httpbin/httpbin等历史依赖；none网络使连接立即失败，bridge网络使测试目标可连接，两者都不满足原测试的ConnectTimeout前提。先保留失败，再分别验证依赖与网络。

- 由PyPI官方发布元数据下载并核对9份上游锁定版本依赖，合计1,310,005字节；安装只发生在无网络临时容器中。PyPI对本地httpbin测试服务的说明见[pytest-httpbin 0.2.0](https://pypi.org/project/pytest-httpbin/0.2.0/)。
- 本地派生镜像：`sha256:cfeca88ecbed6458741940fd81c5b88a9474029d09d93f475349c1427c35f58b`。原目标源码、测试与官方评分脚本不变，逐文件Git blob绑定仍核对。
- 显式开发题策略 `requests-isolated-timeout-v1` 只允许 `psf__requests-3362`。独立内部网络使用未分配目标地址产生真实ARP/连接超时，不修改目标地址、不mock异常；每轮固定容器IP并验证IPAM、owner标签，先清容器再清本轮网络。
- 环境适配与官方镜像原样运行分开标注。该策略不自动用于其他题，不能把本题协议推广成通用网络配置。

| 真实开发环境控制 | 官方F2P成功/失败 | 官方P2P成功/失败 | 官方判定 |
|---|---:|---:|---|
| 未修复基线 | 0/1 | 75/0 | unresolved |
| 上游标准补丁 | 1/0 | 75/0 | resolved |

两次独立资格运行结果一致，容器和网络清理均确认。官方规则按固定F2P/P2P名单判定，参见[官方评测说明](https://www.swebench.com/SWE-bench/guides/evaluation/)。完整原套件的标准补丁运行仍有3项失败和1项错误：旧测试使用的pytest字符串raises接口不兼容、缺少mocker插件，以及历史httpbin证书被当前SSL栈拒绝。它们不在本题官方验收名单内；原始失败完整保留，因此本轮只称“官方F2P/P2P资格通过”，不称原套件全部通过。相关环境问题继续作为工程事项处理。

首轮依赖安装成功后，回执检查命令存在引号错误，未生成镜像；修正执行脚本后在新目录重建。初次资格命令误用了不存在的历史文档路径，未启动harness；改为实际已有独立环境后使用新目录运行。旧失败目录和日志均保留，没有覆盖或改判。

**阶段2已取得的准备结果。** 只读取固定SWE-bench Lite parquet的instance_id、repo、base_commit三列，原数据SHA256核对一致；300个候选、12个仓库的顺序与筛选规则已经保存。已接触的requests题限定为开发用途。规则按仓库轮转、固定种子、同repo/base_commit保守聚簇，开发暴露不得进入最终集；资格失败、未检查与数量不足逐项保留。

当前候选池不是正式子集。其资格汇总初始为空，dev6/validation6/test30均保留缺口；没有把未检查题当合格题。真实任务的镜像资源、独立资格及最终清单仍待准备。真实性能线已开始考察上游问题及公开性能评测范式；例如[SWE-fficiency](https://github.com/swefficiency/swefficiency)提供真实仓库与慢工作负载，但给定工作负载的实验不能代替“从现象构造复现”的实验。本轮尚未下载其任务或镜像，也没有正式性能任务成绩。

**本轮验证与证据。** Docker/MCP/Redis完整回归573项：557通过、16项MySQL服务检查跳过、0失败，201.674秒。新增清单、账本、批次、Issue策略、SWE批次、网络策略和选择工具有专项检查；模型传输使用替身，不产生真实API请求。另有真实官方harness的两组环境资格运行，它们使用标准补丁，不是模型补丁。

| 证据 | 本地位置 |
|---|---|
| 完整工程回归 | `runtime/real-task-phase01-regression-20261110-first.log` |
| 依赖下载与来源哈希 | `runtime/requests-env-artifacts-20261110-verified/artifacts.json` |
| 派生环境构建 | `runtime/requests-dependency-build-20261110-verified/{receipt.json,build.log}` |
| 原始适配数据与说明 | `runtime/requests-adapted-dataset-20261110/` |
| 首轮官方资格通过 | `runtime/requests-adapted-qualification-20261110-v2/` |
| 第二轮官方资格通过 | `runtime/requests-adapted-qualification-20261110-v3/` |
| 候选池和资格缺口 | `runtime/swebench-candidate-pool-20261110/` |
| 安装包核对 | `runtime/real-task-phase01-wheel-check-20261110.json` |

机器可读汇总见[交付清单](/Users/gehanyang/编程/python-repair-workbench/docs/real-task-delivery-20261110.json)。哈希用于一致性核对，不是第三方签名；原始运行数据保存在本地runtime，不公开模型密钥或验收私有目录。

下一阶段先完善真实任务资格与复现协议，并继续处理requests套件的附加环境错误。真实Qwen-Max小批对照需使用新冻结源码、任务、模型配置、Token上限及额度状态；本轮的工程测试与标准补丁不能填入模型通过率、Token节省或自主诊断成果。
