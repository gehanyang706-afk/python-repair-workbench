# 本地MCP诊断接入

官方SDK连接、真实容器、本地安装包接入与证据审计完成；真实模型API调用为0。最新回归与打包结果以[机器可读交付清单](diagnostic-mcp-delivery-20261031.json)为准，其中保留源码/依赖指纹、安装包哈希及运行位置。

## 已实现范围

诊断工具增加可选的本地stdio MCP接入，使用官方Python SDK `mcp==2.3.0`。客户端先发现并核对工具目录，再调用现有诊断执行器。Agent的假设更新、证据登记、候选修改和独立评分仍归主应用控制。

当前MCP仅提供 `list_diagnostics`、`run_probe`、`read_report` 三个工具，覆盖CPU、Python分配和无profiler基准。AST导航仍使用主应用原有工具；未接入远程MCP、编辑器、任意Shell或源码修改工具。每个服务进程绑定一个可信任务，只执行一次诊断，之后可以读取同一份报告。

MCP不是新规划器，不会自动提高修复率。本地直连与MCP模式使用同一输入、执行器和评分规则，便于区分协议接入与Agent策略的影响。

## 调用链

```text
主Agent选择probe
 → DiagnosticSession扣除探索工具/探针额度
 → run_mcp_probe固定本机镜像身份，生成一次性任务配置
 → 官方Client启动本地stdio服务，发现与校验目录
 → MCPServer校验任务绑定/参数，执行原诊断器
 → Docker运行可信探针，保存原始结果并确认清理
 → MCP返回结构化回执，客户端读取同一report_id复核
 → 主应用核对归档文件、快照/镜像/工作负载/脚本身份与测量内容
 → 将有界摘要与evidence_ref返回Agent
```

完整报告保存在文件。协议的文本和结构化通道不会被整份重复回灌给模型，Agent仍接收原有摘要。

## 任务与执行约束

- 任务配置由应用生成，绑定不可变快照、输出目录、工作负载、镜像、超时及不透明报告ID。工具参数不接受任意路径、命令或替代配置；额外参数也会被拒绝。
- 服务与客户端校验目录、符号链接、内容身份和结果大小；模型不能借由报告ID读取任意主机文件。单份协议回执上限256 KiB，超过后明确拒绝，不截断后伪称完整。
- 镜像标签先解析为本机不可变ID，不下载镜像；服务仍使用原执行器的无网络、只读挂载、CPU/内存限制。
- stdio环境不继承模型密钥或追踪设置。服务本身没有模型调用；父Agent的模型消耗另行统计。
- 请求取消和期限触发后，父进程设置取消标记，服务等待执行器finally回收容器。父进程记录容器回执；断连后仅紧急清理本次已登记且名称合法的容器。
- 容器日志尾部不完整或回执无效时，清理状态保持无法确认；不猜测名称删除资源，也不把缺失回执当成功。
- 清理有独立宽限时间，可能晚于执行截止。根取消和预算停止原因不会被协议/日志异常覆盖。
- 这是可信本机应用服务，不提供父进程被强杀后的持久恢复，也不是恶意仓库的完整对抗验证器。

## 安装与使用

```bash
.venv/bin/python -m pip install -r requirements-agent.lock -r requirements-mcp.lock
.venv/bin/python -m pip check
```

MCP为可选依赖，原Agent锁不升级。新增依赖独立锁定，CI集成任务已包含MCP锁；核心快照/候选/验证不依赖MCP。

对已有基线直接诊断，不调用模型；输出目录必须全新：

```bash
.venv/bin/python -m repair_workbench probe --snapshot runtime/diagnostic-acceptance-20261031-frozen/baseline --output runtime/your-new-mcp-probe --image study-agent-deepagents:0.1 --kind cpu --transport mcp
```

`diagnose` 通过 `--diagnostic-transport mcp` 选择接入模式，本地直连仍为默认。真实模型入口继续默认关闭，本轮没有API授权；配置MCP与安装SDK不代表批准模型调用。

服务模块的 `python -m repair_workbench.diagnostic_mcp_server --config <可信配置>` 面向应用启动器，不向模型开放配置路径。

## 验证与证据

```bash
.venv/bin/python -m unittest discover -s tests -p test_diagnostic_mcp_server.py -v
.venv/bin/python -m unittest discover -s tests -p test_diagnostic_mcp_client.py -v
RUN_DOCKER_TESTS=1 REPAIR_TEST_IMAGE=study-agent-deepagents:0.1 .venv/bin/python -m unittest discover -s tests -p test_diagnostic_mcp_docker.py -v
```

覆盖官方SDK内存连接与真实stdio发现、严格Schema、任务绑定、结果一致性、取消/断连、容器清理、截断回执和测量篡改。真实容器还验证三类探针在两种接入方式下的语义/身份一致，以及脚本模型选择MCP探针后提交已知候选的完整链路。

这些是协议、执行器和已知候选验证，不是LLM自主修复成绩。首次原始日志为 `runtime/mcp-docker-20261031-first.log`，首次完整回归为 `runtime/diagnostic-mcp-subbudgets-20261031-regression-first.log`。旧 `measurement_noise_exceeded` 记录保持不变；本次没有改变性能评分或重跑覆盖旧成绩。

独立审计最初把 `probe-01-mcp` 控制目录算成额外探针，返回失败。现已对已登记MCP探针派生出的精确控制目录单独审计：绑定任务配置、报告ID、工具参数、容器挂载与回收回执，拒绝额外文件、嵌套探针和未登记目录。它不会执行仓库代码或跟随读取外部快照路径。原失败 `runtime/diagnostic-mcp-acceptance-064134d6dc7d/audit.json` 保留，修复后对同一档案的结果另存 `audit-after-mcp-fix.json`；后续产品图容器测试直接断言审计通过，避免仅看测试进程退出就漏掉审计失败。

子任务配额与主流程资源预留见[预算交接](diagnostic-subtask-budgets.md)。当前仍无Token/费用硬上限，不能把请求或时间配额说成完整费用治理。
