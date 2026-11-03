# SWE-bench 适配交接（2026-11-03）

已接通任务导入、Issue-only Agent工作区、现有源码导航与受控编辑、标准预测导出，以及独立环境中的官方执行与评分。合成夹具已验证负例未解决、正例解决。首个真实开发任务已执行官方复验，但标准补丁仍有两项官方回归指标不通过，环境资格不成立。没有运行真实模型，没有SWE-bench模型成绩。

## 实现与边界

| 环节 | 行为 |
|---|---|
| 导入 | 读取官方JSON/JSONL导出，固定数据revision，拒绝重复ID、字段缺失与篡改 |
| 隔离 | Agent只接收instance_id、repo、base_commit、problem_statement；不传hints、标准补丁、测试补丁、F2P/P2P名单或验收脚本 |
| 源码 | 固定基线提交，不读取用户未提交内容；Git传输失败可用归档，但必须验证整个上游Git tree |
| 范围 | 从基线文件树和操作方源码根目录推导，不能根据标准补丁挑文件；当前仅修改现有Python源文件 |
| Agent | SWEbenchSession复用Deep Agents、搜索、AST、source_ref编辑与预算；不要求失败基线，不开放官方测试日志工具 |
| 提交 | 权限、版本、语法检查后独立重建；返回submitted而非passed；第一次有效提交即停止，不基于官方反馈重试 |
| 导出 | 从实际文件变化重新生成Git diff，输出官方三字段JSONL，不信任模型保存的patch.diff |
| 评分 | 固定原版run_instance/get_eval_report；缺报告、超时、源码不符和清理不明不能判成功 |
| 外部预测 | 使用Git解析实际路径并核对声明与白名单；禁止测试、配置、增删文件、权限变化和重命名 |
| 资格 | 新容器分别执行未修复负例和标准补丁正例，必须负例unresolved、正例resolved且清理确认 |

原修复入口仍要求失败基线，默认v2未变。新模式只在SWEbenchSession启用。当前轨道没有任意Shell、公共测试执行工具、非Python修改、新增/删除文件或“任意SWE-bench任务均支持”的承诺。Skills/MCP/只读委派的主项目实现保留，本批未把诊断专用工具强行注入普通修复轨道，也未评估其模型增益。

## 固定来源

- 官方源码：02e7a74ffd0b707aab73d203fe87bdc7c76afc8e，版本5.0.2。
- repair_workbench/swebench_harness_lock.json校验75份安装后的官方Python源码。另5份非包目录中的采集脚本按上游setuptools namespaces=false规则未进入wheel，不是运行时评分模块。
- requirements-swebench.lock独立锁定评测器，不安装到Agent虚拟环境。
- 数据：SWE-bench/SWE-bench_Lite，revision b0dde1093fe417d83b7184254edf8199c1f0dff5；test parquet共300题、2,702,170字节，SHA256为438e281d80587aa7be470896ce410557002fde02d2ceee3e099331d308f62dd3。
- 本次仅选开发验证任务psf__requests-3362；不是正式评测子集，不计独立泛化成绩。
- 基线：36453b95b13079296776d11b09cab2567ea3e703；上游tree：5602c48ac5ca0cabc7d13e5fddc339410dc7af18。
- 官方amd64镜像digest：sha256:d1c8c15eeb5e12b7eb3815307d1131d68c72d53419fa2f8b89bc46bc0009a61b。压缩约1.02GB，Docker记录大小3,819,386,195字节。用户了解用途后授权继续，未拉取其他官方任务镜像。

串行执行，2CPU、2GiB、256进程、删除全部capability、no-new-privileges；每轮测试最多600秒，父进程有硬截止与精确容器清理。运行前解析本地不可变镜像ID，禁止自动拉取。

网络策略在运行前指定：默认none；有网络行为检查的官方任务可选bridge，不使用host网络。评分规则不随网络策略改变。子进程不继承模型密钥或第三方追踪设置，Hugging Face离线，不自动下载数据、模型或二进制资产。

## 真实开发任务结果

工作区140个文件，批准83个Python源文件，未按标准补丁缩小范围。Git HTTP传输失败后，使用710,177字节的固定提交归档，完整Git tree比对通过；保留归档、元数据哈希及本地规范化提交。Agent不可见Git历史。

首轮因镜像HEAD不同停止。检查确认镜像重建了历史，并改变部分普通文件的可执行位，全部140个路径和blob内容仍一致。后续校验全部Git blob及工作树清洁状态，记录镜像HEAD；链接、文件增删、内容差异仍拒绝，不以提交名相同代替内容核验。

| 控制 | 官方F2P成功/失败 | 官方P2P成功/失败 | 结果 |
|---|---:|---:|---|
| 未修复 | 0/1 | 73/2 | unresolved |
| 标准补丁 | 1/0 | 73/2 | unresolved |

none和bridge两轮均未取得有效资格。当前环境的两项连接超时行为不符合预期，另外官方镜像缺少pytest-httpbin/httpbin，原始套件存在108项fixture错误。原始输出完整保留，不能将目标缺陷修复写成官方验收通过，也不计为模型失败。未修改测试、删除P2P项目、改变评分门槛或人工修复标准补丁。

适配器已能执行真实任务、读取官方判定、阻止不合格环境进入模型效果评测；当前机器尚未具备这一题的稳定有效环境。正式实测前须在可复现环境重新资格检查，不能复用无效资格。

## 使用入口

建立独立评测环境：

    python3.13 -m venv runtime/swebench-env
    runtime/swebench-env/bin/python -m pip install -r requirements-swebench.lock

导入固定版本官方导出，所有结果目录必须全新：

    .venv/bin/python -m repair_workbench.swebench_adapter import \
      --input runtime/swebench-lite-selected-20261103.json \
      --output runtime/NEW-EVALUATOR-DATASET \
      --dataset-name SWE-bench/SWE-bench_Lite \
      --revision b0dde1093fe417d83b7184254edf8199c1f0dff5

从具有基线提交对象的本地仓库准备：

    .venv/bin/python -m repair_workbench.swebench_adapter prepare \
      --dataset runtime/NEW-EVALUATOR-DATASET --instance psf__requests-3362 \
      --repo /ABSOLUTE/PATH/TO/requests --source-root requests \
      --output runtime/NEW-AGENT-WORKSPACE

固定归档备选入口：

    .venv/bin/python -m repair_workbench.swebench_adapter prepare-archive \
      --dataset runtime/NEW-EVALUATOR-DATASET --instance psf__requests-3362 \
      --archive runtime/swebench-requests-base-20261103.tar.gz \
      --commit-metadata runtime/swebench-requests-base-metadata-20261103.json \
      --source-root requests --output runtime/NEW-ARCHIVE-WORKSPACE

不调用模型的环境正负例检查：

    .venv/bin/python -m repair_workbench.swebench_adapter qualify \
      --dataset runtime/NEW-EVALUATOR-DATASET --instance psf__requests-3362 \
      --workspace runtime/NEW-AGENT-WORKSPACE --output runtime/NEW-QUALIFICATION \
      --harness-python runtime/swebench-env/bin/python --network-policy bridge

正式应用入口为run_qualified_agent(workspace, output, model, qualification, ...)，先核对正负例资格、任务、源码绑定、镜像、网络/资源策略及清理，再创建SWEbenchSession并接入现有run_agent。不合格资格会在创建Agent和调用模型前拒绝。SWEbenchSession是底层构件，供离线测试和集成使用。调用方仍负责提供新批次批准、共享Token预算的模型。本轮没有新增模型授权，也没有CLI自动读取电商配置或启动推理。

提交后导出并评测：

    .venv/bin/python -m repair_workbench.swebench_adapter export \
      --workspace runtime/NEW-AGENT-WORKSPACE --agent-output runtime/AGENT-OUTPUT \
      --output runtime/NEW-PREDICTIONS.jsonl --model-label workbench-swe

    .venv/bin/python -m repair_workbench.swebench_adapter evaluate \
      --dataset runtime/NEW-EVALUATOR-DATASET --instance psf__requests-3362 \
      --workspace runtime/NEW-AGENT-WORKSPACE --predictions runtime/NEW-PREDICTIONS.jsonl \
      --output runtime/NEW-EVALUATION --harness-python runtime/swebench-env/bin/python \
      --network-policy bridge

每轮唯一run_id，不复用官方缓存。不得把Evaluator目录、官方日志、标准补丁或整个宿主目录挂入Agent工具工作区。

## 验证记录

官方执行器合成夹具在无网络环境得到unresolved/resolved，实际容器策略与清理可核对。它有两个合成测试，不是SWE-bench成绩。开发中保留虚拟环境路径、Docker SDK动态集合、网络策略和镜像提交核验的失败记录，随后增加回归和实际inspect回执。

真实任务原资格检查位于runtime/swebench-lite-qualification-20261103；内容绑定后的none结果在同名-v2目录，bridge结果在-v3目录。目录含官方eval.sh、patch.diff、test_output.txt、report.json、资源策略和源码绑定记录；未覆盖旧结果。

CI增加手动可选swebench_harness任务，只用小型合成夹具验证官方执行器，不调用模型、不自动拉取真实任务镜像。最终测试、打包和证据哈希见机器可读交付清单。

来源：[评测指南](https://www.swebench.com/SWE-bench/guides/evaluation/)、[固定执行器](https://github.com/SWE-bench/SWE-bench/blob/02e7a74ffd0b707aab73d203fe87bdc7c76afc8e/swebench/harness/run_evaluation.py)、[固定评分器](https://github.com/SWE-bench/SWE-bench/blob/02e7a74ffd0b707aab73d203fe87bdc7c76afc8e/swebench/harness/grading.py)。
