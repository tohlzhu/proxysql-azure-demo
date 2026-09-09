# 部署、维护与清理

## 本地链路

`make demo-local` 生成本地密码和 ProxySQL 前端 TLS 证书、启动 MySQL/Redis、导出本地 MySQL 公共 CA、渲染 ProxySQL 模板、构建应用、初始化数据及用户，再启动两个节点和 HAProxy。配置端口与默认模式使用 `.env`；业务限流配置位于 `config/limits.yaml`。`LIMITER_PUBLIC_URL` 仅供页面展示，`LIMITER_LOAD_URL` 是节点实际访问的内部 LB 地址，两者不能混用。

```bash
DOCKER='sudo -n docker' bash scripts/configure-proxysql.sh
DOCKER='sudo -n docker' bash scripts/configure-proxysql.sh --stats-only
```

管理客户端在对应 ProxySQL 容器的网络命名空间运行，因此无需暴露 `6032`。脚本输出仅投影后的 `runtime_mysql_servers`、`runtime_mysql_query_rules`、`stats_mysql_connection_pool`、`stats_mysql_query_digest`；不导出 mysql_users 密码。

如果用户已拥有 Docker 权限，省略 `DOCKER` 设置即可。所有本地生命周期操作限定当前 Compose project，停止默认保留数据。

## Terraform 工作流

先阅读 `infra/terraform/terraform.tfvars.example` 和 `variables.tf`。真实 tfvars、SSH 私钥、凭据、state 和 plan 仅保存在受限本地环境中。

1. 确认 Azure CLI 当前目标订阅，不自动切换或注册 provider。
2. 执行 `make terraform-check` 与 `scripts/plan.sh`，审查创建范围、网络入口、SKU 和费用。
3. 在明确批准 `DEPLOY` 后执行 `scripts/deploy.sh`，部署资源并通过版本化发布脚本配置应用。
4. 从 Terraform outputs 获取统一入口；私网默认要求从同 VNet 或已授权网络访问，不自动修改 `rg-dev` 或创建跨 RG peering。
5. 验证 ProxySQL runtime/disk、TLS 数据库连接、HTTP readiness、两节点 distributed 及真实故障转移。
6. 执行 `scripts/destroy.sh`，核验精确 state 清单并输入完整项目标识。

测试资源清理后核验状态；清理失败时保留明确残留清单与费用风险，不宣称已清理。不要因为某个资源带项目标签就删除未在本次 state 中记录的资源。

## 成本基线

2026-09-09 查询 [Azure Retail Prices API](https://prices.azure.com/api/retail/prices) 得到 Japan East 按需 Linux `Standard_B2ls_v2` 为 USD `0.0544/小时`，`Standard_B2ats_v2` 为 `0.0123/小时`；MySQL Flexible Server Burstable `B1MS` 为 `0.026/小时`，普通存储为 `0.138/GB/月`。这两个 VM SKU 是当前默认值；旧版 B2s/B1ms 的 VM SKU 在本次订阅只读查询中不可用，不能只根据零售价决定 SKU。

两台 B2ls_v2 应用节点加一台 B2ats_v2 Redis 和 B1ms MySQL，仅计算部分约为 `0.1471/小时`（四小时 `0.5884`），**不是总价**。另计三块 30-GiB VM 磁盘、20-GiB 数据库存储、两个 Standard LB、出口公网 IP、流量、额外 IOPS/备份和可选监控。默认通过公网 LB 的明确 outbound rule 提供 SNAT，不使用 NAT Gateway，也不开放 SSH。实际部署 SKU 若不同，必须重新计算；Azure 区域容量、订阅折扣和税费也不由零售 API 保证。

创建资源前应给出最终 plan 对应的全量费用风险和预计保留时长。默认不保留长期部署；超过部署者已明确批准的预算、资源范围或保留时长必须取得新批准。

## State 与远程 backend

本项目默认本地 state，适合单人 Demo；并行操作同一 state 可能冲突。敏感值依然可能出现在 state 中，保护文件权限，不能上传到 Git。

迁移前备份本地 state 到受限存储。在 root module 增加一个选定的 backend 块，例如：

```hcl
terraform {
  backend "azurerm" {}
}
```

然后使用独立 backend 配置文件传入现有 Storage Account、container 和 key，再执行：

```bash
terraform -chdir=infra/terraform init -migrate-state -backend-config=backend.hcl
```

不要把 backend 凭据放入该配置文件；使用 Terraform backend 支持的环境认证。创建 backend 存储属于单独基础设施权限范围，不能在未授权 RG 自动创建。

迁移到 S3 或 GCS 时，将 backend 类型分别改为 `s3` 或 `gcs` 并提供各自配置、认证和锁策略；再次运行 `init -migrate-state`。backend 块不能引用普通 Terraform variables，不能通过业务变量动态切换云。

## 诊断

`429` 是有意限流，检查响应 `Retry-After`、拒绝原因及配置；`503` 应检查 Redis、ProxySQL/MySQL 连接和节点状态。网络连接故障不应静默切换到 local 模式。

TLS 故障检查数据库 CA、主机名、ProxySQL `use_ssl`、monitor 用户和业务用户 SSL 要求。Azure CA 变更时更新信任，不能简单关闭验证。`/readyz` 非 200 时 LB 将不再为该节点分配新连接，但已有连接仍可能失败或继续存活。

运行元数据和事件有 TTL，过期 run ID 应显示过期/不存在，不返回虚假空成功结果。控制台刷新恢复依靠 `run_id`，不依赖粘性会话。

当前开发机不在 Demo VNet 时，使用 `.venv/bin/python scripts/azure-verify.py`：它在 Redis VM 启动短时测试容器，测试请求经真实内部 LB；两个业务节点不获得 Azure 控制面凭据。故障操作由本地已授权 Azure 身份执行，只停止当前 state 中 node-1 的 ProxySQL 容器，20 秒后恢复。浏览器测试镜像固定为 Playwright Python `v1.58.0-noble`，结果经过压缩、分块和长度校验后保存到本地。ProxySQL 运维使用受限 `proxy.env`，发布完成仅删除数据库管理员临时 `admin.env`，避免后续统计收集失效。

Azure 部署后使用 `scripts/collect-results.sh --azure`，可加 `--base-url` 指向已授权的统一入口。收集器先核验精确 state 所有权，读取 VM/MySQL 状态及 LB `DipAvailability` 平台指标，再在两个节点执行固定的 ProxySQL stats 查询，保留原始场景 JSON。内部 HTTP 入口必须从已连通 VNet 的主机访问；资源统计已保存但 HTTP 不可达时会明确失败，不自动修改 VNet peering。刚部署的 LB 平台指标可能尚未产生数据，应等待采样后重新收集，不把空时间序列视为健康证明。
