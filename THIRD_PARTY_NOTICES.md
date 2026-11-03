# 第三方来源与许可

第三方源码和测试材料遵循其各自随附的许可。

## tqdm / BugsInPy 衍生开发任务

benchmarks/bugsinpy-derived-v1包含tqdm固定历史提交的源码、上游修复文件，以及从公开触发断言适配的测试。作者、MIT/MPL说明及许可文本保存在：

- [tqdm-1随附许可](benchmarks/bugsinpy-derived-v1/tqdm-1/source/LICENCE)
- [tqdm-4随附许可](benchmarks/bugsinpy-derived-v1/tqdm-4/source/LICENCE)

上述说明同时适用于对应任务oracle目录中的tqdm文件。固定提交、文件Git blob、适配方式见[任务清单](benchmarks/bugsinpy-derived-v1/manifest.json)。额外边界测试为本项目提供；任务集为衍生开发集，不代表官方BugsInPy成绩。

来源：[tqdm](https://github.com/tqdm/tqdm)、[BugsInPy](https://github.com/soarsmu/BugsInPy)。

## SWE-bench

本项目通过独立依赖环境调用固定版本的官方评测器，记录官方源码哈希；评测器源码、任务标准补丁和大型镜像未复制进本仓库。安装官方包后其许可由官方发行物提供。

来源与执行边界见[SWE-bench适配](docs/swebench-adapter-20261103.md)、[官方仓库](https://github.com/SWE-bench/SWE-bench)。

## 其他历史缺陷样例

其他benchmarks目录中的历史缺陷材料按各任务README及manifest记录上游出处、固定提交与许可说明；原作者的权利与许可要求继续适用。
