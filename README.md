# API Gateway 统一对外能力层

为服务提供一致的版本协商、组合配额限流、错误归一化与失败诊断能力。所有入口
（`POST /orders`、`GET /orders/preview`）共用同一套前置判定（`gateway_guard`），
保证同一版本、同一调用方、同一异常在不同入口下得到一致结论。

## 版本协商

- 调用方通过请求头 `X-Api-Version` 表达期望版本（如 `v1`、`v2`）。
- 未携带该头时使用默认版本 `v1`。
- 不受支持的版本一律拒绝：HTTP 400，`error.code = UNSUPPORTED_VERSION`，
  响应体附带 `supported_versions` 列表。
- 判定逻辑集中在 `app/versioning.py::negotiate_version`，所有入口共用，
  同一版本在不同入口下判定一致。

## 组合配额限流（调用方 × 版本 + 总量上限）

实现在 `app/quotas.py::QuotaLimiter`，一次请求必须同时通过下面三层口径才放行；
任一维度拒绝都直接返回，**不会占用任何其他维度的额度**：

| 维度 | 分桶 key | 含义 |
|---|---|---|
| 组合突发窗口 `combo_burst` | `(caller, version)` | 限制短时间突发：`burst_window_seconds` 内最多 `burst.capacity` 次 |
| 组合持续窗口 `combo_sustained` | `(caller, version)` | 限制持续平均流量：`sustained_window_seconds` 内最多 `sustained.capacity` 次 |
| 调用方总量 `caller_total` | `caller` | 跨所有版本合计，一个持续窗口内最多 `caller_total` 次 |

叠加规则：

- **同一调用方的不同版本各用各的组合额度，互不挤占**；但它们共享该调用方的
  总量上限——任何组合单独没满、只要跨版本总量超了，照样拒绝（响应中
  `rejected_by=["caller_total"]`）。
- 不同调用方之间完全隔离。
- 同一调用方同一版本无论走哪个入口，命中的都是同一份组合额度。
- 计数、判定、扣减在同一把锁内完成，并发下放行数**精确等于**配置容量，
  不超发（测试覆盖 200 并发 / 总量 25，放行数恰为 25）。
- 窗口区间为 `[start, start + window)`，窗口结束后额度自动恢复，
  无需重启或人工干预；总量窗口与持续窗口等长、同步恢复。

### 默认值与配置方式

默认口径与旧版"每调用方 5 次/60s"**保持兼容**：

| 配置 | 环境变量 | 默认值 |
|---|---|---|
| 突发容量 | `GATEWAY_BURST_LIMIT` | 5 |
| 突发窗口（秒） | `GATEWAY_BURST_WINDOW_SECONDS` | 10.0 |
| 持续容量 | `GATEWAY_SUSTAINED_LIMIT` | 5 |
| 持续窗口（秒） | `GATEWAY_SUSTAINED_WINDOW_SECONDS` | 60.0 |
| 调用方总量 | `GATEWAY_CALLER_TOTAL_LIMIT` | 5 |

默认突发容量与持续容量相同，因此稳态行为仍是"5 次/60s"；需要压制短时突发时，
把 `GATEWAY_BURST_LIMIT` 调小即可。代码内可用 `QuotaConfig` 为每个
`QuotaLimiter` 实例单独配置，例如只给某类入口更严的配额。

### 重试语义（两层窗口）

超限返回 HTTP 429，响应头 `Retry-After` 与响应体
`error.retry_after_seconds` 同值；`error.rejected_by` 标明被哪些维度拒绝。

- 只被突发窗口拒绝时：等到当前突发窗口结束即可重试（通常只需秒级）。
- 同时被持续窗口/总量拒绝时：`retry_after_seconds` 取**所有被拒维度中恢复
  最晚的那个时刻**，调用方等到该时刻之后重试才可能成功；提前重试只会继续被拒，
  且被拒请求不扣额度，不会让恢复时刻后移。

## 失败诊断与请求关联标识

每个请求在最外层中间件获得一个服务端认可的关联标识：

- 调用方传 `X-Request-Id` 时沿用；不传时服务端生成 12 位十六进制 ID。
- **无论成功、版本不被接受、被限流、参数格式不对，还是访问不存在的位置**，
  响应头 `X-Request-Id` 与响应体中的 `error.request_id`（成功时为顶层
  `request_id`）始终是同一个值。
- 服务端每一步日志（版本协商、配额判定、错误归类）都携带该 ID，
  可用它在日志中串起同一次请求。

### 错误分类

所有错误归一化为稳定结构（`app/errors.py`）：

```json
{"error": {"category": "...", "code": "...", "message": "...", "request_id": "..."}}
```

| category | code | HTTP | 含义 |
|---|---|---|---|
| `business_rejection` | `BUSINESS_REJECTION` | 422 | 业务规则拒绝（如数量非正、库存不足、未知商品） |
| `invalid_request` | `INVALID_REQUEST` | 422 | **调用方侧**输入问题：JSON 非法、字段类型不对（含字段明细 `details`） |
| `dependency_failure` | `DEPENDENCY_FAILURE` | 502 | 下游依赖失败（服务端侧） |
| `internal_error` | `INTERNAL_ERROR` | 500 | 未预期异常（服务端侧） |
| `rate_limited` | `RATE_LIMITED` | 429 | 配额超限（含 `rejected_by` 与重试信息） |
| `unsupported_version` | `UNSUPPORTED_VERSION` | 400 | 版本不受支持 |
| `not_found` | `NOT_FOUND` | 404 | 访问了不存在的位置 |

调用方输入问题（格式、类型）一律按 `invalid_request` 的 4xx 处理，不会混进
服务端 `internal_error`；但"字段齐全、取值不满足业务规则"（如 `quantity=0`）
仍按旧行为归类为 `business_rejection`，保持兼容。内部异常细节（堆栈、地址、
敏感字段）依旧不透出，仅随 `request_id` 进入服务端日志。

## 本地验证

```bash
# 安装依赖
uv sync

# 运行全部测试（日志会打印请求标识与判定依据）
uv run pytest -q

# 连续跑两遍，确认无跨用例状态污染
uv run pytest -q && uv run pytest -q

# 启动服务
uv run uvicorn main:app --reload

# 用更严格的配额启动，便于手动观察突发/持续两层拒绝
GATEWAY_BURST_LIMIT=2 GATEWAY_SUSTAINED_LIMIT=5 \
GATEWAY_CALLER_TOTAL_LIMIT=5 uv run uvicorn main:app --reload

# 手动验证
curl -i -X POST localhost:8000/orders \
  -H 'Content-Type: application/json' \
  -H 'X-Caller-Id: demo' -H 'X-Api-Version: v2' \
  -d '{"item": "book", "quantity": 1}'

# 触发版本拒绝（响应头与响应体 request_id 一致）
curl -i -X POST localhost:8000/orders -H 'X-Api-Version: v9' \
  -H 'Content-Type: application/json' -d '{"item": "book", "quantity": 1}'

# 触发限流：观察 X-Request-Id、Retry-After 与 rejected_by
for i in $(seq 1 8); do curl -s -X POST localhost:8000/orders \
  -H 'X-Caller-Id: demo' -H 'Content-Type: application/json' \
  -d '{"item": "book", "quantity": 1}' | head -c 400; echo; done

# 触发调用方输入错误（422 invalid_request）
curl -i -X POST localhost:8000/orders -H 'Content-Type: application/json' \
  -H 'X-Caller-Id: demo' -d '{"item": "book", "quantity": "x"}'

# 访问不存在的位置（404 not_found，同样带 X-Request-Id）
curl -i localhost:8000/no/such/path

# 触发三类既有错误：quantity=0（业务拒绝）、item=fragile（依赖失败）、item=boom（未预期异常）
curl -s -X POST localhost:8000/orders -H 'Content-Type: application/json' \
  -d '{"item": "boom", "quantity": 1}'
```
