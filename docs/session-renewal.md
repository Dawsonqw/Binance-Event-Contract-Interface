# 会话有效期与续期（非官方接口）

## 当前事实

- Binance 事件合约尚未开放官方 API，仅可通过 App 交易。该项目使用的是未公开的 Web 接口，可能随时改变。
- 上游作者说 `p20t` Web 凭证最长约五天；这不是官方保证。CSRF token 与 Cookie 也可能提前失效或被撤销。
- 现有代码没有官方 `refresh_token`、官方服务端幂等键或可验证的订单查询接口。浏览器 Cookie 存在 **不等于** 交易所仍接受该登录会话。

## 当前实现（安全优先）

1. 通过 `python -m session_lifecycle status` 查询本地凭证元数据，不会读取并打印 Token 的实际内容。
2. 默认提前 24 小时提醒；到期、过期时间未知或剩余有效期不足 15 分钟时拒绝下单。
3. `python main.py` 使用既有 Playwright 持久化浏览器 profile 尝试正常登录；只有用户在 Binance App 中批准 QR 才能完成需要的交互式验证。
4. 登录最多等待 300 秒，超时不改变凭证也不会触发下单。`--timeout 600` 可手工加长；`--reset-browser-profile` 将删除旧浏览器状态，请谨慎使用。
5. 如果现有浏览器仍被币安认为已登录，可以尝试通过正常网站流程重新取得有效 Cookie；是否得到新 Token 和 `csrftoken` 需针对真实账户验证。**不会自动伪造登录请求、跳过安全验证或承诺无人工续期。**

## 建议操作

用户在远程服务器打开交互式 TTY，不要写入 shell 日志或终端录像。先检查：

```bash
cd /home/binanceevt/Binance-Event-Contract-Interface
runuser -u binanceevt -- /home/binanceevt/.venvs/binance-event-contract/bin/python -m session_lifecycle status
```

如果状态显示 `expired` / `unknown_expiry` / `missing`，使用本人 App 扫码重新验证：

```bash
runuser -u binanceevt -- /home/binanceevt/.venvs/binance-event-contract/bin/python main.py --timeout 300
```

如已持有有效浏览器会话，此操作**可能**更新凭证；如果仍需扫码，必须由账户本人确认。登录成功后再次检查 `python -m session_lifecycle status`。

状态命令退出码：0=本地未到预警期；1=距到期不足 24 小时；2=缺失/过期/有效期未知。退出码仅针对本地文件和时间戳，不是线上认证检查。

可选使用 `deploy/binance-event-session-check.service` 和 `.timer` 每六小时执行一次无网络状态检查。此定时器只把结果输出到 journald；**不会自动续期、不会自动通知到手机，也不会下单**。使用它需要自行接入通知系统并启用 systemd timer。

## 应对长时间无人值守的原则

- 未能获得有效新凭证时必须 `fail closed`：禁用下单，等待本人重新验证。
- 未来若官方提供事件合约 API / 正式授权续期能力，优先换用官方接口；不通过模拟设备、破解认证或规避风控实现强制续期。
- 接入真实交易前必须独立验证本地过期检查、交易所接受的认证、报价有效性、订单查询与结算对账。
