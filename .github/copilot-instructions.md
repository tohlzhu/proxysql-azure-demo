# Repository instructions

## 项目上下文与依据

本仓库用于演示 Azure Database for MySQL Flexible Server 前的 SQL 限流、ProxySQL 高可用及 Web 可视化。Python 应用位于 `limiter/src/ratelimit_demo/`，本地 Compose 运行真实双节点链路；Azure 资源与部署结果须通过 Terraform 和目标环境另行验收，不能用本地容器结果代替。

- [README.md](../README.md) 定义项目用途；[实施规格](../azure-mysql-proxysql-rate-limit-prompt.md) 定义具体设计与验收。实现前阅读相关章节，保持代码、配置和文档与契约一致。
- [AGENTS.md](../AGENTS.md) 定义操作权限和资源生命周期。与用户交流用中文，代码、注释、提交信息和日志用英文，文档沿用既有语言风格。
- 规格中的执行步骤不等于当前任务授权；文档或审阅任务不自动触发部署、压测或完整项目实现。

## 目标架构与职责边界

Azure Standard Load Balancer 后放置两个独立节点，每个节点运行 Python 3.12 + FastAPI Web/API、自定义 limiter 和 ProxySQL。HTTP `8080` 流量经 limiter 再访问本机 ProxySQL；MySQL 协议 `6033` 入口直接到 ProxySQL，不能将其描述为经过 HTTP limiter 的严格限流入口。ProxySQL 经 TLS 连接私网 Azure MySQL。

- FastAPI 同时提供 API 和单页控制台，保持单一部署单元；静态资源本地提供，不依赖运行时 CDN。
- `app.py` 组合数据库、限流器、运行存储和压测核心；`limiters.py` 实现内存计数与 Redis Lua，`store.py` 管理共享状态，`load.py` 产生流量并聚合结果，`cli.py` 调用同一场景 API，而非另写压测算法。
- 自定义代码负责精确 QPS 和执行中并发配额。ProxySQL query rules 负责 SQL 匹配、路由、超时、延迟或拒绝；连接池及 `max_connections` 仅是隔离/背压近似手段，不是严格单 SQL 并发信号量。
- `local` 是每节点独立计数；`distributed` 使用共享 Redis 保持全局配额。Redis 也承载双节点 Demo 的运行状态，不能因选择 `local` 限流而改为进程内运行状态。
- Azure Load Balancer 和健康探测负责流量切换；ProxySQL Cluster（若采用）只同步配置。ProxySQL、数据库和 Redis 是独立故障域，数据库 HA 为可选参数。
- 应用镜像、限流业务代码、测试场景及 `DATABASE_*`、`REDIS_*`、`LIMITER_*` 环境变量契约保持云无关；Azure 网络、数据库、计算及负载均衡属于 provider-specific Terraform 模块。

## 限流、API 与共享状态约定

- `POST /query` 仅接受预定义 `query_id` 映射的参数化 SQL，不接受任意 SQL。QPS 采用 token bucket 或 sliding window，配置至少包含 `rate_per_second`、`burst`。
- 并发控制按 `query_id` 设置 `max_concurrency` 和 `queue_timeout_ms`。排队超时返回 HTTP `429`、`Retry-After` 及 `concurrency_limit_exceeded`；数据库或 ProxySQL 不可用返回 `503`，不得混同限流与依赖故障。
- 并发令牌在 `try/finally` 中释放，覆盖取消、超时和数据库异常。分布式 QPS 使用原子操作；并发租约有唯一 lease ID、TTL 及续租或合理超时，确保异常退出后名额最终回收。
- `POST /demo/runs` 只接受服务端定义的场景及有界参数。任务必须支持最长运行时间、最大并发、取消和过期清理，不接受任意 URL、SQL、Redis key 或命令。
- 双节点的运行元数据、事件序列、取消状态、最终摘要及节点 heartbeat 存入共享 Redis，并设置有界 TTL。内存存储适配器仅用于纯本地单进程开发。
- SSE 以 `run_id` 和最后事件 ID 支持跨节点重连；无法恢复时回退状态轮询，必须保留最终结果。浏览器刷新后也可按 `run_id` 恢复。
- CLI 与 Web 场景复用同一个 Python asyncio 负载生成核心及结果 schema，结果收集脚本也复用该 schema。JSON 包含 `schema_version`、运行/场景 ID、限流模式、时间范围、请求及状态码计数、延迟 percentile、节点分布和错误摘要。
- `/metrics` 使用 Prometheus 格式，按 `query_id` 聚合；不以完整 SQL 或高基数用户 ID 为标签。日志和页面不显示凭据、完整连接串或敏感 SQL 参数。

## ProxySQL 与故障转移约定

- 管理端口 `6032` 仅本机或管理网段可访问，页面不直接连接该端口。配置使用版本化模板和可重复脚本，变更需 `LOAD ... TO RUNTIME` 与 `SAVE ... TO DISK`。
- 核验配置与流量使用 `runtime_mysql_servers`、`runtime_mysql_query_rules`、`stats_mysql_connection_pool` 和 `stats_mysql_query_digest`。
- TCP `6033` 和 HTTP `8080` 使用独立健康探测；HTTP readiness 要求 Web/API 与本机 ProxySQL 均 ready。
- 故障转移演示停止一个 ProxySQL、测量短暂失败和恢复时间、验证另一节点承接流量，再恢复原节点。不得宣称零失败或零中断。
- Web 故障注入默认关闭；启用需管理授权，只能控制当前项目 Terraform state 中的 ProxySQL 服务，不授予普通用户或 CI 此权限。

## Terraform 与 Azure 操作边界

- 仅用 Terraform，不并行维护 Bicep。固定 Terraform/provider 兼容版本并提交 `.terraform.lock.hcl`；网络、数据库、计算、负载均衡和可选可观察性拆分模块。
- Demo 目标为 `japaneast` 的共享资源组 `rg-dev2`。将资源组作为 data source 引用，不纳入 state。`rg-dev` 云基础设施只读；其他操作范围和成本批准阈值以 `AGENTS.md` 为准。
- 部署前核验实际 Azure 登录和订阅与规格一致；不匹配时停止，不自动切换。通过 `ARM_SUBSCRIPTION_ID` 向 Terraform 注入订阅 ID，不把真实订阅/tenant ID、token 或密码写入仓库。
- 创建收费资源前展示 plan、SKU、成本风险、state 位置和清理方式，并按实施规格等待明确的 `DEPLOY`。资源组权限不等于自动部署授权。
- 默认本地 state；远程 backend 迁移写入文档，不尝试用普通 Terraform 变量动态切换 backend。敏感输入声明 `sensitive = true`，普通 outputs 只给非敏感地址和资源名。
- 节点初始化采用版本化 cloud-init/模板和幂等脚本，应用发布与基础设施分离，不堆叠不可重复的 provisioner 长脚本。
- MySQL 默认不对公网开放且连接启用 TLS；公开 HTTP 必须限定 CIDR。真实 `.env`、`.tfvars`、state、plan 和运行产物需排除提交，只提交无秘密的配置示例及小型脱敏结果。
- 资源按 `AGENTS.md` 标记 `owner=agent task=<task-id> autodelete=true`，精确资源清单仅保存在本地。销毁仅针对当前 state 精确记录且带项目标签的资源；脚本展示清单并要求完整项目标识确认，不删除共享资源组或按标签搜索扩大删除范围。

## 命令与验收契约

从仓库根目录运行 `make install` 建立 `.venv` 并安装 `limiter[test]`。直接运行 Python/ruff 命令前激活该环境；pytest 的根配置为 `pytest.ini`。

| 用途 | 规格要求的命令 |
| --- | --- |
| 本地容器 Demo | `make demo-local` |
| 无浏览器运行同一组场景 | `make test-demo` |
| Python 测试 | `make test`（自动读取本地 `.env` 中的 Redis 测试连接）；或 `python -m pytest` |
| 真实浏览器 smoke | `.venv/bin/python -m playwright install chromium` 后 `make test-web` |
| Python lint / 格式检查 | `ruff check .` / `ruff format --check .` |
| Terraform 格式 | `terraform -chdir=infra/terraform fmt -check -recursive` |
| Terraform 初始化与校验 | `terraform -chdir=infra/terraform init -backend=false` 然后 `terraform -chdir=infra/terraform validate` |
| 目标订阅下的 plan | `terraform -chdir=infra/terraform plan -input=false -out=tfplan` |

单个 Web smoke：`DEMO_BASE_URL=http://127.0.0.1:8080 .venv/bin/python -m pytest -c pytest.ini limiter/tests/test_web_smoke.py::test_console_success_rejection_and_restore`。Redis 测试使用 `TEST_REDIS_URL`、独立数据库 15 和唯一 key 前缀；缺少 Redis 而跳过的测试不能当作集成验收。`make test-demo` 将五个场景 JSON 写入 `artifacts/demo/`；CLI `--all` 还支持以 `.json` 结尾的 `--output`，将原始快照放入带 `schema_version` 的 `runs` 数组。plan 检查后删除本地 plan，不提交 plan/state。

本地容器命令经 `scripts/docker.sh`，需要时设置 `DOCKER='sudo -n docker'`，不要修改系统服务或用户组。`make stop-local` 保留数据卷，`make clean-local` 才删除当前 Compose 项目的可重建数据。`.env` 首次生成后不覆盖；配置文件按数据解析，不用 shell source/eval。

Azure 部署后，开发机没有私网连通时用 `.venv/bin/python scripts/azure-verify.py` 在 Redis VM 内验证十个场景、浏览器和真实 LB 故障切换；该命令会停止当前项目 node-1 的 ProxySQL 20 秒并自动恢复，需要对应操作授权。结果收回 `artifacts/azure/`，不为测试自动开放公网或创建 peering。发布后保留受限 `proxy.env` 供管理统计，仅删除数据库管理员临时 `admin.env`。

验收重点是算法的真实行为：两个 limiter 实例共享 Redis 时全局阈值不翻倍；异常/取消后并发令牌释放；场景参数边界、重复取消、过期和 SSE 重连可验证；摘要与原始请求计数及 percentile 一致。Web smoke test 要实际启动场景并呈现成功与拒绝。CLI、Web 下载和收集结果使用同一 schema，不能只凭截图验收。

建议演示基线为 QPS `10`、burst `5`、并发 `3`、排队超时 `500 ms`，判断 QPS 结果时计入 burst。本地可用真实 MySQL、ProxySQL 和 Redis 容器，不 mock 被测限流算法。Shell 脚本使用 `set -euo pipefail`；压测结果保存到 `artifacts/`。
