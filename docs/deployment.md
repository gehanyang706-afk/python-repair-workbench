# 本机部署与运行

## 适用范围

版本 1.0.0 面向单用户、本机、受控 Python Git 仓库。服务只监听 `127.0.0.1`，没有多用户认证或公网部署能力。候选在独立目录构建，原仓库不会被工作台自动修改。仅将自己有权发送给模型服务的代码和测试日志交给工作台处理。

## 前置条件与依赖

- Python 3.13、Git、正在运行的 Docker，以及可调用的 DashScope `qwen-plus` API Key。
- 已存在的本地测试镜像，至少包含 Python、pytest 和目标仓库所需的依赖。工作台不会拉取镜像、安装目标项目依赖或执行模型生成的命令。不同目标仓库可以使用不同的测试镜像。
- 本仓库的 `requirements-agent.lock` 固定了当前完整虚拟环境中 84 个包的版本；`pyproject.toml` 列出直接依赖。锁定版本提高同环境复现性，但没有跨平台哈希锁定，也不保证上游模型输出确定性。

在项目根目录执行：

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements-agent.lock
.venv/bin/python -m pip check
docker image inspect /your/local-test-image:tag
RUN_DOCKER_TESTS=1 REPAIR_TEST_IMAGE=/your/local-test-image:tag \
  .venv/bin/python -m unittest discover -s tests -v
```

测试镜像必须适合本仓库测试夹具，才能运行上述完整回归。只检查核心 Python 逻辑时可去掉 `RUN_DOCKER_TESTS=1`，此时 Docker 测试会跳过，不应写成完整验收通过。本机交付验收使用的镜像 ID、依赖状态及结果见 [交付记录](release-v1.md)。

## 模型配置与启动

在仓库外保存私有配置文件，只授予当前用户读取权限：

```text
DASHSCOPE_API_KEY=<your-key>
REPAIR_MODEL=qwen-plus
```

不要将密钥粘贴进命令行参数、网页任务、补丁或 Git。`--env-file` 指向该文件；进程环境中若已有 `DASHSCOPE_API_KEY`，其优先级高于文件。服务启动后从 `http://127.0.0.1:8765` 使用。

```bash
.venv/bin/python -m repair_workbench serve \
  --repo-root /absolute/path/to/approved-repositories \
  --data-dir /absolute/path/to/private-task-data \
  --image /your/local-test-image:tag \
  --env-file /absolute/path/to/private-model.env \
  --profile v2 \
  --port 8765
```

`--repo-root` 可重复指定，建议只指向需要处理的目录。任务数据目录须与目标仓库分开；同一数据目录只能有一个服务进程。默认 `v2`，只有显式 `--profile full` 才启用尚未通过真实模型验收的 v3 实验机制。网页任务默认每题最多 12 次模型调用、3 次候选，可能产生费用；提交前检查模型、镜像、目录与任务描述。

## 操作与演示案例

准备一个独立的演示 Git 仓库，不修改工作台源文件：

```bash
mkdir -p runtime/demo-repo
cp -R tests/fixtures/broken_discount/. runtime/demo-repo/
git -C runtime/demo-repo init -q
git -C runtime/demo-repo add --all
git -C runtime/demo-repo -c user.name=Demo -c user.email=demo@example.invalid commit -qm 'Boundary regression'
```

启动服务时将 `--repo-root` 指向该 `runtime/demo-repo` 绝对路径，将 `--data-dir` 指向仓库外或 `runtime/` 下的兄弟目录。网页中新建任务：读取仓库，批准 `pricing.py`，描述“满 100 元应享九折，保持低于与高于 100 元的行为”，然后提交。原始测试应复现 1 个失败；模型是否成功修复不作保证。成功后检查前后测试、事件、日志与 `.diff`，自行审核补丁，再决定是否应用到原仓库。

无需模型费用的确定性链路验收由 `tests/test_web.py` 覆盖；它通过真实 HTTP 应用、SQLite 队列、Docker 测试、v2 Agent 工具、补丁和下载接口，但使用脚本模型，不作为真实模型成绩。真实 Bug 的独立验收记录见 [评测报告](evaluation-v3-20261012.md)。

## 运行数据、备份与回退

数据目录包含 `tasks.sqlite`、事件、测试日志、候选副本及补丁。停止服务后再复制整个目录做备份；不要只复制 SQLite 文件而遗漏 `runs/`。原仓库的未提交改动从不进入任务快照。服务重启时遗留运行任务标记为 `interrupted`，不会自动续跑或重复计费；排队任务会继续领取。

交付源码由本地 Git 标签 `v1.0.0` 固定。要回退，先停止服务并备份数据目录，再从该标签启动独立进程；不要让两个版本同时操作同一数据目录。没有数据库迁移系统，跨未来版本复用数据前应另行核对格式。当前版本只提供本机运行说明，不包含容器化服务部署、自动镜像构建、进程守护或公网访问支持。
