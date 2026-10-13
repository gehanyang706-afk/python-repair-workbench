# Agent 运行与权限边界

## 组件职责

- `settings.py`：显式读取环境变量或指定配置文件，创建 DashScope 模型客户端，不搜索其他项目、不复制密钥。
- `agent.py`：使用 `create_deep_agent` 组装模型、规划与工具循环；统一计算模型调用预算，禁用默认通用子 Agent。
- `model_transport.py` / `provider_worker.py`：管理内置模型请求的墙钟限制、取消、网络超时与进程回收；请求进程不是另一个 Agent。
- `tool_recovery.py`：按当前轮次和调用 ID 收集全部工具错误，提供安全反馈和有界恢复。
- `session.py`：管理批准范围、任务预算、候选次数、执行事件和结果记录。工具只能调用此应用层操作。
- `candidate.py` 与 `verification.py`：继续负责候选目录构建和独立验证，不使用模型的自评作为修复结果。

采用 Deep Agents 0.7.20。框架保留上下文管理和规划；`StateBackend` 只存内存虚拟文件。框架文件工具只开放 `read_file`，用于读取框架生成的上下文材料，没有本机文件系统 backend 或 Shell。业务文件通过 `read_source` 读取固定快照。

`HarnessProfile` 以本项目模型包装器的专用名称注册，关闭默认 general-purpose 子 Agent。该接口为框架 beta API，因此依赖固定版本，并以工具可见性和伪造 Shell 调用测试验证边界。

## 一次修复如何执行

1. 调用方提供已复现断言失败的基线和批准文件清单。
2. 应用检查快照内容标识、失败报告、容器清理状态与任务输出位置。
3. Agent 调用 `list_sources`、`read_source`、`read_test_log` 获取证据。
4. Agent 优先调用 `submit_edits(edits, diagnosis)` 提交版本绑定的局部修改；`submit_candidate(changes, diagnosis)` 仍接受完整替换内容。两者共享语法预检、重复识别与候选预算，随后进入原有 Docker 验证。
5. 应用新建 `attempt-NN/candidate`，重新构建 `attempt-NN/verification`，在基线镜像 ID 下运行原始测试。
6. 不通过时 Agent 可查阅该轮日志并提交下一候选；通过后禁止继续提交修改。
7. 最终 `result.json` 由应用保存，模型文本单独保存为 `model_response.txt`。

所有候选都相对于原始基线；第二次提交不能假设第一次修改已经写入基线。当前读取接口也始终读取基线，Agent 必须在新候选中保留仍需要的修改。

## 预算与记录

默认最多 3 次候选、12 次模型调用、60 次业务工具调用，总期限 180 秒，单次容器测试 30 秒。非法候选也消耗候选次数。模型绑定工具和框架摘要共享同一计数器，失败模型请求也计数。

内置 DashScope 请求分别限制连接 5 秒、读取等待 30 秒、完整调用墙钟 30 秒。应用在等待期间检查取消和 Session 截止时间，终止并确认回收本地请求进程后返回；清理可能额外耗时。Session 期限只覆盖 Agent 修复阶段，不包含此前快照、失败复现和此后独立评测。容器操作仍在操作边界协作取消，最多等待本次测试上限和清理。CLI Ctrl+C 也保存停止状态并回收请求；崩溃后的续跑尚未实现。没有费用硬上限；本地终止不保证供应商停止远端推理，缺失用量报告不代表免费。

`events.jsonl` 保存工具开始、模型调用及尝试结果；`result.json` 通过临时文件替换写入，避免普通写入过程中暴露半份 JSON。事件记录不包含密钥或原始供应商异常文本，第三方 tracing 默认关闭。上述机制不是数据库事务或崩溃恢复系统。

`state` 表示 Agent 是否正常结束，`status` 表示补丁验证结论。例如已存在通过候选、随后模型请求失败时，可以同时出现 `state=stopped` 与 `status=passed`；必须分别解释。模型自行声称成功但没有通过候选时为 `inconclusive/no_candidate`。

## 安装与使用

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements-agent.lock
.venv/bin/python -m repair_workbench repair \
  --baseline /path/to/run/baseline \
  --output /path/to/run/agent \
  --allow src/pricing.py \
  --task '修复原始测试暴露的边界问题，保持其他行为不变' \
  --env-file /path/to/private/.env
```

配置文件使用 `DASHSCOPE_API_KEY` 和可选 `REPAIR_MODEL`；兼容旧配置中的 `STUDY_MODEL`，默认 `qwen-plus`。进程环境中的密钥优先于文件，模型优先级为 `--model`、环境 `REPAIR_MODEL`、文件 `REPAIR_MODEL`、文件 `STUDY_MODEL`、默认值。配置文件不会作为 Agent 输入。

此命令会向 DashScope 发送任务和工具读到的源代码/日志，并产生模型调用费用。当前不自动将补丁应用到原仓库，最终补丁位置在任务结果的 `patch` 字段中。

## 尚未覆盖

当前验证场景是受控小型仓库。没有证明大型真实仓库的修复成功率、恶意代码抵抗、多任务并发、部署能力或复杂依赖兼容性。测试报告仍来自被测进程，不能抵抗主动伪造。Web 界面与 SQLite 任务队列已实现，见本机工作台文档；持久化断点续跑尚未实现。

## 修复可靠性 v2

局部修改字段、失败反馈、模型结束原因及前后计数差异见 [可靠性优化记录](reliability-v2.md)。预检只位于 Agent 的 Session 提交链；独立 candidate/verify 命令仍通过原有 Docker 验证。

## 当前需求与上下文机制

正式命令行与本机服务默认使用 `v2`：保留局部提交、权限、语法和 Docker 验证链。`--profile full` 可显式启用 v3 的源码读取记录、符号定位、需求/约束登记及范围审查，属于实验配置。原因与真实模型结果见 [v3 评测](evaluation-v3-20261012.md)；v2 的历史表现见 [v2 评测](evaluation-v2-20261011.md)。当前环境的确定性网页全链路验收验证了 v2 工程调用链，但不等于新的 qwen-plus 成功率评测。

简化接口实验提供 `simple_edit`、`simple_context`、`simple_review`、`simple_full`，分别控制失败驱动上下文与非阻断风险审查；不要求 v3 需求表单。新请求生命周期适用于全部内置模型配置，严格墙钟限制改变了过去长响应的停止行为，历史成绩不能直接归因比较。实现、计数差异及离线验收见[本轮工程记录](reliability-context-20261013.md)。
