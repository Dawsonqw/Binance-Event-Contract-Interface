# 自动化框架接入：交易接口 / 查询接口 v1

> **风险声明**：币安事件合约没有已验证的官方交易 API。下单仍使用未公开接口，尚未验证真实成交。查询部分已验证 Binance Web 域名上的两个非官方、只读订单列表接口（仅第一页）；本地提交记录与币安返回的订单列表必须严格区分，不能据此断言完整对账。

## 架构

```text
其他策略 / 量化框架
   |  Python SDK（本地）或 HTTP + Bearer（本机 / SSH 隧道）
   v
event_gateway.EventContractGateway
   |-- .trading.submit_order(OrderRequest) -> safe_orders.place_order_web
   |-- .queries.get_session() -> session_lifecycle
   |-- .queries.get_order() / .list_orders() -> SQLite（本地提交记录）
   |-- .queries.list_ongoing_orders() -> Binance Web 只读未结算列表
   |-- .queries.list_closed_orders() -> Binance Web 只读已结算列表
   '-- .queries.get_quote() / .get_exchange_order() / .get_settlement()
             未验证：抛出 RemoteQueryUnavailable / HTTP 501
```

## Python SDK 示例

```python
from event_gateway import EventContractGateway, OrderRequest

gateway = EventContractGateway()
print(gateway.queries.capabilities())
print(gateway.queries.get_session())
print(gateway.queries.list_orders(limit=20))
record = gateway.queries.get_order('strategy-2026-11-21-0001')

# 下列仅演示参数格式。真实调用可能执行真实资金交易。
# 首先要有用户本人确认的登录凭证，并显式设置实盘开关。
request = OrderRequest(
    intent_id='strategy-2026-11-21-0001',
    orderAmount='5',
    timeIncrements='TEN_MINUTE',
    symbolName='BTCUSDT',
    payoutRatio='0.80',  # 仅格式示例，不是实时赔率！
    direction='LONG',
)
# gateway.trading.submit_order(request)  # 默认禁用，切勿随意取消注释
```

## Binance 真实订单列表：已验证只读实现

默认 `EventContractGateway()` 已注入 `BinanceEventQueryProvider`，支持：

- Python：`gateway.queries.list_ongoing_orders(page=1)`、`gateway.queries.list_closed_orders(page=1)`。
- HTTP：`GET /v1/exchange/orders/ongoing?page=1`、`GET /v1/exchange/orders/closed?page=1`。
- 内部固定访问 `https://www.binance.com`：未结算查询 `/bapi/futures/v2/private/future/event-contract/ongoing-order-list`，已结算查询 `/bapi/futures/v1/private/future/event-contract/closed-order-list`。两者使用 **HTTP POST + 空 JSON `{}`** 执行只读列表查询，绝不调用 `place-order`。
- 认证凭证仅在服务器端从私有目录读取，禁止日志打印；不将 Cookie 传给客户端或任何其他域名。
- 仅接受 HTTP 200、`success: true`、业务码 `000000`；拒绝重定向/WAF、超时或畸形 JSON。上游异常返回 HTTP 502，登录凭证失效/过期返回 HTTP 409。
- 兼容两种不同响应结构：未结算 `data.result.rows / total`；已结算 `data.rows / total`。只保留已观察到的订单字段，包括 `id`、`symbolName`、`direction`、`openPrice`、`closePrice`、`orderAmount`、`payoutRatio`、`settlementAmount` 等。
- 输出 `kind`、`source`、`page`、`returned_count`、`total`、`has_more`、`pagination_supported`、`fetched_at_utc` 和 `items`。

**分页暂未验证。** 截至 2026-11-21，已验证的默认已结算订单查询返回 `total=326`、`returned_count=10`，但没有确认 Binance 内部分页参数；因此只支持 `page=1`。请求 `page>1` 会返回 HTTP 501，不能将第一页 10 条误认为全部历史。该账户的这两个列表查询已在远程服务器以只读方式复测成功。

**仍不支持** 任意订单 ID 精确检索、实时赔率、独立结算查询和账户资金流水自动对账。即便订单来自“已结算列表”，也不等于本系统已独立完成资金流水核对。

SDK 仍支持 `EventContractGateway(query_provider=MyVerifiedProvider())` 注入将来验证的替代 Provider；需要根据实际能力准确实现 `capabilities()`，不能伪造已支持的接口。

**Python SDK 兼容性提醒**：现在 `TradingInterface.submit_order()` 也是**默认非阻塞**的，与 `submit_order_async()` 等价；需要显式传入 `dispatcher=FastOrderDispatcher()`。旧同步路径仅保留为 `submit_order_sync_legacy()`，不应在实时策略中调用。接口行为变化会影响之前假设“调用返回即交易成功”的代码，迁移时必须调整为异步状态轮询。

## 非阻塞下单：2026-11 新版本

**POST /v1/orders 已改为非阻塞语义（有意改变旧行为）**：服务端首先同步执行 SQLite WAL 持久化和按 `intent_id` 唯一去重，然后立即返回 HTTP **202 + 状态 QUEUED**，后台只有一个发送工作线程按 FIFO 发出网络请求。*HTTP 202 只表示本地持久受理，不代表币安已经接受或成交*。

发单流程：`策略信号 -> 持久化幂等 ID -> 快速回 202 -> 单独后台工作线程 -> HTTP 下单 -> 本地结果状态`。策略不需要等待交易所；可按 `GET /v1/orders/{intent_id}` 查询异步结果。Python 直接调用使用 `EventContractGateway(dispatcher=FastOrderDispatcher()).trading.submit_order_async(order)`，必须由同一进程持有并关闭 Dispatcher。

### 信号时效和超时语义

- `signal_time_ms`：可选，由策略提供**Unix 时间戳毫秒整数**；如果不传，则以 API 接收时为起点，**无法发现策略本身已延迟的信号**。建议策略务必传入。
- `max_age_ms`：可选，默认 1000ms，可设置 50–5000ms。超过截止时间且**未开始网络请求**，记为 `DROPPED_STALE_BEFORE_SEND`，确认不会发送。
- HTTP 使用可复用的 `requests.Session` 连接池。默认连接超时 0.7 秒、响应读取超时 1.3 秒，**不是严格的总耗时上限**；TLS、DNS 和调度还可能增加耗时。
- 网络开始后才超时，记为 `OUTCOME_UNKNOWN`：不等于币安未收到，不自动补单，更不能换个 `intent_id` 盲目重发。
- 重启不会自动重放旧订单：原 `QUEUED` 标记为 `DROPPED_STALE_BEFORE_SEND`，原 `DISPATCHING` 标记为 `OUTCOME_UNKNOWN`。
- 同一 ID 重复调用返回现有状态、`already_exists: true`，不会再次发出；同一 ID 携带不同业务参数返回 HTTP 409。
- 默认队列容量 128；超载会快速拒绝未受理的信号。单发送线程避免多线程争用同一登录凭证。
- sqlite 是**本机**幂等库，不保证不同机器、不同 ID、其他人工/App 下单的全局去重。

示例（只有用户显式开启实盘才可能提交；请不要直接执行）：

```json
{
  "intent_id":"signal-20261121-001",
  "orderAmount":"5",
  "timeIncrements":"TEN_MINUTE",
  "symbolName":"BTCUSDT",
  "payoutRatio":"0.80",
  "direction":"LONG",
  "signal_time_ms":1795260000000,
  "max_age_ms":1000
}
```

上述时间戳与赔率都是**格式示例**，实际策略必须填写当前信号和真实报价。不要把示例 0.80 当实时赔率。

### 非阻塞订单查询字段

`GET /v1/orders/{intent_id}` 返回 SQLite 记录，重要状态：
`QUEUED`、`DISPATCHING`、`DROPPED_STALE_BEFORE_SEND`、
`REJECTED_AUTH_BEFORE_SEND`、`REJECTED_DISABLED_BEFORE_SEND`、
`ACKNOWLEDGED_NOT_SETTLEMENT_VERIFIED`、`OUTCOME_UNKNOWN`。

所有记录都带有 `exchange_verified:false`、`final_settlement_verified:false`、`safe_to_auto_retry:false`。这三者不会因为拿到订单号而自动置真。

`GET /v1/orders` 使用 SQLite 索引查询**本地提交记录**（默认 50，最多 100）；旧文件记录仍可按 `intent_id` 单独读取。交易所订单列表则使用单独的 `/v1/exchange/orders/ongoing` 与 `/v1/exchange/orders/closed`，并且**目前仅支持第一页**。实时赔率、任意 ID 订单详情和独立结算接口仍为 HTTP 501。

## 本机 HTTP API（标准库，无额外 Web 框架依赖）

绑定 **127.0.0.1:18765**，不能直接绑定 0.0.0.0。所有路由必须提供 `Authorization: Bearer <API_TOKEN>`，API_TOKEN 至少 32 个 ASCII 字符。API_TOKEN 与币安 `p20t` 是两个独立秘密：**不得将币安 Token 传给外部客户端。**

| 方法 | 路由 | 功能 | 状态 |
|---|---|---|---|
| GET | `/v1/health` | 本地服务状态、实盘开关状态 | 已实现 |
| GET | `/v1/capabilities` | 能力发现，不支持项明确标记 | 已实现 |
| GET | `/v1/session` | 本地登录时间戳状态（非交易所实时验证） | 已实现 |
| GET | `/v1/orders?limit=50` | 本地提交审计列表 | 已实现 |
| GET | `/v1/orders/{intent_id}` | 按业务幂等 ID 查询本地提交 | 已实现 |
| POST | `/v1/orders` | 真实下单入口，**默认关闭** | 非官方接口，未实盘验证 |
| GET | `/v1/exchange/orders/{order_id}` | 币安真实订单状态 | HTTP 501，不支持 |
| GET | `/v1/quotes?symbolName=BTCUSDT&timeIncrements=TEN_MINUTE` | 实时赔率 | HTTP 501，不支持 |
| GET | `/v1/settlements/{order_id}` | 结算结果 | HTTP 501，不支持 |

服务端运行示例（必须以 binanceevt 等非 root 用户执行，变量应从受限文件载入）：

```bash
export BINANCE_EVENT_API_TOKEN='<generate-a-random-32-plus-char-token>'
python -m http_api --port 18765
```

仅查询调用示例（不要把 API_TOKEN 写入日志或 Git 仓库）：

```bash
curl -sS -H "Authorization: Bearer $BINANCE_EVENT_API_TOKEN" \
  http://127.0.0.1:18765/v1/capabilities
```

下单 JSON 结构（**不要执行该示例**）：

```json
{"intent_id":"strategy-20261121-0001","orderAmount":"5","timeIncrements":"TEN_MINUTE","symbolName":"BTCUSDT","payoutRatio":"0.80","direction":"LONG"}
```

其中 `intent_id` 必须是策略侧稳定的唯一决策 ID，相同 ID 不自动重试。为了接入不同框架，交易模块仅接收上述 JSON 业务字段；认证凭证由服务器从私有状态目录读取。

### 返回结果和异常语义

- `200`：返回本地记录；`api_acknowledged_not_settlement_verified` 只代表接口响应里有成功标志及订单号，**并非 Binance App 订单对账完成**。
- `202`：调用后结果不确定，**禁止同一请求或新 ID 自动重发**，需在 App 对账；不能解释为交易失败。
- `401`：外部调用者 Bearer Token 无效。
- `403`：真实交易开关未打开，或当前权限不允许交易。
- `404`：本地没有该 `intent_id` 对应的记录。
- `409`：重复意图或本地登录状态不可用。
- `501`：目前没有经过验证的币安实时查询接口。
- `503`：本地审计记录损坏或权限错误。

### 远程框架连接

**不要**把 18765 端口公开到互联网。远程框架使用 SSH 隧道，把远程服务器 `127.0.0.1:18765` 映射到框架所在机器的本地端口。API Token 只通过安全的秘密管理方式传给可信策略服务。HTTP 无自带 TLS，这是只监听 loopback 的原因。

### 运行部署

预置 `deploy/binance-event-api.service`。服务使用专用的 `binanceevt` 用户，需要 root 管理员创建权限 0600 的 `/etc/binance-event-api.env`，包含 `BINANCE_EVENT_API_TOKEN`。**默认不设置** `BINANCE_EVENT_LIVE_TRADING`，HTTP 只提供查询，真实 POST 下单会被拒绝。

### 安全、性能与生产边界

- 不使用 Binance 官方 API；交易端未在真实账户验证。真实赔率查询未实现，示例 `0.80` 并不是当前市场报价。
- 本地审计文件随交易次数增加。大量高频交易前应迁移到 SQLite/PostgreSQL 并索引、归档，不要依赖目录全量扫描。
- 本地意图锁不保证跨服务器级别去重，不代表交易所级幂等。
- 目前只有“提交/接口响应”的本地记录，没有最终订单、持仓、结算及资金回报核对。正式自动交易前必须补齐。
- 原型 HTTP 接口仅供内网可信系统、SSH 隧道和受控用户使用；不面向公网，也不支持多租户授权。

## 对外接口速查（区分数据来源）

| 路由 | 状态 | 数据来源 |
|---|---|---|
| `GET /v1/orders` | 已实现 | 本地 SQLite |
| `GET /v1/orders/{intent_id}` | 已实现 | 本地 SQLite/文件 |
| `GET /v1/exchange/orders/ongoing?page=1` | 已实测，只读 | Binance Web 未结算订单列表第一页 |
| `GET /v1/exchange/orders/closed?page=1` | 已实测，只读 | Binance Web 已结算订单列表第一页 |
| `GET /v1/exchange/orders/{order_id}` | 501 | 精确远端订单号查询未验证 |
| `GET /v1/settlements/{order_id}` | 501 | 独立远端结算查询未验证 |
| `GET /v1/quotes` | 501 | 实时赔率未验证 |

所有 HTTP 路由（包括只读查询）均需提供私有网关 Bearer Token；网关只监听 `127.0.0.1`。**实盘下单开关仍默认关闭。**
