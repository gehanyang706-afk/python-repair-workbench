# 冻结默认配置后的新增验证任务

这两道上游历史问题在保守固定 `v2` 后准备，未用于调整 Agent Prompt、工具或策略；目前仅完成资格验证，没有 Agent 成绩。“未参与调优”不表示能排除模型预训练包含上游历史代码。

| 任务 | 类型 | 输入范围 | 许可证 |
| --- | --- | --- | --- |
| `cache-replace-growth-repository` | 仓库级 | cachetools 父提交归档全部 43 个文件，原有 291 项测试原样保留，追加公开复现及独立测试 | MIT |
| `timestamp-future-age-module` | 模块级 | itsdangerous 源码包与许可证，共 10 个文件；使用专用公开及独立测试，不包含完整上游测试集 | BSD-3-Clause |

来源、完整父/修复提交、归档 SHA-256、逐文件哈希和批准范围见 `manifest.json`。cachetools 对应[上游修复](https://github.com/tkem/cachetools/commit/39b31bc9b63abe98497409945e9d382d8918c8fb)，itsdangerous 对应[上游修复](https://github.com/pallets/itsdangerous/commit/c30678d19e37011890e2374cca04f7789e101793)。

资格验证只使用原始父提交源码与原测试，并在相同镜像下应用参考源码修复。`visible` 是追加的公开复现，`holdout` 是独立验收，`oracle` 仅用于资格验证；后两者不进入 Agent 基线。仓库级源文件与上游父提交归档逐字节一致，未为了通过测试改原源码或原测试。

评测器为该原版树及追加测试创建规范化输入 Git 提交；它与原始上游父提交 SHA 不同，二者分别记录。仓库级表示保留完整上游树与原测试，不把这种规范化提交伪称为原始上游 Git 对象。

两项目运行依赖均为标准库；测试依赖来自工作台参考镜像的 pytest 锁定文件。额外固定 `PYTHONPATH=/workspace/src`，从本次挂载快照导入源码，避免镜像中预装目标包遮盖候选。模型运行无网络，镜像不内置任务源码或官方补丁。

```bash
docker build -f Dockerfile.tests -t repair-workbench-test:reference .
docker build -f benchmarks/delivery-heldout-v1/Dockerfile \
  -t repair-workbench-heldout:reference benchmarks/delivery-heldout-v1
.venv/bin/python scripts/qualify_delivery_cases.py \
  --image repair-workbench-heldout:reference \
  --output /path/to/new-qualification-record
```

命令不创建模型客户端，模型调用为 0。正式付费验证必须单独批准。结果按仓库级、模块级分别报告，不能把参考修复资格通过写成 Agent 成功。

资格阶段发现 RRCache 默认随机驱逐使原始失败项波动；新增独立测试改为注入固定 `choice`，保留首次资格记录并重新核验。没有运行 Agent 或据此调整修复策略。
