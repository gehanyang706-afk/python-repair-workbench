# 第一批后端可靠性交付

2026-10-20。基于历史提交 f980087 继续开发，包版本保留 1.1.0.dev0，Agent 默认仍为 v2。新增 MySQL/独立 Worker 是显式后端入口，SQLite 是兼容默认，不对历史数据库原地改造。仅本地提交，不推送、不发布、不真实调用模型。

## 完成内容与主链路

MySQL 初始化迁移与九张职责明确的表；事务领取、租约/心跳/代次校验；跨代预算与调用回执；幂等键与请求摘要；快照、失败基线、候选及验证阶段恢复；原始执行器/请求/容器取消链复用；HTTP 回执查询、SSE 游标和补丁下载兼容。

用户提交 → API 校验/幂等裁决/固定提交 → MySQL 持久任务 → 独立 Worker 短事务领取 → 续约 → 阶段校验/复用 → 单 Agent + 预算回执 → 受控局部编辑 → 候选确认 → Docker 原测试独立重建 → 验证记录和终态 → 页面审核下载。

权限、版本、原文唯一匹配、原子编辑、语法预检、重复检测、Docker 隔离及原评分未取消。没有 Redis、MCP、多 Agent、向量记忆或新的修复算法。

## 实际验收

统一执行：

```bash
RUN_MYSQL_TESTS=1 RUN_DOCKER_TESTS=1 \
REPAIR_TEST_IMAGE=repair-workbench-test:20261014-pinned \
.venv/bin/python -m unittest discover -s tests -v
```

结果：168 项全部通过，0 跳过，147.536 秒。上一轮完整回归也通过（146.883 秒），两轮记录均保留。包含原有 152 项与新增 16 项真实 MySQL 专项。原始日志 runtime/backend-release-regression.log，完整故障文件 runtime/backend-acceptance-72843b79d0；可公开、可重算的脱敏摘要为 [验收 JSON](backend-acceptance-20261020.json)。真实模型调用 0，测试模型的 token 字段是合成观测数据，不是供应商消耗。

| 场景 | 实际故障/观察与结论 |
| --- | --- |
| 并发幂等 | 4 个独立进程同时创建，相同键只得到一个 task；异内容冲突；HEAD 前进后重试保持旧 SHA |
| 并发领取 | 4 个独立进程抢一个任务，只有一个合法领取和一次代次 |
| 自然租约过期 | SIGSTOP 停住旧 Worker，包括其心跳；等待数据库时间到期，再由新进程接管；SIGCONT 恢复后 9 类旧写入被拒绝 |
| 活跃续约 | 在超过原租约时长后尝试接管，仍无法领取；心跳时间实际前进 |
| 候选后崩溃 | 已登记候选后同步屏障，SIGKILL Worker；新代复用失败基线和同一候选验证，无模型回放，旧候选字节不变 |
| 快照后崩溃 | 已确认快照后 SIGKILL；新代校验并复制快照，执行剩余阶段 |
| 候选篡改 | 崩溃后改测试专用候选；新代校验拒绝，不调用模型掩盖不一致 |
| 请求未知 | 实际模型替身进入阻塞后杀父进程；回执 started→unknown，预算保留，接管进程不再调用模型 |
| 请求子进程孤立 | 真实请求子进程超出父进程生命期；新代核对 PID/随机标记/进程组后终止，仍保留未知用量与中断状态 |
| 未发起预留 | reserved 阶段被杀后接管，保留 abandoned 占用；总上限 3 只剩 2 次实际调用，没有重置 |
| 缺失用量/截止 | 无完整用量即停止；数据库预留事务直接拒绝到期预算 |
| 容器中取消 | 确认 Docker 容器实际运行后取消，工作进程退出、容器移除、清理回执保存 |
| HTTP 重启 | 实际服务进程停止再启动，任务、三次模型回执、事件与补丁仍可查询 |

开发中失败记录保留：首次 Docker 启动因 bind 目录不存在失败；一次默认沙箱回归因 socket bind 权限失败，授权环境重跑；数据库 deadline 预留缺口及随后局部导入遮蔽异常由失败测试定位并修复；失败测试未隔离选题造成后续领取到遗留取消任务，测试 Worker 现在显式限定目标任务，同时保留公共队列竞争测试。没有手改 owner/lease 或改评分掩盖失败。

其他检查：pip check 通过；新增代码 F 类静态检查通过；SQL 迁移包及模块/静态资源 26 个文件字节校验通过；四组历史数据集 Hash 校验通过；Compose 配置校验通过；验收后没有 repair-run 容器残留。核心包构建使用本机已有工具环境的 setuptools 82.0.1，不安装新构建工具；当前开发 venv 缺少 setuptools 的首次失败记录保留，因此不声称按旧 build lock 在全新环境完整重建。本批 wheel 为本地检查产物，不发布。

## 默认、兼容与启动

- 新后端：serve --backend mysql；独立 worker 进程，可启动多个。详见 [启动及事务设计](backend-reliability.md)。
- 原 serve 默认 SQLite；原 CLI、单 Agent 与 Web/Docker 回归已覆盖。MySQL 不是自动替换旧 SQLite 文件。
- API 和 Worker 的 data-dir 必须相同且位于批准目标仓库之外；同机共享产物，不是跨主机服务。
- 初始化 `init-db --db-env runtime/mysql-backend/client.env`；本机专用 MySQL 名称 repair-workbench-mysql、端口 127.0.0.1:33079、1GB/2CPU 上限，数据位于忽略 Git 的 runtime/mysql-backend/data。
- Worker 不传模型 env-file 时不创建付费模型客户端。真实调用仍需另行授权，本批没有运行。
- 旧数据继续通过原服务查询；新任务使用新 MySQL data-dir。回退停止新 Worker 后使用旧入口与旧数据，不双写或反向迁移活动任务。

## 主要文件

- mysql_store.py / migrations/001_mysql.sql：数据库权威、事务、所有权和回执。
- durable_worker.py：独立 Worker、续约、代次隔离与安全恢复。
- mysql_service.py：API 侧任务创建及产物查询，不启动执行器。
- session.py / agent.py：可选持久预算、调用/候选回执，原算法不变。
- model_transport.py / runner.py / verification.py：请求身份、受控取消与 Docker 恢复资源追踪。
- api.py / __main__.py / static/app.js：明确后端入口、幂等头、回执接口和浏览器键保留。
- tests/test_mysql_backend.py / backend_worker_probe.py：真实故障与脚本模型验收。
- compose.mysql.yml / prepare_mysql.py / requirements-backend.lock：隔离数据库与私有配置。
- audit_backend_acceptance.py：从实际验收目录与 MySQL 回执导出脱敏证据。

## 未完成与边界

没有任意 Agent 图节点续跑、全局 exactly-once、跨主机执行、数据库 HA、完整 SQLite 导入、磁盘配额与垃圾清理、全局模型并发限制或吞吐基准。已发起请求的远端状态不确定时不自动重试。已有模型响应但无安全候选时明确中断，不重置预算重推理。该取舍优先避免重复费用和错误恢复，牺牲自动完成率。

无密钥 CI 增加 MySQL job，仅配置准备并本地等价验证，未推送、未取得远端结果。当前测试主机 macOS；Linux/Windows 与真实供应商行为没有新增验收结论。故障验证通过不表示模型修复能力提高。

第二批仅给建议：先测 1/2/4 Worker 的 HTTP/队列/执行时长和资源，再决定是否引入 Redis AST 缓存和事件唤醒；业务事件仍以 MySQL 补读为准。本阶段到此停止，不自动实施第二批。
