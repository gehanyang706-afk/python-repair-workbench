# 诊断主图只读并发与Redis共享速率

本轮将并发从调度器契约测试推进到真实Deep Agents工具节点，并为多个独立任务进程提供可选Redis共享准入。没有调用真实模型API；并发图验证使用脚本模型，不能作为模型诊断质量或Token收益。

## 主图中的真实并发

诊断会话新增`tool_concurrency=1..4`，CLI对应`--tool-concurrency`。该值传给LangGraph原生`max_concurrency`，不是另建模拟执行器。默认仍为1；显式设为2后，同一轮中的独立只读工具可同时执行。

允许并发的工具为源码/符号/搜索/文件列表、日志/证据/报告读取，以及只读虚拟文件工具。源码引用、读取统计与证据编号登记在短临界区内保护；源码读取的登记阶段不会并发修改共享字典。证据查询的协议通信可并发，重复同版本页面进入同一任务的single-flight。

探针测量、假设更新、待办、委派、补丁提交及结论必须每轮单独调用。同轮包含多个工具且任意一个不是只读工具时，整个批次在执行前拒绝；拒绝仍计工具预算。这样无需依靠线程顺序决定“先修改还是先读取”，并避免并行测量互相干扰。模型仍决定下一步调查动作，应用控制可并发的范围。

这不是多个规划Agent同时修改代码；Investigator仍保持只读、上下文隔离和原有共享预算。原RepairSession与SWE-bench入口默认串行，不自动启用新策略。

## 可选Redis后端

安装`requirements-redis.lock`，固定`redis==6.4.0`。本机验证使用官方`redis:7.4.2`镜像，清单摘要为`sha256:fbdbaea47b9ae4ecc2082ecdb4e1cea81e32176ffb1dcf643d422ad07427e5d9`。执行器和测试不会自动拉取镜像；镜像准备是独立显式步骤。

操作方指定私有JSON配置，例如：

```json
{"url":"redis://127.0.0.1:6379/0","namespace":"diagnostic-tools-v1","qps":20}
```

诊断CLI用`--tool-rate-config /absolute/path/redis-policy.json`接入，或在Python中传入`tool_rate_config=Path(...)`。该参数本身不会授权模型调用；真实批次仍须冻结任务、源码、模型与预算。配置由应用或操作方选择，模型不能指定Redis地址、凭据、命名空间或限额。含凭据配置应保存在仓库外或被忽略的私有目录并设为600权限；结果与日志只保存后端类型、命名空间与策略，不记录连接URL。

多个任务连接同一个Redis实例、同一个命名空间时，所有经过ToolRuntime的端点共享一个准入节奏。Lua脚本在服务端原子执行，以Redis TIME作为共同时间基准；不以各进程本机时间拼接窗口。速率范围0.01—1000次/秒，采用无突发的间隔准入，实际间隔向上取整到微秒。同命名空间的间隔策略不一致会拒绝，不让各Worker悄悄覆盖策略。

Redis只控制实际执行前的共享速率；缓存命中和并发合并的等待者不额外占用远端准入。各逻辑调用仍先计应用工具额度。Redis异常、鉴权失败或超时统一使任务以`shared_rate_backend_unavailable`停止，策略冲突以`shared_rate_policy_conflict`停止；无自动本地降级，也不重试未知结果的业务动作。

## 退出、过期与部署边界

该后端是速率准入，不是分布式执行信号量，因此没有需要持续续租的执行槽。进程拿到准入后退出，最多浪费一次准入，不会永久占住并发名额。限流状态在闲置至少120秒后过期，低QPS下TTL至少覆盖两个准入间隔；这是清理状态，不是取消正在执行的工具。

以下范围保持明确：

- 并发数、缓存和请求合并仍在任务/进程内；没有声称Redis分布式缓存、分布式single-flight或全局执行槽已实现。
- Redis限制业务操作准入，不逐条限制底层MCP发现/校验消息，也不覆盖绕过本应用调度器的外部宿主调用。
- 诊断任务仍未接入MySQL持久Worker恢复。本轮验证多个独立Python任务进程共享调度后端，不冒充多Worker诊断生产部署。
- Redis不可用时停止新准入；已经发出的业务操作不会因此被撤销。Redis重启、清空或驱逐限流状态会影响约束连续性，部署应使用受控实例、`noeviction`、访问控制及合适的持久化/高可用策略。本轮没有验证故障转移或跨机网络。

## 验证方法

原生图测试验证两个只读调用在不同线程相遇，若仍串行会在屏障处失败；65次读取提议保持60次共享工具上限，源码引用和证据不丢失；读写或委派混合批次不执行。相同报告页面由真实图并发发起，实际stdio MCP只调用一次，另一请求合并等待。

Redis测试启动独立、仅绑定127.0.0.1的临时容器，以`redis`用户运行并保留cap-drop/no-new-privileges。3个spawn Python进程跨本地/MCP端点争用20次/秒的同一限额，记录12次服务端准入时间并检查相邻间隔至少50000微秒。此外检查策略冲突、不可用时不执行工具、进程异常退出及等待取消。测试只删除自己创建的容器，不操作外部Redis数据库。

复验命令（使用已存在的本机镜像）：

```bash
.venv/bin/python -m unittest discover -s tests -p test_diagnostic_concurrency.py -v
RUN_REDIS_TESTS=1 .venv/bin/python -m unittest discover -s tests -p test_redis_rate_limit.py -v
```

首次容器因入口脚本在cap-drop环境切换用户失败，原日志保留。修复为明确使用非root的`redis`用户运行，没有放宽容器权限。真实测试、完整回归与交付指纹分别记录；多进程准入结果是限流正确性证据，不是修复质量或吞吐收益。

原理参考：[Redis Lua EVAL](https://redis.io/docs/latest/commands/eval/)、[redis-py](https://redis.io/docs/latest/develop/clients/redis-py/)。

最终完整回归485项：469通过、16项MySQL检查跳过、0失败；Docker、Redis与MCP均启用。包含真实Redis容器pause/unpause故障注入，验证不可用时不执行工具，恢复后显式调用可成功；初始化预检失败会在模型调用前记录停止状态。安装包51项产品文件与源码一致。机器可读结果、准入间隔、容器清理及源码指纹见[交付记录](parallel-redis-delivery-20261106.json)。
