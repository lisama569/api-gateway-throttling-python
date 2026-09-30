# API Gateway 统一对外能力层

为服务提供一致的版本协商、组合配额限流与错误归一化能力。所有入口
（`POST /orders`、`GET /orders/preview`）共用同一套前置判定
（`gateway_guard`），保证同一版本、同一调用方、同一异常在不同入口下
得到一致结论。

## 版本协商

- 调用方通过请求头 `X-Api-Version` 表达期望版本（如 `v1`、`v2`）。
- 未携带该头时使用默认版本 `v1`。
- 不受支持的版本一律拒绝：HTTP 400，`error.code = UNSUPPORTED_VERSION`，
  响应体附带 `supported_versions` 列表；版本协商先于配额判定，
  被版本拒绝的请求**不占用任何额度**。
- 判定逻辑集中在 `app/versioning.py::negotiate_version`，所有入口共用。

## 组合配额限流（调用方 × 版本 + 调用方总量）

实现在 `app/quota.py::QuotaManager`，默认配置在 `app/quota_config.py`。

### 维度与叠加规则

每个请求依次经过三个窗口维度，**全部有余量才放行，任一维度拒绝即整次拒绝**：

| 维度 | 计数键 | 含义 |
|---|---|---|
| `burst`（突发） | `(caller, version)` | 短时间突发容量 |
| `sustained`（持续平均） | `(caller, version)` | 较长一段时间的平均流量容量 |
| `caller_total`（调用方总量） | `(caller)` | 同一调用方跨所有版本的总容量上限 |

- **组合隔离**：额度按 `(调用方, 版本)` 组合独立记账。同一调用方的 v1、v2
  各用各的突发/持续额度，互不挤占；不同调用方之间同样互不影响。
- **总量兜底**：即使每个组合单独都没满，只要该调用方跨版本合计超过总量
  上限，请求也会被拒绝（`reason = caller_total`）。
- **跨入口一致**：计数键只与 `(caller, version)` 有关，与入口路径无关。
  同一组合经过 `/orders` 与 `/orders/preview` 时消费同一份额度。
- **拒绝不扣额**：判定先读取所有维度状态，全部有余量才在同一把锁内统一
  计数；被任一维度拒绝时不递增任何维度，被拒请求不占用其他维度的额度。
- **并发不超发**：窗口推进与计数都在同一把锁内完成，并发请求下实际放行数
  精确等于容量（测试覆盖 100 并发 / 总量 5，放行数恰好 5）。
- **自动恢复**：窗口结束后计数自动归零，无需重启或人工干预。

### 默认配置（`app/quota_config.py`）

```python
DEFAULT_COMBO_QUOTAS = (
    ComboQuota(caller=None, version="v1",
               burst=WindowSpec(capacity=5,  window_seconds=60.0),    # 5 次/60 秒
               sustained=WindowSpec(capacity=20, window_seconds=300.0)),  # 20 次/300 秒
    ComboQuota(caller=None, version="v2",
               burst=WindowSpec(capacity=5,  window_seconds=60.0),
               sustained=WindowSpec(capacity=20, window_seconds=300.0)),
)
DEFAULT_CALLER_TOTAL = WindowSpec(capacity=10, window_seconds=60.0)  # 跨版本 10 次/60 秒
```

- `caller=None` 表示该版本的**兜底配置**，对所有调用方生效；实际记账仍按
  各自的 `(caller, version)` 独立，不会因为共用配置而共享额度。
- 需要给特定调用方单独配额时，追加一条带 `caller="xxx"` 的 `ComboQuota`；
  同版本下指定调用方的配置优先于兜底配置。
- 默认 v1 的突发口径仍是 5 次/60 秒，与旧版按调用方限流的对外表现兼容。

### 两层窗口与重试语义

- 突发窗口控制短时尖峰，持续窗口控制一段时间内的平均流量；两层是"与"关系，
  任意一层耗尽都拒绝。
- 被拒响应包含 `error.reason`（`burst` / `sustained` / `caller_total`）与
  `error.retry_after_seconds`，并回写 HTTP 头 `Retry-After`。
- `retry_after_seconds` 取**所有同时被打满维度中最长的恢复等待时间**：
  等到该时刻后重试才可能成功（例如突发 0.6s 后恢复但总量还要 59.6s，
  则返回约 59.6s，提前重试仍会被总量拒绝）。

### 旧实现兼容

`app/ratelimit.py::RateLimiter`（按调用方的固定窗口）仍保留，原有单测与
构造方式不变；应用主链路已升级为 `QuotaManager`，对外错误结构、
`Retry-After` 头、`X-Caller-Id` / `X-Api-Version` 头语义保持不变。

## 失败诊断与请求关联标识

- 调用方可通过请求头 `X-Request-Id` 提供关联标识；缺省/为空/含非法字符时
  服务端生成 12 位十六进制标识（合法字符 `A-Za-z0-9._:-`，最长 128）。
- 同一次请求中，**响应头 `X-Request-Id`、响应体 `error.request_id`
  （成功时为顶层 `request_id`）、服务端日志三处是同一个值**，覆盖：
  成功、版本不被接受、被限流、参数格式错误、访问不存在的位置、
  业务拒绝、依赖失败、未预期异常。
- 调用方输入问题（请求体不是合法 JSON、参数类型/格式不对）明确按
  **客户端错误**处理：HTTP 400，`category = client_error`，
  `code = INVALID_REQUEST`，只返回参数位置与简短原因，不混入服务端
  内部异常（不会出现 500/internal_error），也不泄露内部上下文。
- 访问不存在的位置返回 HTTP 404，`code = NOT_FOUND`，同样携带 request_id。

所有异常归一化为稳定结构（`app/errors.py`）：

```json
{"error": {"category": "...", "code": "...", "message": "...", "request_id": "..."}}
```

| category | code | HTTP | 含义 |
|---|---|---|---|
| `client_error` | `INVALID_REQUEST` | 400 | 请求参数/请求体格式不合法（调用方侧错误） |
| `unsupported_version` | `UNSUPPORTED_VERSION` | 400 | 版本不受支持 |
| `rate_limited` | `RATE_LIMITED` | 429 | 配额超限（含 `reason`、`retry_after_seconds`、`Retry-After`） |
| `business_rejection` | `BUSINESS_REJECTION` | 422 | 业务规则拒绝（如数量非法、库存不足） |
| `dependency_failure` | `DEPENDENCY_FAILURE` | 502 | 下游依赖失败 |
| `internal_error` | `INTERNAL_ERROR` | 500 | 未预期异常 |
| `client_error` | `NOT_FOUND` / `METHOD_NOT_ALLOWED` | 404 / 405 | 位置不存在 / 方法不允许 |

内部异常细节（堆栈、地址、敏感字段）不透出给调用方，仅随 `request_id`
记录在服务端日志中；调用方可凭 `request_id` 定位。同一原因在不同入口下
得到相同分类与结构。

## 本地验证

```bash
# 安装依赖
uv sync

# 运行全部测试（既有版本协商/限流/异常用例 + 组合配额/诊断新用例）
uv run pytest -q

# 启动服务
uv run uvicorn main:app --reload

# 正常请求：观察响应头与响应体 request_id 相同
curl -i -X POST localhost:8000/orders \
  -H 'Content-Type: application/json' \
  -H 'X-Caller-Id: demo' -H 'X-Api-Version: v2' -H 'X-Request-Id: my-rid-1' \
  -d '{"item": "book", "quantity": 1}'

# 触发版本拒绝（不计配额）
curl -i -X POST localhost:8000/orders -H 'X-Api-Version: v9' \
  -H 'Content-Type: application/json' -d '{"item": "book", "quantity": 1}'

# 触发客户端错误（非法 JSON，400 client_error）
curl -i -X POST localhost:8000/orders \
  -H 'Content-Type: application/json' -d '{bad-json'

# 触发 404
curl -i localhost:8000/no/such/place

# 触发组合限流（同一 caller 连续 6 次以上；注意 Retry-After 与 reason）
for i in $(seq 1 8); do curl -s -o /dev/null -w '%{http_code}\n' \
  -X POST localhost:8000/orders -H 'X-Caller-Id: demo' \
  -H 'Content-Type: application/json' -d '{"item": "book", "quantity": 1}'; done

# 触发三类错误：quantity=0（业务拒绝）、item=fragile（依赖失败）、item=boom（未预期异常）
curl -s -X POST localhost:8000/orders -H 'Content-Type: application/json' \
  -d '{"item": "boom", "quantity": 1}'
```

### 测试覆盖说明

- `tests/test_gateway.py`：版本协商、旧限流口径、异常归类（不回归）。
- `tests/test_quota.py`：组合按版本/调用方隔离、突发与持续两层精确判定、
  多维度同时打满时的 retry_after、拒绝不扣额度、窗口自动恢复、
  调用方总量并发不超发。
- `tests/test_quota_http.py`：HTTP 层组合隔离、跨入口同一份额度、
  总量跨版本兜底、版本拒绝不扣额度。
- `tests/test_diagnostics.py`：各成功/失败路径响应头与响应体 request_id
  一致、服务端生成/校验 request_id、非法 JSON 与参数类型错误归类为
  client_error、404 可追踪、日志可按 request_id 串联。
