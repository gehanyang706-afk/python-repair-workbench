# VS Code 真实宿主接入验证

2026-11-06使用已安装的Visual Studio Code 1.143.1，完成原生MCP宿主验证。未使用项目SDK客户端替代宿主，也未发送模型聊天请求。

## 实际接入

专用工作区为`runtime/vscode-mcp-review-20261106`。其中`.vscode/mcp.json`注册`repair-evidence`，使用项目虚拟环境启动本地stdio证据服务；`launch_evidence.py`清理不需要的环境变量并定位项目模块。服务读取工作区中的冻结`evidence.json`并验证指定哈希，不启动Agent、Docker或模型。

仅为这个独立目录授予VS Code工作区信任；没有修改全局MCP配置，也没有把完整项目或其他运行档案加入该工作区。

VS Code原生操作路径：

1. 打开该工作区。
2. 命令面板运行`MCP: List Servers`，选择`repair-evidence`并启动。
3. 输出面板显示连接正在运行和`Discovered 2 tools`。
4. 命令面板运行`MCP: Browse Resources`，分别打开`catalog`与具体报告摘要。

工具目录包含`list_reports`和`query_report`。资源目录包含`diagnostic://catalog`与`diagnostic://reports/r-5d076bb4ae4fbb1f4c98b6ae7b6727df`。VS Code通过MCP读取资源，并在带“只读”标记的编辑器中显示JSON。

CPU摘要实际显示：

- `evidence_ref`: `E0003`
- `snapshot_id`: `34b08120f340dcfbed4aef6d1a70b7b38f8fcf4cd1372d4c5a2dd5a6a8bb2a7e`
- `section`: `summary`，`scale`: `large`
- `object_count`: 6000，`resource_count`: 600，`total_calls`: 6002
- `semantic_ok`: true

证据包SHA-256为`6074e69df9181fcc016e13e4f30de3a0eac65be67ebe0bebb25649bdebc38f45`。这些字段与本地冻结报告一致。该语义检查是探针观测，不是对模型修复效果或最终验收的评价。

## 证据与边界

机器可读摘要见[vscode-mcp-host-delivery-20261106.json](vscode-mcp-host-delivery-20261106.json)。本机原始配置、启动器、证据包、VS Code服务日志及操作结果保存在`runtime/vscode-mcp-review-20261106/`；原始运行目录不提交到GitHub。

本轮验证了真实宿主启动、协议连接、工具发现和Resource读取。没有验证VS Code聊天中的模型自主选工具，也没有在聊天中执行`query_report`；没有调用项目Qwen-Max API，未提交任何VS Code模型聊天请求。前轮SDK工具调用与本轮宿主资源读取分别记录，不混合为一次模型实测。

用户可保留服务以复核报告，或通过`MCP: List Servers → repair-evidence → Stop Server`停止。配置仅在这个工作区生效。接入其他报告时应重新导出证据包并更新哈希，不能修改现有档案伪装为原始测量。

配置与操作参考：[VS Code官方MCP文档](https://code.visualstudio.com/docs/agent-customization/mcp-servers)。该宿主版本支持`.vscode/mcp.json`；其他版本或Agent harness可能使用不同配置位置，需要按对应版本核对。
