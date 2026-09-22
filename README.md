# API Gateway 统一对外能力层

为服务提供一致的版本协商、限流与错误归一化能力。所有入口（`POST /orders`、
`GET /orders/preview`）共用同一套前置判定（`gateway_guard`），保证同一版本、
同一调用方、同一异常在不同入口下得到一致结论。

## 版本协商

- 调用方通过请求头 `X-Api-Version` 表达期望版本（如 `v1`、`v2`）。
- 未携带该头时使用默认版本 `v1`。
- 不受支持的版本一律拒绝：HTTP 400，`error.code = UNSUPPORTED_VERSION`，
  响应体附带 `supported_versions` 列表。
- 判定逻辑集中在 `app/versioning.py::negotiate_version`，所有入口共用，
  同一版本在不同入口下判定一致。

## 限流口径

- 按调用方身份限流：身份取自请求头 `X-Caller-Id`，缺省为 `anonymous`。
- 算法：固定窗口计数，应用级默认 `limit=5 / 60s`（见 `main.py`），
  核心实现在 `app/ratelimit.py::RateLimiter`。
- 计数与窗口推进在同一把锁内完成，并发请求下额度不超发（测试：
  100 并发 / 限额 20，放行数精确等于 20）。
- 超限返回 HTTP 429，响应头带 `Retry-After`，响应体带
  `error.retry_after_seconds`，调用方可据此安排重试。
- 窗口结束后额度自动恢复，无需外部干预。

## 错误分类

所有异常归一化为稳定结构（`app/errors.py`）：

```json
{"error": {"category": "...", "code": "...", "message": "...", "request_id": "..."}}
```

| category | code | HTTP | 含义 |
|---|---|---|---|
| `business_rejection` | `BUSINESS_REJECTION` | 422 | 业务规则拒绝（如数量非法、库存不足） |
| `dependency_failure` | `DEPENDENCY_FAILURE` | 502 | 下游依赖失败 |
| `internal_error` | `INTERNAL_ERROR` | 500 | 未预期异常 |
| `rate_limited` | `RATE_LIMITED` | 429 | 限流超限（含重试信息） |
| `unsupported_version` | `UNSUPPORTED_VERSION` | 400 | 版本不受支持 |

内部异常细节（堆栈、地址、敏感字段）不透出给调用方，仅随 `request_id`
记录在服务日志中；调用方可凭 `request_id` 定位。同一原因在不同入口下
得到相同分类与结构。

## 本地验证

```bash
# 安装依赖
uv sync

# 运行测试（日志会打印请求标识与判定依据）
uv run pytest -q

# 启动服务
uv run uvicorn main:app --reload

# 手动验证
curl -i -X POST localhost:8000/orders \
  -H 'Content-Type: application/json' \
  -H 'X-Caller-Id: demo' -H 'X-Api-Version: v2' \
  -d '{"item": "book", "quantity": 1}'

# 触发版本拒绝
curl -i -X POST localhost:8000/orders -H 'X-Api-Version: v9' \
  -H 'Content-Type: application/json' -d '{"item": "book", "quantity": 1}'

# 触发限流（连续 6 次以上）
for i in $(seq 1 8); do curl -s -o /dev/null -w '%{http_code}\n' \
  -X POST localhost:8000/orders -H 'X-Caller-Id: demo' \
  -H 'Content-Type: application/json' -d '{"item": "book", "quantity": 1}'; done

# 触发三类错误：quantity=0（业务拒绝）、item=fragile（依赖失败）、item=boom（未预期异常）
curl -s -X POST localhost:8000/orders -H 'Content-Type: application/json' \
  -d '{"item": "boom", "quantity": 1}'
```
