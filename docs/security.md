# 演示边界与操作权限

## 入口与数据

`POST /query` 只接受服务端已定义的 query ID 和参数化查询。场景只能使用有界持续时间、QPS 和并发；页面不允许任意 URL、SQL、Redis key 或系统命令。该控制台能触发真实负载，不应暴露给不可信用户。

本地 HTTP、Redis 测试端口和直接 MySQL 入口仅绑定 loopback。Azure MySQL 默认私网，ProxySQL 管理端口仅 loopback，HTTP 默认私网；启用公网时必须设置受限 CIDR。HTTP 本身不是 TLS 终结，公网使用还应通过受控 HTTPS 网关及身份认证，不能视为生产 Web 服务。

本地应用到 ProxySQL 使用本任务生成的 TLS 证书并验证节点主机名；ProxySQL 到 MySQL 使用独立 CA 和 TLS，数据库业务/monitor 用户强制 SSL。Azure 发布时也使用独立的 ProxySQL 前端证书和数据库 CA。本地私钥保存在受限 `.local/`，不提交；演示证书有效期 30 天，过期前需停机更新。CA 信任必须随 Azure MySQL 根证书更新维护。

生成的本地 `.env` 只保存本任务随机密码且权限为 `600`。不提交真实 subscription/tenant ID、token、密码、state、plan、日志或压测数据。Terraform 的 `sensitive` 仅抑制显示，不对 state 加密；本地 state 文件应限制权限，远程 backend 应设置独立 RBAC、加密和锁。

## 配额与可用性

QPS 和并发限制是独立演示；直接访问 `6033` 不经过 HTTP limiter。Redis 原子配额保证正常依赖运行时的共享准入，不能代替生产分布式容错协议。Redis 故障不应自动降级到 local；并发租约的 TTL 必须大于最大执行时间并留有余量，防止活跃任务尚未结束就重用配额。

默认禁用 Web 故障注入；管理脚本只操作当前 Terraform state 中本项目节点的 ProxySQL 服务。不得将 Azure 凭据或 Docker socket 提供给普通 Web 服务。客户端取消和服务关闭应清理压测子任务；运行状态和事件均有 TTL。

## 云资源

Demo 在共享 `rg-dev2` 中创建独立资源，资源组作为 data source，不能被 destroy。每次部署都核验目标订阅；创建收费资源前必须审核 plan、成本和 state，再明确输入 `DEPLOY`。

删除仅限当前 state 记录且属于项目的资源；显示清单并确认完整项目标识，不使用标签查询扩大范围。部署前由部署者明确授权目标资源范围、费用预算和保留时长，超出授权范围时必须重新取得批准。VM 停机仍可能产生磁盘/IP 等费用，验证后应按明确任务清单清理。

ProxySQL 是独立开源组件；Azure MySQL 服务支持不等于微软提供 ProxySQL 运维支持。生产化需要自行验证 ProxySQL/MySQL 认证插件、TLS、升级兼容，建立补丁维护、备份恢复、身份授权、Redis HA、配额审计和告警机制。
