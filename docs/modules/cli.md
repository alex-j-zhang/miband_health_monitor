# cli — 命令行与会话监督

## 职责

所有用户可见入口的编排层：

- 参数解析（与重构前 **逐字节兼容**：相同 flag、默认值、help、校验与退出码）；
- `.env` 加载与权限收紧、日志配置；
- 校验设备标识/密钥，决定真机或 demo；
- 组装 `TelemetryStore`、可选 `TelemetryHub/DashboardServer` 与回调；
- 连接、同步历史、启动数据流、等待时长、断链重连、退出清理与 CSV 导出。

源码：[miband_monitor/cli/](../../miband_monitor/cli/)

## 模块拆分

| 文件 | 内容 |
|---|---|
| `args.py` | `build_parser`、`parse_args`、`demo_requested`、历史私有助手 `_env_float/_env_optional_int/_secure_env_file` |
| `app.py` | `main(argv)`：整个命令的生命周期与退出码 |
| `callbacks.py` | `build_callbacks(store, hub=None)` → `StreamCallbacks` |
| `logging.py` | `configure_logging`、`LOG_FORMAT`（容忍 Windows 旧代码页） |
| `runner.py` | `run_monitor`（重连监督）、`run_demo`、`_sync_band_history` |

## 公开 API

`main(argv) -> int`（`miband_monitor.cli` 包级导出）；`build_parser()`、`parse_args()`、
`demo_requested(args)`；`run_monitor(device_id, auth_key_hex, *, duration, imu, callbacks,
scan_timeout, connect_timeout, reconnect, reconnect_initial_delay, reconnect_max_delay,
sync_history_days, sync_sleep_spo2, device_name, mtu, direct_connect, client_factory)`；
`run_demo(...)`；`build_callbacks(store, hub=None)`；`configure_logging(level)`。

## 数据流

```
main()
 ├─ parse_args()（load_env + secure_env_file）
 ├─ --doctor ──> diagnostics.run_doctor（不连接手环）
 ├─ 校验 DEVICE_ID/AUTH_KEY（demo 时允许占位符）
 ├─ TelemetryStore + 可选 DashboardServer
 ├─ build_callbacks()
 ├─ run_monitor() / run_demo()
 │    ├─ connect_and_authenticate()
 │    ├─ 首次会话：_sync_band_history()（失败不影响实时流）
 │    ├─ start_raw_sensor_stream? → start_heart_rate_stream → start_steps_stream
 │    └─ 等待时长/断链 → 退避或 0.3s 极速重连
 └─ finally：停止看板 → store.close() → --export-csv
```

## 依赖

- 领域包：`ble`、`history`、`storage`、`dashboard`、`simulator`（仅 demo）、
  `diagnostics`（仅 --doctor）；
- `miband_monitor.config`、`bluez.errors.describe_ble_error`；
- 根入口 `miband_client.py` 只是 `from miband_monitor.cli import main` 的兼容门面。

## 配置与常量

CLI 参数（完整说明见 [guides/configuration.md](../guides/configuration.md)）：
`duration`、`--imu`、`--device-id`、`--device-name`、`--mtu`、`--sync-history [DAYS]`、
`--sync-sleep-spo2`、`--demo`、`--output`、`--storage`、`--export-csv`、
`--scan-timeout`、`--connect-timeout`、`--log-level`、`--web-port`、`--web-host`、
`--no-reconnect`、`--doctor`、`--no-direct-connect`。
重连策略：认证成功后断链 → 0.3 s 快速重连（抓住手环断链后的广播窗口）；
连续失败 → 1 s 起指数退避，上限 30 s。

## 失败模式

| 情况 | 行为 |
|---|---|
| 未配置 DEVICE_ID/AUTH_KEY | 报错并退出码 1，提示配置 `.env` 或 `--demo` |
| `--mtu` 越界 | 明确错误 + 合法区间，退出码 1 |
| `--sync-sleep-spo2` 缺 `--sync-history` | argparse 拒绝，退出码 2 |
| 看板端口被占用 | 记录 error，继续无看板采集 |
| 历史同步失败 | 只记录 error，实时采集继续 |
| 会话异常 | 记录 `Monitoring stopped after error`，退出码 1；`--no-reconnect` 时不重连 |
| Ctrl+C | 优雅退出，`Storage summary` 正常打印 |
| `--export-csv` 与 `--storage jsonl/none` | warning，跳过导出 |

## 测试

- `tests/test_device_matching.py`：CLI 参数、环境变量优先级、macOS 名称匹配；
- `tests/test_dashboard.py`：CLI 接线与看板启停；
- `tests/test_history.py`：`--sync-history` 参数与摘要；
- `tests/test_simulated_band.py`：`run_monitor/run_demo` 的重连与超时行为。
- CI demo 冒烟命令直接覆盖 `main()` 的大部分分支。

## 相关文档

- [guides/configuration.md](../guides/configuration.md)、[guides/usage.md](../guides/usage.md)
- [modules/ble.md](ble.md)、[modules/diagnostics.md](diagnostics.md)
