---
name: cpu-hotspot
description: 候选诊断策略：当任务涉及计算耗时，且需要用函数耗时与调用次数区分可疑热点时使用；不预设根因或优化方式，尚未验证真实泛化效果。
compatibility: Registered diagnostic tools and read-only source access
metadata:
  version: "0.1.0-candidate"
  validation: "offline-wiring-only"
allowed-tools: read_source source_outline read_symbol search_source run_probe read_evidence record_hypothesis
---

# CPU 热点诊断

本策略由应用维护，只读加载。不得修改、生成新版本或自动晋升为已验证经验。它提供可选择的检查步骤，不是必须执行的工具顺序。

## 适用条件

任务明确关注计算耗时，或已有测量提示某段计算值得调查。先确认当前证据和目标，已有同一版本的证据时优先回查。若主要现象是等待、外部服务或输入输出延迟，CPU 报告可能不能解释瓶颈。

## 工具步骤

1. 用源码片段与已有回执建立一个可被否定的短假设，并记录对应 evidence_ref；不要把函数名或代码外观当作已确认根因。
2. 需要区分计算分布时选择 run_probe(cpu)。区分累计耗时与自身耗时，结合调用次数查看相关符号；被频繁调用不等于消耗最多。
3. 根据观察选择下一步：回读少量相关源码、调查分配，或运行无 profiler 的 benchmark。每一步应回答当前仍不确定的问题；缓存命中不是一次新实验。
4. 新证据不支持原解释时更新或否定假设，再选择其他检查。可以保持未决，不补造因果关系。

## 反例

函数累计耗时高，可能因为调用了更慢的子函数；自身耗时很低的高调用次数函数，也可能没有优化价值。一次 profiler 数字降低，不能证明实际端到端更快。

## 复验与限制

候选应通过应用独立重建的原测试、注册语义检查、无 profiler 耗时与内存复验。CPU profiling 用于定位，不作为最终速度成绩。只报告当前场景和环境的观察；不要宣称适用于任意仓库、原生扩展、并行程序或真实游戏渲染。
