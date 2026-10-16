# 历史模型补丁的无密钥回放

这两份补丁来自 2026-10-12 固定 `7303817` 的真实 qwen-plus 运行，逐字节复制并在 `manifest.json` 记录 SHA-256，没有人工补齐。来源为[原始脱敏评测](../evaluation-simple-edit-20261012.json)；相关上游许可证分别位于 boltons 与 more-itertools 对应任务的 `source/LICENSE`。

```bash
python3.13 scripts/replay_recorded_cases.py \
  --image repair-workbench-test:reference \
  --output /path/to/new-recorded-case-replay
```

镜像按项目根目录 `Dockerfile.tests` 预先构建，输出目录必须全新。回放只读取固定输入和已记录补丁，使用临时副本应用 diff，再通过现有候选/独立重建验证 API 执行公开与独立测试。原仓库、基线和测试不修改；不启动 Agent、不读取密钥、不产生模型调用。

预期观察：bucket validator 补丁公开与独立均通过；Bits 空转换补丁公开通过、独立失败。退出码 0 表示与这两条历史记录一致，不表示所有补丁都修好。它不是新模型推理或未见任务成绩，不能证明当前 30 秒客户端仍能生成相同补丁。

2026-10-16 已在无 Agent 依赖的核心虚拟环境中实际回放，两种结果均复现；镜像和脱敏结果见[回放证据](replay-20261016.json)。输出中各 `reports/result.json` 保存实际镜像与清理状态，完整运行目录仍只在本机保留。
