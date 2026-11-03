# BugsInPy 衍生公开开发任务

该小规模任务集选取 BugsInPy 的 `tqdm-1`、`tqdm-4`。它验证本平台的真实模型评测链路与上下文优化，不代表官方 BugsInPy 全量成绩。

- 元数据固定于 BugsInPy `11c5f1eea954a42132cfd06bf257766a7963e0fd`。
- 源码固定于官方缺陷提交，许可保存在各任务 `source/LICENCE`；每个源文件的 Git blob SHA 及 SHA256 均有记录。源码没有为适配 Python 而修改。
- `visible/` 保留上游触发测试的原始断言，将 nose 包装改为 pytest，并关闭后台监控线程。镜像、Python 与官方 Python 3.6.9 不同。
- `holdout/` 是本项目在模型调用前冻结的边界与保留行为测试；不是官方隐藏测试。它和上游修复 `oracle/` 均不进入 Agent 快照。
- 只接受可复现失败、上游修复在可见和独立测试均通过、容器已清理的任务。
- 两题都是公开历史缺陷，不能保证模型训练数据未见过；三次重复仍然只有两个独立缺陷。

来源：[BugsInPy](https://github.com/soarsmu/BugsInPy)、[tqdm](https://github.com/tqdm/tqdm)。具体提交、触发测试适配和文件哈希见 `manifest.json`。

资格检查：

```sh
.venv/bin/python -m repair_workbench.evaluation validate \
  --dataset benchmarks/bugsinpy-derived-v1 \
  --output runtime/NEW-QUALIFICATION \
  --image repair-workbench-test:20261014-pinned
```

每次必须使用新目录，镜像须已在本机。真实评测用独立入口 `repair_workbench.public_live_evaluation`，不复用旧评测器的固定价格估算与汇总缺省值。
