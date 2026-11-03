---
name: behavior-preservation
description: 候选诊断策略：准备性能修改或解释验证失败时，用用户要求、可见源码和公开测试检查必须保留的行为；引用存在不等于语义正确，尚未验证真实泛化效果。
compatibility: Approved source edits and application-owned independent verification
metadata:
  version: "0.1.0-candidate"
  validation: "offline-wiring-only"
allowed-tools: read_source source_outline read_symbol search_source read_test_log read_evidence record_hypothesis submit_ref_edits
---

# 修改前后的行为保持

这是应用维护的只读候选策略，不能被 Agent 改写、自动晋升或作为修改权限。需要时按需读取，不增加复杂的必填计划表单。

## 适用条件

准备修改已批准的源码，或候选虽有性能变化但语义验证失败。依据用户任务、可见源码与公开测试区分需要改变的表现和必须保留的行为。描述不清时保留不确定性，不借用不可见答案。

## 工具步骤

1. 回查实际原文，简短列明相关输入范围、输出关系、顺序或身份、异常条件与副作用。只记录当前任务有依据的约束，不推断不存在的规范。
2. 检查相关入口、边界和异常路径。可以使用 source_outline、search_source、read_symbol 与短片段；证据引用证明材料来自哪里，不证明假设或修改正确。
3. 如需修改，用当前会话返回的 source_ref 提交精确原文替换。文件、版本、批准范围和冲突由应用验证；不得改测试、评分、基准或配置来让结果变好。
4. 依据提交工具的独立回执决定继续、停止或说明限制。拒绝、超时、缺报告与语义失败分别记录，不把测量失败解释成新的性能根因。

## 反例

缩小实际处理范围、改变异常条件、丢弃输出或改变副作用，可能让程序更快却违反原要求。仅通过一条可见断言，也不能证明所有关联行为都保持。

## 复验与限制

复验由可信应用执行，模型不能宣布独立成功。原测试和注册工作负载通过只覆盖该范围；未修改、证据不足或模型自评都不构成优化成功。策略仍处于候选阶段，未证明能稳定减少语义回归或提高真实模型效果。
