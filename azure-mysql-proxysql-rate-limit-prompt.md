# GitHub Copilot CLI 实施 Prompt：Azure MySQL + ProxySQL SQL 限流、高可用与可视化 Demo

你是一名资深 Azure、MySQL、ProxySQL、Linux、Python、Web 和 Terraform 工程师。请直接在当前本地 Git 项目中完成一个**可部署、可观察、可验证、可清理**的实验项目，用于演示面向 **Azure Database for MySQL Flexible Server** 的 SQL 限流方案，并让用户通过 Web 页面直观看到正常流量、限流和节点故障转移的差异。

## 1. 已知 Azure 环境

- Azure 订阅名称：`ME-MngEnvMCAP012397-zhuhonglei-1`
- 实验区域：`japaneast`
- Azure CLI 已登录，不要要求我重新登录。
- 不要把真实 subscription ID、tenant ID、密码或 token 写入仓库。需要订阅 ID 时，从当前登录上下文读取并通过环境变量 `ARM_SUBSCRIPTION_ID` 传给 Terraform。
- 在执行任何部署前，先运行并核验：

```bash
az account show --query '{name:name,id:id,tenantId:tenantId}' -o json
```

如果当前订阅名称不匹配，立即停止，不得自动切换或部署到其他订阅。输出中不得打印或持久化 token。

## 2. 总体目标

实现一个完整 Demo，架构如下：

```text
Browser / Load Generator / Demo Client
                    |
                    v
    Azure Standard Load Balancer
       HTTP 8080 and MySQL 6033
                    |
          +---------+---------+
          |                   |
   Node 1: Web/API         Node 2: Web/API
   + limiter service       + limiter service
   + ProxySQL-1            + ProxySQL-2
          |                   |
          +---------+---------+
                    |
                    v
 Azure Database for MySQL Flexible Server

Distributed mode: both limiter services -> shared Redis-compatible backend
```

需要同时演示：

1. **自定义代码限流实现**。
2. **按请求 QPS 限流**和**按请求并发数限流**两类场景。
3. **ProxySQL 层的高可用**，至少两个 ProxySQL 实例，单节点故障时客户端仍可通过统一入口访问。
4. **Web 可视化评估**，无需命令行即可发起低于/高于阈值的流量并观察接受、排队、拒绝、延迟和故障转移结果。
5. **Terraform 交付和可移植性边界**：Azure 是本项目实际实现和验收目标；应用镜像、配置契约、测试场景和 Terraform 工作流应保持云无关。Azure、网络、负载均衡和托管数据库资源仍是 provider-specific 模块，不得宣称一套资源定义可不经修改部署到所有云。

请注意并在 README 中明确说明：

- ProxySQL 原生 `mysql_query_rules` 适合 SQL 匹配、路由、重写、缓存、延迟、超时或拒绝等治理，但不要把并不存在的原生“精确分布式 QPS/并发令牌桶”能力写成 ProxySQL 内置功能。
- 精确 QPS 和并发控制由自定义代码负责。
- Azure Database for MySQL 的 HA 与 ProxySQL 层 HA 是两个独立故障域。本 Demo 重点验证 ProxySQL 层 HA；数据库端是否启用 HA 应做成参数，并说明成本影响。
- ProxySQL 是开源组件，生产支持边界、版本兼容性和运维责任必须写入 README。

## 3. 技术设计要求

### 3.1 Azure 资源

只使用 Terraform 实现基础设施，不得同时维护 Bicep。固定 Terraform 与 provider 的兼容版本并提交 `.terraform.lock.hcl`；资源名称要带随机或参数化后缀，避免冲突。在已授权的共享资源组 `rg-dev2` 中创建本实验资源；资源组本身作为 data source 引用，不纳入 Terraform state，`terraform destroy` 不得删除资源组或其中不受当前 state 管理的既有资源。

至少部署：

- 1 个 VNet。
- 数据库委派子网，供 Azure Database for MySQL Flexible Server 私网接入。
- ProxySQL 子网。
- Azure Database for MySQL Flexible Server，使用成本较低但足以测试的 SKU，数据库名称如 `ratelimitdemo`。
- 2 台 Linux VM 或两个等价的独立 ProxySQL 节点，尽量分布在不同可用区。如果所选 SKU/区域当时无法跨区，要自动降级并在输出中说明。
- Azure Standard Load Balancer，提供统一入口：
  - TCP `6033`：ProxySQL/MySQL 协议入口和 TCP 健康探测。
  - TCP `8080`：Web/API 入口；使用独立健康探测，只有 Web/API 和本机 ProxySQL 均 ready 时节点才接收 Demo HTTP 流量。
- NSG 最小权限配置。
- Log Analytics/Azure Monitor 作为可选参数，默认可以关闭以控制成本。

安全要求：

- 不把数据库密码、ProxySQL 管理密码或应用密码提交到 Git。
- 使用环境变量、本地 `.env`（必须加入 `.gitignore`）或 Key Vault。
- MySQL 连接启用 TLS。
- 默认不向公网开放 MySQL。
- VM 管理入口默认限制为当前调用者 IP，或优先使用 Azure CLI Run Command 完成配置。

Terraform 要求：

- 使用 `azurerm` provider 和清晰的 root module；按网络、数据库、计算、负载均衡、可选可观察性拆分模块，避免将所有资源堆在单一文件。
- 使用 `variables.tf`、`outputs.tf` 和不含秘密的 `terraform.tfvars.example`；敏感输入声明 `sensitive = true`，真实 `.tfvars`、state、plan 文件必须加入 `.gitignore`。
- 默认使用本地 state 以简化 Demo，同时在文档中提供 Azure Storage、S3 或 GCS 远程 backend 的迁移说明，说明 backend 配置不能在普通变量中动态切换。
- 不在 provisioner 中堆叠不可重复的长脚本。节点初始化使用版本化模板/cloud-init 和幂等脚本；应用发布与基础设施资源职责分离。
- 输出只包含后续脚本需要的非敏感地址和资源名；敏感值不得通过普通 output 暴露。
- 为多云验证建立稳定接口：应用只依赖 `DATABASE_*`、`REDIS_*`、`LIMITER_*` 等环境变量；云模块负责映射网络、DNS、身份和服务端点。其他云只需要新增独立 provider 模块，不复制或分叉限流业务代码。

### 3.2 ProxySQL 配置

每个节点安装并配置 ProxySQL，至少包含：

- 管理端口 `6032`，仅本机或管理网段可访问。
- MySQL 流量端口 `6033`。
- Azure MySQL 后端服务器定义。
- monitor 用户和业务用户。
- TLS 后端连接。
- 健康检查。
- `LOAD ... TO RUNTIME` 和 `SAVE ... TO DISK` 的持久化流程。
- 示例 `mysql_query_rules`：
  - 根据 SQL digest 或正则匹配 Demo 查询。
  - 一个用于路由或标记的规则。
  - 一个用于超时、延迟或明确拒绝的保护规则。
- 配置验证命令，展示：
  - `runtime_mysql_servers`
  - `runtime_mysql_query_rules`
  - `stats_mysql_connection_pool`
  - `stats_mysql_query_digest`

不要错误地使用 `max_connections` 声称它可以直接实现“某一 SQL 指纹的运行中并发数”。如果使用独立 hostgroup 加小连接池作为近似保护，必须标注其是**隔离/背压近似方案**，不是严格的单 SQL 并发信号量。

### 3.3 自定义限流代码

使用 Python 3.12 + FastAPI。代码必须清楚、带类型提示、单元测试和结构化日志。Web 页面由同一 FastAPI 服务提供，以保持单一部署单元并避免额外公网依赖；前端可使用轻量原生 HTML/CSS/JavaScript 和图表库的本地静态文件，不得依赖运行时 CDN。

限流服务位于客户端与 ProxySQL 之间，提供 HTTP Demo API，例如：

```text
POST /query
GET  /healthz
GET  /readyz
GET  /metrics
GET  /
POST /demo/runs
GET  /demo/runs/{run_id}
GET  /demo/runs/{run_id}/events
POST /demo/runs/{run_id}/cancel
```

`POST /query` 不允许执行任意用户输入 SQL。通过预定义 `query_id` 映射到参数化 SQL，避免把 Demo 变成任意 SQL 代理和注入入口。

`POST /demo/runs` 只能接受服务端定义并校验的场景 ID、持续时间和并发/QPS 等有界参数，不得接受 shell 命令或 SQL。运行任务必须有最大持续时间、最大并发、取消机制和过期清理，避免浏览器误操作制造无限压测。`events` 使用 Server-Sent Events（SSE）；断线后允许客户端携带最后事件 ID 重连。若 SSE 无法恢复，则回退轮询 `GET /demo/runs/{run_id}`，但不得静默丢失最终结果。

实现两种独立限流器：

#### A. QPS 限流

- 使用 token bucket 或 sliding window 算法。
- 支持按 `query_id`，可选再叠加 tenant/user 维度。
- 参数至少包括：`rate_per_second`、`burst`。
- 超限返回 HTTP 429，并提供 `Retry-After` 或等价可观察字段。
- 必须提供可重复压测，清楚展示阈值以内成功、阈值以上出现 429。

#### B. 并发限流

- 使用 semaphore 控制同一 `query_id` 同时执行中的请求数。
- 参数至少包括：`max_concurrency`、`queue_timeout_ms`。
- 超过并发上限时可短暂排队，排队超时返回 HTTP 429，并返回 `Retry-After` 和机器可读的拒绝原因 `concurrency_limit_exceeded`。README 必须解释该语义；数据库或 ProxySQL 不可用才返回 503，不得与限流混淆。
- 使用一个可控耗时的预定义查询演示并发占用。不要依赖生产禁用的危险函数。可以通过测试表、存储过程或应用侧受控延迟构造。
- 必须使用 `try/finally` 释放 semaphore，覆盖取消、数据库异常和超时场景。

### 3.4 多节点一致性

由于两个 ProxySQL/限流节点放在负载均衡器后面，单机内存计数会使全局限流阈值被放大。请实现并比较两种模式：

1. `local` 模式：每节点独立内存限流，仅用于说明问题和本地测试。
2. `distributed` 模式：使用 Redis 兼容后端实现全局 QPS 与并发配额。

对于 distributed 模式：

- 优先选择低成本且便于 Demo 的 Redis 方案。若部署 Azure Managed Redis 会显著增加成本或区域不可用，可使用单独 Redis VM/容器，但必须明确它在 Demo 中可能成为单点。
- QPS 使用原子 Lua 脚本或等价原子操作。
- 并发租约必须有 TTL、唯一 lease ID、续租或合理超时时间，并确保异常退出后名额最终可回收。
- 编写测试验证两个 limiter 实例共同访问一个 Redis 时，全局阈值不会简单翻倍。

### 3.5 Web 可视化控制台

实现一个面向演示者的响应式单页控制台，由 `GET /` 提供。页面至少包含：

- **系统状态**：Load Balancer 入口、两个节点的 ready 状态、当前服务节点 `node_id`、ProxySQL/MySQL/Redis 连通状态，以及当前 `local` 或 `distributed` 模式。不得显示凭据和完整连接串。
- **场景控制**：提供“阈值内 QPS”“超过 QPS”“阈值内并发”“超过并发”“双节点分布式”“节点故障转移”预置场景；允许调整的值必须有前后端一致的安全上下限，并显示当前限流配置。
- **实时图表**：按 1 秒时间桶展示总请求、成功、429/503、当前并发、P50/P95/P99 延迟和各节点处理量；接受与拒绝应使用不同颜色，图例和单位清晰。
- **运行摘要**：显示场景参数、开始/结束时间、总请求、成功率、限流率、峰值并发、各状态码数量、恢复时间和短暂失败数。结果支持下载为 JSON；下载内容采用稳定 schema 并包含 `schema_version`。
- **对照解释**：在同一页面说明 QPS token bucket 的 burst、并发排队/拒绝语义、`local` 阈值可能随节点数放大，以及 `distributed` 的全局约束。
- **错误与取消**：展示可操作错误，不把失败包装成成功；浏览器刷新后可通过 `run_id` 恢复最近一次运行，用户可取消正在执行的场景。

页面不得直接连接 ProxySQL 管理端口，不得允许用户填写任意目标 URL、SQL、Redis key 或系统命令。Demo 默认仅内网访问；如果显式启用公网 HTTP，Terraform 必须要求受限 CIDR，并在 README 中提示该控制台可触发负载，不应暴露给不可信用户。

在双节点部署中，运行元数据、事件序列、取消状态和最终摘要必须存入共享 Redis，并设置有界 TTL；SSE 连接切换到另一节点后应能按 `run_id` 和最后事件 ID 继续读取。节点通过带 TTL 的 heartbeat 发布自身状态，页面据此展示两个节点，不能依赖某一节点的进程内状态。`local` 限流模式只表示限流计数器本地化，不代表 Demo 运行状态可以丢失。纯本地单进程开发可提供内存存储适配器，但双节点和故障转移测试必须使用 Redis。

至少增加以下自动化测试：

- API 场景参数边界、未知场景、重复取消和运行过期。
- SSE 事件顺序、重连和最终完成事件；或所选可靠轮询方案的等价覆盖。
- 汇总数据与实际请求计数一致，延迟 percentile 计算正确。
- Web 页面 smoke test，确认页面能加载、启动预置场景并呈现成功/拒绝两类结果。

## 4. ProxySQL 高可用演示

至少实现以下内容：

- 两个 ProxySQL 节点位于 Standard Load Balancer 后。
- TCP 6033 健康探测。
- 节点配置生成方式必须可重复，不能依赖手工逐台修改。
- 配置源采用 Git 中的模板加部署脚本，或一个管理节点向其他 ProxySQL 节点同步配置。
- 如果采用 ProxySQL Cluster 配置同步，需要：
  - 明确配置 `admin-cluster_*` 凭据和 `proxysql_servers`。
  - 展示查询规则/用户/后端服务器配置同步验证。
  - 说明 ProxySQL Cluster 同步的是配置，不是客户端流量故障转移机制。
  - 流量故障转移由 Azure Load Balancer 和健康探测完成。
- 提供 `scripts/failover-test.sh`：
  1. 持续通过负载均衡器发送请求。
  2. 停止 ProxySQL-1。
  3. 记录短暂错误和恢复情况。
  4. 验证流量进入 ProxySQL-2。
  5. 恢复 ProxySQL-1 并验证重新加入。
- 不要宣称零失败或零中断。应测量并输出实际失败请求数与恢复时间。

## 5. Demo 数据与压测

创建最小测试数据，例如：

- `customers`
- `orders`
- `query_audit`

提供初始化脚本和参数化查询。

至少提供以下脚本：

```text
scripts/deploy.sh
scripts/configure-proxysql.sh
scripts/init-db.sh
scripts/test-qps-limit.sh
scripts/test-concurrency-limit.sh
scripts/test-distributed-limit.sh
scripts/failover-test.sh
scripts/collect-results.sh
scripts/destroy.sh
```

命令行压测使用 Python asyncio 客户端，使 Web 场景和 shell 脚本复用同一个负载生成核心与结果 schema，避免两个实现产生不同统计口径。测试输出保存到 `artifacts/`，但运行结果和大文件不要提交 Git；仅提交小型脱敏示例结果用于说明 UI。

建议默认演示配置：

```yaml
limits:
  qps_demo:
    rate_per_second: 10
    burst: 5
  concurrency_demo:
    max_concurrency: 3
    queue_timeout_ms: 500
```

测试必须展示：

- QPS：低于 10 QPS 时基本成功，高于阈值时触发 429，允许 token bucket 的 burst 行为。
- 并发：并发 3 以内成功，高于 3 时出现排队或拒绝。
- 分布式：流量分散到两个节点时仍遵守全局阈值。
- HA：停止任一 ProxySQL 节点后，统一入口仍能恢复服务。

展示流程必须同时支持：

1. `make demo-local` 启动不依赖 Azure 的本地容器环境，浏览器可访问控制台并完成 QPS/并发/local/distributed 场景。
2. `make test-demo` 无浏览器运行相同场景，适用于 CI，并生成与 Web 下载相同 schema 的结果。
3. Azure 部署后，通过 Terraform output 获取统一 HTTP 入口；用户打开页面依次执行阈值内、超阈值、distributed 和故障转移场景。
4. 每个场景在 UI 中显示“预期”和“实测”，验收以原始状态码和服务端指标为准，不仅依赖图表截图。

本地环境可以使用 MySQL、ProxySQL 和 Redis 容器，但不得 mock 掉被测限流算法。Azure 故障转移场景涉及 VM 服务控制，Web 页面只能发起服务端预定义且受保护的演示动作；默认关闭该能力。启用时必须要求管理授权，且只能操作 Terraform state 中本项目的 ProxySQL 服务。CI 和普通公网用户不得获得该权限。

## 6. 可观察性

实现：

- Prometheus 格式 `/metrics`。
- 指标至少包括：
  - 请求总数。
  - 成功数。
  - QPS 限流拒绝数。
  - 并发限流拒绝数。
  - 当前并发数。
  - 排队时间。
  - SQL 执行耗时。
  - MySQL/ProxySQL 连接错误数。
  - 按 `query_id` 标签聚合，禁止把完整 SQL 或高基数用户 ID 放入指标标签。
- 日志不得输出密码、连接串或敏感 SQL 参数。
- `collect-results.sh` 汇总负载测试、ProxySQL stats、Azure 资源状态和健康探测状态。
- 所有 JSON 结果统一使用带 `schema_version`、`run_id`、`scenario_id`、`limiter_mode`、时间范围、请求计数、状态码计数、延迟 percentile、节点分布和错误摘要的 schema；CLI、Web 和 `collect-results.sh` 必须复用。

## 7. 代码仓库产物

请生成并实际填写以下结构，可按实现微调，但不能只提供伪代码：

```text
.
├── README.md
├── Makefile
├── .gitignore
├── .env.example
├── config/
│   ├── limits.yaml
│   └── proxysql.cnf.tpl
├── infra/
│   └── terraform/
│       ├── versions.tf
│       ├── providers.tf
│       ├── main.tf
│       ├── variables.tf
│       ├── outputs.tf
│       ├── terraform.tfvars.example
│       └── modules/
├── limiter/
│   ├── pyproject.toml
│   ├── Dockerfile
│   ├── src/
│   ├── static/
│   ├── templates/
│   └── tests/
├── sql/
│   ├── schema.sql
│   └── seed.sql
├── scripts/
└── docs/
    ├── architecture.md
    ├── operations.md
    ├── security.md
    └── test-plan.md
```

README 必须包括：

- 架构图，允许 Mermaid。
- 前置条件。
- 一键部署、验证、故障转移测试、清理命令。
- 成本提示。
- 安全说明。
- ProxySQL、Azure MySQL、Redis 三类故障域。
- 本 Demo 限制和生产化改进清单。
- QPS 与并发算法说明。
- `local` 与 `distributed` 模式差异。
- ProxySQL Cluster 配置同步与 Azure Load Balancer 流量切换的职责边界。
- Web 控制台使用方法、每个场景的预期图表和结果判读方法。
- Terraform state 安全、初始化、plan/apply/destroy、远程 backend 迁移和 provider-specific 模块边界。
- 多云可移植性矩阵：明确哪些层可直接复用、哪些层必须按云重写，以及在第二个云上验证可行性所需的最小步骤。不得把 Terraform 等同于自动实现多云。

## 8. 自动化质量门禁

执行并修复直到通过：

```bash
python -m pytest
ruff check .
ruff format --check .
terraform -chdir=infra/terraform fmt -check -recursive
terraform -chdir=infra/terraform init -backend=false
terraform -chdir=infra/terraform validate
terraform -chdir=infra/terraform plan -input=false -out=tfplan
```

还应包含：

- 限流算法单元测试。
- 两实例共享 Redis 的集成测试。
- 数据库异常时并发令牌释放测试。
- 非法 `query_id` 和非法参数测试。
- 配置文件 schema 校验。
- Web/API 与结果 schema 测试。
- Terraform 格式、校验和 plan 检查；测试后删除本地 `tfplan`，不得提交 plan/state。
- Shell 脚本 `set -euo pipefail`。
- CLI 命令失败时输出明确错误。

## 9. 执行方式与安全护栏

请按以下顺序工作，不要只给建议：

1. 检查当前仓库、工具版本、Azure CLI 登录和订阅。
2. 调研并引用最新官方文档，特别是 Azure MySQL、ProxySQL query rules/cluster、Azure Load Balancer 健康探测。
3. 输出简短实施计划和文件树。
4. 编写完整代码、Web 控制台、Terraform、配置和文档。
5. 先运行本地单元测试和静态检查。
6. 运行 Terraform fmt、init、validate 和 plan；检查 plan 不包含意外删除、公开数据库或敏感 output。
7. 在真正创建收费 Azure 资源前，输出 plan 摘要、将创建的资源、SKU、预计成本风险、state 存放位置和清理命令，并等待我明确输入 `DEPLOY`。
8. 收到 `DEPLOY` 后才执行部署与端到端测试。
9. 不得删除既有资源组或修改与本 Demo 无关的资源。
10. `destroy.sh` 只能销毁当前 Terraform state 精确记录且带本项目标签的资源；运行前展示待删除资源并要求输入完整项目标识确认。不得删除共享资源组 `rg-dev2`，不得通过标签搜索结果扩大删除范围。

## 10. 验收标准

只有满足以下条件才视为完成：

- 仓库中存在可运行代码，不是概念性片段。
- 自定义 QPS 限流测试通过。
- 自定义并发限流测试通过。
- 两实例 distributed 全局限流测试通过。
- 本地 Web 控制台可以启动所有非故障注入场景，实时呈现成功、限流、并发、延迟和节点分布，并下载与 CLI 一致的 JSON 结果。
- QPS 和并发 UI 场景的“预期/实测”结果与服务端指标一致；Web smoke test 和场景 API 测试通过。
- ProxySQL 可连接 Azure MySQL 并执行预定义查询。
- ProxySQL query rules 已加载到 runtime 并持久化到 disk。
- Load Balancer 健康探测可区分两个 ProxySQL 节点。
- 停止一个 ProxySQL 节点后，故障转移脚本给出实际测量结果。
- 所有 secret 均未进入 Git。
- Terraform fmt、validate 和部署目标订阅下的 plan 通过，state/plan 未进入 Git，且没有 Bicep 作为并行实现。
- README 明确列出多云可复用层和 provider-specific 层，不夸大 Terraform 的跨云能力。
- 提供一键清理命令。
- README 清楚区分“严格限流”“背压/连接池隔离”“SQL 路由/超时/拒绝”三类机制。

## 11. 最终回复格式

完成每个阶段后，用中文简洁汇报：

1. 已创建或修改的文件。
2. 已执行的命令。
3. 测试结果。
4. 未解决问题或环境限制。
5. 下一条我应执行的命令。

现在开始执行第 1 至第 6 步。未收到我的 `DEPLOY` 前，不要创建收费 Azure 资源。
