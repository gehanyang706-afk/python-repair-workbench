---
name: python-allocation
description: 候选诊断策略：当任务关注 Python 分配峰值，或已有证据提示分配可能影响计算时使用；帮助解释 tracemalloc 范围与性能取舍，尚未验证真实泛化效果。
compatibility: Registered memory and unprofiled benchmark probes
metadata:
  version: "0.1.0-candidate"
  validation: "offline-wiring-only"
allowed-tools: read_source source_outline read_symbol search_source run_probe read_evidence record_hypothesis
---

# Python 分配诊断

本策略是应用维护的只读候选，不允许 Agent 改写或自动晋升。按问题选择需要的检查，不要求为了使用策略而重复测量。

## 适用条件

任务要求控制 Python 分配，或已有 CPU、源码或内存证据提示分配值得调查。先回查相同版本的观察，明确是否真的需要新实验。仅凭存在容器、列表或缓存，不能判断分配是主要问题。

## 工具步骤

1. 记录简短假设及实际证据引用，说明要区分“分配过多”还是其他解释。
2. 需要测量时选择 run_probe(memory)，阅读 measurement_scope。追踪峰值只覆盖 Python 可追踪分配，不等于进程常驻内存、原生堆或显存；输入准备是否被计入必须按回执解释。
3. 用 read_symbol 或短源码片段检查对象的存活范围与必要行为。需要判断耗时影响时，再选择独立 benchmark；不要把更少分配直接写成更快。
4. 用同一工作负载、同一环境与候选版本的回执比较取舍。无法用当前指标回答问题时标记证据不足，不能擅自添加工具、执行命令或扩大测量范围。

## 反例

峰值降低可能只是改变了测量范围；缓存可能减少计算却增加存活内存；分配次数减少也可能增加其他计算。不能只挑一项变好的数字而忽略行为与其他资源回退。

## 复验与限制

所有修改仍须通过原测试、注册语义检查、无 profiler 耗时及内存约束。保留不利结果和停止原因。当前候选没有证明跨项目收益，Python traced peak 也不能代表真实场景的全部内存或游戏品质。
