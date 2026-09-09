# 测试与验收

## 本地命令

```bash
make install
source .venv/bin/activate
python -m pytest
ruff check .
ruff format --check .
make demo-local
make test
make test-demo
python -m playwright install chromium
make test-web
# Include the Azure frontend certificate contract test without deploying Azure:
DOCKER='sudo -n docker' AZURE_COMPOSE_TLS_TEST=1 DEMO_BASE_URL=http://127.0.0.1:8080 make test
```

若当前用户没有 Docker socket 权限，但已有非交互 sudo Docker 权限，可使用 `DOCKER='sudo -n docker' make demo-local`；无需修改用户组或系统服务。

Redis 集成测试使用真实 Redis，测试连接与临时 key 空间独立于运行中的 Demo。Web smoke 通过真实浏览器打开控制台，启动超阈值场景，确认成功和拒绝、刷新恢复及 JSON 下载；不能用 API mock 替代此链路。运行中的本地容器用于真实 MySQL/ProxySQL/TLS 验证。

本地启动后 `make test` 自动读取本任务 `.env`，将 Redis 测试连接指向数据库 15，测试 key 使用唯一前缀。使用独立 Redis 时通过 `TEST_REDIS_URL` 注入，不要将连接串输出到终端或提交。单独运行 `python -m pytest` 且没有可用 Redis 时会显示依赖 Redis 的测试跳过；这不能替代集成验收。CI 先启动容器，再执行真实 Redis 测试、五个场景和浏览器 smoke，不获得云故障注入权限。

## 验收矩阵

| 场景 | 关键断言 |
| --- | --- |
| QPS 阈值内 | 接受请求，正常情况下无 429/503 |
| QPS 阈值外 | 同时存在 200 和 429；成功数符合 rate + burst 包络 |
| 并发阈值内 | 正常完成，执行占用不超过配置 |
| 并发阈值外 | 有排队/429；数据库异常、超时、取消均释放令牌 |
| 两实例 distributed | 两节点均处理请求，全局配额不翻倍 |
| local 对照 | 两节点各自计数，不能宣称全局上限 |
| 场景 API | 未知 ID、越界参数、重复取消、过期和运行总数上限 |
| SSE | 单调事件 ID、重连/跨节点读取、最终完成事件 |
| 结果 | 原始状态码计数一致，percentile 可重复验证，schema_version 固定 |
| 故障转移 | 实测短暂失败、恢复时间、另一节点流量、原节点重新 ready |

负载产生器的请求并发与 SQL 执行并发应区分；高于上限的压测客户端数本身不能证明后端超额执行。图表只作可视化，验收以结果 JSON、服务器指标和 ProxySQL stats 为准。

## Terraform 与 Azure

```bash
make terraform-check
bash scripts/plan.sh
```

检查 plan 没有意外删除、公开数据库和敏感普通 output；验证后删除本地 plan。真正部署和 Azure 故障转移必须在收到明确 `DEPLOY` 后执行。本地模拟不能替代 Azure MySQL 连通、Azure LB probe 和实际节点恢复证据。

压测结果位于 `artifacts/`，不提交真实运行产物。`scripts/collect-results.sh` 将原始同 schema 的结果与安全投影后的 ProxySQL stats、健康状态、Prometheus 指标汇总，保留原始运行计数。
