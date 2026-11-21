# Binance 事件合约接口研究（安全加固分支）

> **非官方、未经实盘验证。** 内部接口可能随时变更；本项目不能保证真实下单成功，也不提供绕过币安风控或身份验证的方法。

## Linux 部署（必须是非 root 用户）

安装隔离 Python 环境，优先执行 `pip install -r requirements.lock`（完全固定的实测依赖版本；`requirements.txt` 是直接依赖清单）、`python -m playwright install chromium`，随后运行 `python -m unittest discover -s tests -v`。系统若没有 venv 或 Chromium 所需系统库，应由管理员另行安装。

不再依赖 Pillow：登录 QR 只输出到交互式终端，不再生成 qrcode.jpg；三个直接依赖已固定版本。

## 账户登录

以独立的非 root 用户，在交互式 TTY 运行 `python main.py`，使用本人的 Binance App 扫码。程序不会输出 csrftoken 或 p20t。

登录凭证保存在 `~/.local/state/binance-event-contract/token.json` (0600)；Chromium profile 位于私有状态目录 (0700)。通过 `BINANCE_EVENT_STATE_DIR` 可更换状态目录，请确保其位于可信磁盘，且不要备份或上传凭证。

## Token 有效期检查与交互式续期

代码现在提供 **仅本地元数据** 检查：`python -m session_lifecycle status`。默认提前 24 小时预警；过期、有效期未知或剩余不足 15 分钟会在下单前被拒绝。正常存储的登录 Cookie 也可能提前被平台撤销，因此“本地未过期”不代表交易所确认有效。

重新获取登录态请使用本人 Binance App 扫码确认，在非 root 交互式 SSH 终端执行 `python main.py --timeout 300`。它保留原有浏览器 Profile，但**不保证旧登录态能无人工自动刷新**；平台要求重新验证时必须重新扫码。不要使用 `--reset-browser-profile`，除非确实希望删除原有浏览器状态。

完整方案见 [docs/session-renewal.md](docs/session-renewal.md)。可选的 systemd timer 模板位于 `deploy/`，默认**没有启用**，且只监控本地时间戳，不会续期、不连接交易所或发送通知。

## 给其他框架接入的交易 / 查询 API

已抽离出稳定的 [Python SDK 和本地 HTTP API](docs/integration-api.md)：

- Python：`EventContractGateway().trading.submit_order(OrderRequest(...))` 和 `EventContractGateway().queries.get_order(intent_id)`
- HTTP：`POST /v1/orders`、`GET /v1/orders/{intent_id}`、`GET /v1/orders`、`GET /v1/session`、`GET /v1/capabilities`
- HTTP 服务默认只绑定 `127.0.0.1:18765`，要求单独的 Bearer Token；其他机器通过 SSH 隧道接入。
- **查询到的只是本地提交审计记录**：币安实时订单、赔率和结算接口仍未验证，明确返回 HTTP 501，不会伪造远端查询数据。

## 真实下单默认关闭

只有调用 `safe_orders.place_order_web()`，同时设置环境变量 `BINANCE_EVENT_LIVE_TRADING=I_ACCEPT_REAL_MONEY_RISK`，才能尝试下单。默认单笔上限 5 USDT；通过 `BINANCE_EVENT_MAX_ORDER_USDT` 配置。每次必须传入稳定且唯一的 `intent_id` 关键字参数。

下单前记录独占的持久意图文件，用来拒绝同一个 ID 再次发送；使用 5 秒连接、12 秒响应超时，**绝不自动重试**。未返回明确成功标志和订单号时抛出 `OrderNotConfirmed`，必须人工去币安 App 查证，切勿超时后盲目重发。即便得到订单号，也应通过 App 对账。

**限制**：本地意图记录只保护同一状态卷、相同 `intent_id`，不是交易所服务端幂等保障；没有可靠的查询及对账 API。不同机器、不同 ID 或恢复丢失的状态目录仍可能重复交易。默认拒绝 root 登录和 root 下单，不提供 24 小时无人值守的稳定性承诺。

## 离线测试与安全

`python -m unittest discover -s tests -v` 使用 HTTP Mock，不访问交易所。不要提交 token.json、浏览器 profile、二维码、状态目录或日志。使用者需自行评估币安相关服务条款及资金风险。

上游项目： https://github.com/thuhollow2/Binance-Event-Contract-Interface 。本 Fork 保留上游许可证。

远程服务器上若已配置专用系统账户，管理员可用 `runuser -u binanceevt -- /home/binanceevt/.venvs/binance-event-contract/bin/python /home/binanceevt/Binance-Event-Contract-Interface/main.py` 在交互式 SSH 终端执行登录。登录时需要分配 TTY；请勿将 QR 或凭证写入终端会话日志。
