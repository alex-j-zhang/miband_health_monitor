# dashboard — 实时看板与 HTTP API

## 职责

在采集会话期间提供一个**零第三方依赖**的本地 Web 界面：

- 内存环形缓冲 + SSE 实时推送；
- HTTP JSON API（snapshot/history/healthz）；
- 单页前端（Canvas 图表）由本地资源组合成单文件 HTML；
- 可选的 SQLite 历史 provider：页面刷新时回填已落盘数据，不从空图开始。

源码：[miband_monitor/dashboard/](../../miband_monitor/dashboard/)

## 模块拆分

| 文件 | 内容 |
|---|---|
| `constants.py` | 端点路径、SSE 队列上限、默认点集、keepalive 间隔 |
| `history.py` | `make_db_history_provider(db_path)`：`source=db` 查询 SQLite |
| `hub.py` | `TelemetryHub`：环形缓冲、订阅队列、`publish_*`、`snapshot`、`history` |
| `http.py` | 轻量 HTTP 请求解析/响应工具 |
| `page.py` | 把 `assets/` 组合成自包含页面 |
| `server.py` | `DashboardServer`：asyncio HTTP 服务、路由、SSE |
| `assets/` | `dashboard.html`、`dashboard.css`、`dashboard.js` |

## 公开 API

| 名称 | 说明 |
|---|---|
| `TelemetryHub()` | `attach_loop`、`publish_heart_rate/steps/imu/spo2`、`snapshot`、`history`、`subscribe/unsubscribe`、`subscriber_count` |
| `DashboardServer(hub, host, port, history_provider=None)` | `start()`、`stop()`、`port`、`url` |
| `make_db_history_provider(db_path)` | 返回 `(kind, limit) -> list[dict]` |
| 端点 | `GET /`、`/api/snapshot`、`/api/history?kind=&limit=&source=db`、`/events`（SSE）、`/healthz` |

## 数据流

```
cli.callbacks ──> hub.publish_*() ──> ring buffer ──> 订阅队列 ──> /events SSE
                                            │
页面加载 ──> /api/history?source=db ──> SQLite（线程池，不阻塞事件循环）
         └─> /events 首帧 snapshot（持久化点 + 实时点按 ts 合并去重）
```

## 依赖

- 标准库 `asyncio`、`json`、`sqlite3`、`http`；
- `miband_monitor.config`（默认 host/port/缓冲上限）与
  `miband_monitor.storage.readers`（`utc_iso` 等）；
- 不导入 BLE：只接收已解析样本，便于测试。

## 配置与常量

| 项 | 默认 | 说明 |
|---|---|---|
| `--web-port` / `WEB_PORT` | 关闭 | `0` 表示随机空闲端口 |
| `--web-host` / `WEB_HOST` | `127.0.0.1` | 默认仅本机；`0.0.0.0` 会暴露到局域网 |
| `DEFAULT_MAX_POINTS` | 7200 | 每指标内存点数 |
| `DEFAULT_SNAPSHOT_POINTS` | 600 | snapshot/history 默认条数 |
| `DEFAULT_SSE_QUEUE_SIZE` | 512 | 慢客户端丢弃阈值，绝不阻塞 BLE |
| `DEFAULT_SSE_KEEPALIVE_SECONDS` | 15 | SSE 保活注释帧 |

## 失败模式

| 情况 | 行为 |
|---|---|
| 端口被占用 | `start()` 抛 `OSError`，CLI 记录 error 并继续无看板采集 |
| 慢客户端 | 队列满时丢事件，连接仍在，不拖慢 BLE |
| 浏览器 SSE 断线 | 浏览器自动重连；首帧 snapshot 重新同步 |
| SQLite 不存在/缺表/`--storage jsonl` | `source=db` 静默降级为空列表，纯实时视图 |
| DB 查询慢 | 走线程池（`asyncio.to_thread`），不阻塞事件循环 |
| 页面资源缺失 | `page.py` 组合失败时报错；正常安装包含 `assets/` |
| 未授权访问 | 无鉴权；默认只绑回环，切勿直接暴露公网 |

## 测试

- `tests/test_dashboard.py`（重构后 `tests/dashboard/{test_hub,test_server,test_cli}.py`）：
  hub 环形缓冲与订阅、HTTP API、SSE 推送、页面路由与回退页、SQLite history provider、
  CLI 接线。
- 真机链路验证见 [validation/linux-validation.md](../validation/linux-validation.md)
  （`/api/snapshot`、`/events` 均有真实数据）。

## 相关文档

- [guides/usage.md](../guides/usage.md)（看板启动方式）
- [guides/data-formats.md](../guides/data-formats.md)（实时/历史时间语义）
- [modules/offline-dashboard.md](offline-dashboard.md)（离线版区别）
