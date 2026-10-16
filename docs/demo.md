# 最短演示与复现路径

使用本轮交付源码包进入项目根目录。稳定标签 `v1.0.0` 和此前公开 GitHub 提交不包含本轮新 Dockerfile/CI；本轮仅准备本地交付，不发布新版本。

## 安装并构建测试镜像

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements-agent.lock -r requirements-build.lock
.venv/bin/python -m pip install --no-deps --no-build-isolation .
.venv/bin/python -m pip check
docker build -f Dockerfile.tests -t repair-workbench-test:reference .
.venv/bin/python scripts/check_delivery.py
```

参考镜像固定 Python 基础镜像摘要及 pytest 依赖，仅支持工作台的纯 Python 测试夹具。构建需要联网，实际目标测试运行无网络；不承诺兼容任意仓库。

## 准备并提交任务

复制已有测试夹具到一个全新的仓库外目录；下面的示例路径必须尚不存在，不覆盖原仓库：

```bash
.venv/bin/python scripts/prepare_demo.py --output /tmp/repair-workbench-demo/repository
.venv/bin/python -m repair_workbench serve \
  --repo-root /tmp/repair-workbench-demo/repository \
  --data-dir /tmp/repair-workbench-demo/data \
  --image repair-workbench-test:reference \
  --profile v2 \
  --env-file /path/to/private-model.env
```

模型文件放在仓库外，包含 `DASHSCOPE_API_KEY` 和 `REPAIR_MODEL=qwen-plus`。启动服务和打开页面本身不调用模型；提交失败任务会产生费用，按事先批准的额度执行。当前交付保守固定 `v2`；四配置矩阵未启动，不把沿用默认解释为测得最优。演示记录实际选择，不自动切换至功能最多的配置。

1. 打开 `http://127.0.0.1:8765`，选择新仓库、修订 `HEAD`，批准 `pricing.py`。
2. 填写：“金额达到 100 时应享受折扣；保持其他金额和非法输入的既有行为。”最多 3 个候选、12 次模型调用，页面默认 180 秒修复期限、30 秒测试上限。
3. 提交并观察固定提交、失败复现、Agent 工具、候选和验证事件。
4. 候选通过原测试后，查看 diff 和测试前后对比，审核并下载补丁及报告。该结论只覆盖原测试范围。
5. 没有候选或验证失败时，展示真实停止原因和日志，不宣称任务成功、不手工替换模型候选。

仓库中的故障是确定性演示夹具，不属于真实上游成绩。原始仓库不被修改，补丁需要开发者另行审核应用。退出服务使用 Ctrl+C。

## 无密钥复现链路验收

```bash
RUN_DOCKER_TESTS=1 REPAIR_TEST_IMAGE=repair-workbench-test:reference \
  .venv/bin/python -m unittest discover -s tests -p test_web.py -v
```

这项测试通过 HTTP 提交、SQLite 队列、固定快照、真实 Docker 失败与候选验证，下载补丁和报告；模型为确定性测试替身，代码修改已知。它证明业务链路可运行，不证明真实模型修复能力。参考真实模型案例见[公开案例](showcase.md)。
