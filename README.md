# API Gateway Throttling（统一对外能力层）

为不同调用方提供一致且可解释的 **版本协商、按身份限流、错误归一化** 能力。
所有入口（`/api`、`/internal`）共享同一套策略，由统一中间件裁定，
因此同一原因在任何入口下得到相同结论。

## 1. 版本协商规则

- 约定位置：请求头 **`X-API-Version`**（大小写不敏感）。
- **未携带** 该头：使用默认版本 **`v1`**。
- 携带为空 / 全空白 / 不在支持列表：明确拒绝（HTTP `400`，
  分类 `version_unsupported`），响应中给出 `requested_version`、
  `supported_versions`、`default_version` 便于调用方纠正。
- 当前支持：`v1`、`v2`（值大小写不敏感，统一归一化为小写）。
- 判定逻辑集中在 `VersionNegotiator.decide` 单一函数，HTTP 与未来其他入口
  复用同一判定，保证同一版本表达在不同入口下结论一致。

```
$ curl -s -H 'X-API-Version: v9' localhost:8000/api/echo
{"error":{"category":"version_unsupported","message":"unsupported api version",
"request_id":"...","details":{"requested_version":"v9",
"supported_versions":["v1","v2"],"default_version":"v1","requested_at":"x-api-version"}}}
```

## 2. 限流口径

- 身份位置：请求头 **`X-Caller-Id`**；未提供时归入固定身份 `anonymous`
  （未认证流量同样受限）。
- 算法：**按调用方的固定时间窗（fixed window）**。默认口径
  **每个调用方 60 秒内 10 次请求**（`gateway.config.default_config`）。
- 并发安全："检查额度 → 计数" 在同一把 `asyncio.Lock` 临界区内完成，
  并发请求绝不超发（测试中 50 并发 / 5 额度，恰好 5 个放行）。
- 自动恢复：窗口结束后的下一次请求开启新窗口，额度自动恢复为满额。
- 超限与正常请求可区分：HTTP `429` + 分类 `rate_limited`，并给出可操作重试信息：
  - 标准响应头 `Retry-After`（秒，整数）；
  - 限流头 `X-RateLimit-Limit` / `X-RateLimit-Remaining` / `X-RateLimit-Reset`；
  - 结构化字段 `error.details.retry_after` / `reset_in` / `limit`。
- 放行响应同样携带 `X-RateLimit-*` 头，调用方可据此主动退避。

## 3. 错误分类与统一结构

所有错误（含框架自身的 404、请求体校验失败）归一化为：

```json
{
  "error": {
    "category": "rate_limited",
    "message": "rate limit exceeded",
    "request_id": "由 X-Request-Id 传入或服务端生成",
    "details": { "retry_after": 42.5, "reset_in": 42.5, "limit": 10 }
  }
}
```

响应头恒回 `X-Request-Id`，便于调用方报障与服务端日志对账。

| category               | HTTP | 含义与来源                                     | 对外消息策略           |
|------------------------|------|------------------------------------------------|------------------------|
| `bad_request`          | 4xx  | 路由不存在、请求体校验失败等协议/参数问题       | 仅必要的定位信息       |
| `version_unsupported`  | 400  | 版本协商失败（含空值）                          | 附支持列表与默认版本   |
| `rate_limited`         | 429  | 超出调用方窗口额度                              | 附 retry_after 等      |
| `business_rejected`    | 422  | 业务规则拒绝（如商品不可下单）                  | 可暴露业务原因码       |
| `dependency_failure`   | 503  | 下游依赖失败（连接拒绝/超时等）                 | 固定文案，细节不外泄   |
| `internal_error`       | 500  | 未预期异常                                      | 固定文案，堆栈不外泄   |

安全约定：依赖异常文本、异常类型名、堆栈、连接串、密钥等只写入服务端日志，
**不出现在响应体/消息中**（测试对 `s3cr3t`、`connection refused`、
`RuntimeError` 等做了不透出断言）。

每次关键判定均以 `gateway` logger 输出，包含请求标识与判定依据，例如：

```
gateway decision request_id=rl-00 reason=rate_allowed caller=hot remaining=2
gateway decision request_id=rl-07 reason=rate_limited caller=hot limit=3 retry_after=59.998
gateway decision request_id=err-boom-1 reason=unexpected_exception caller=anonymous error_type=RuntimeError
```

## 4. 入口与演示端点

| 方法 & 路径                  | 说明                                        |
|------------------------------|---------------------------------------------|
| `GET  /api/echo`             | 回显调用方/协商版本（正常请求）              |
| `GET  /api/remotes/{name}`   | 调用下游；`name=broken` 触发依赖失败         |
| `POST /api/orders`           | 下单；`item=frozen` 触发业务拒绝             |
| `GET  /internal/echo`        | 另一入口的 echo，策略与 /api 一致            |
| `GET  /internal/remotes/{name}` | 另一入口的下游调用（同因同果）            |
| `GET  /internal/boom`        | 抛未预期异常，演示 INTERNAL_ERROR 兜底       |
| `GET  /health`               | 健康检查（不受网关策略约束）                 |

## 5. 本地验证方法

```bash
# 安装依赖（uv）
uv sync

# 运行全部单测（26 个）；gateway 判定日志默认实时打印（pytest live log）
uv run pytest -q

# 仅看并发超发 / 版本拒绝 / 异常归类
uv run pytest tests/test_ratelimit.py tests/test_versioning.py tests/test_errors.py -q

# 启动服务
uv run uvicorn main:app --reload --port 8000
```

手动验证示例：

```bash
# 默认版本
curl -s localhost:8000/api/echo
# 指定版本
curl -s -H 'X-API-Version: v2' -H 'X-Caller-Id: partner-a' localhost:8000/internal/echo
# 版本被拒
curl -s -H 'X-API-Version: v9' localhost:8000/api/echo
# 连续压测同一调用方，观察 429 与 Retry-After
for i in $(seq 1 12); do
  curl -s -o /dev/null -w "%{http_code} " -H 'X-Caller-Id: stress' localhost:8000/api/echo
done
# 三类异常归类
curl -s -X POST localhost:8000/api/orders -H 'Content-Type: application/json' -d '{"item":"frozen","qty":1}'
curl -s localhost:8000/api/remotes/broken
curl -s localhost:8000/internal/boom
```

## 6. 代码结构

```
gateway/
  config.py       # 版本/限流口径与默认配置
  versioning.py   # 版本协商（decide 为与传输无关的唯一判定入口）
  ratelimit.py    # 并发安全的按调用方固定窗口限流器
  errors.py       # 错误分类、异常层级、统一错误体
  middleware.py   # 请求标识→版本→限流→异常归一化；框架异常处理器
  dependencies.py # 下游依赖模拟（可注入失败）
  routes.py       # /api 与 /internal 多入口路由
  app.py          # 组装
main.py           # uvicorn 入口
tests/            # 版本拒绝 / 并发超发 / 异常归类测试
```
