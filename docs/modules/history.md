# history — 手环侧历史数据

## 职责

读取手环内部保存的分钟级活动记录与血氧记录，并解析成带时间戳的样本：

- 私有 fetch 协议（`0x0004` 控制/元数据，`0x0005` 数据分片）；
- 三种 payload：普通血氧 `0x25`（65B/条）、活动 `0x01`（8B/分钟）、睡眠血氧 `0x26`（30B/条）；
- 高层同步循环：按数据类型的窗口推进、失败隔离、结果汇总。

源码：[miband_monitor/history/](../../miband_monitor/history/)

## 模块拆分

| 文件 | 内容 |
|---|---|
| `protocol.py` | UUID、命令/响应字节、记录尺寸、`FetchDataType`、短日历与 fetch 时间编解码、日志格式化 |
| `models.py` | `HistoryFetchError`、`HistoryParseError`、`HistorySyncResult` |
| `parsers.py` | `parse_spo2_normal`、`parse_spo2_sleep`、`parse_activity`、`_trim_to_records`、`_parse_payload` |
| `fetcher.py` | `BandHistoryFetcher`：一次 fetch 轮次、GATT 订阅、分片组装、完成帧/ACK |
| `sync.py` | `sync_history`、`summarize_history_result`、`_extend_*` |

## 公开 API

| 名称 | 说明 |
|---|---|
| `FetchDataType` | `ACTIVITY=0x01`、`SPO2_NORMAL=0x25`、`SPO2_SLEEP=0x26` |
| `encode_fetch_time` / `decode_fetch_time` | 6B 本地日历 + reason + 时区（15 min 单位） |
| `encode_short_calendar` / `decode_short_calendar` | 短日历编解码 |
| `parse_activity(payload, start_ts)` | 8B/分钟 → `kind,intensity,steps,heart_rate,unknown1,sleep,deep_sleep,rem_sleep` |
| `parse_spo2_normal(payload)` | 版本 2 + N×65B；负值 = 自动测量，真实值 +128 |
| `parse_spo2_sleep(payload)` | 版本 2 + N×30B；含 duration/high/low/signal/extend |
| `BandHistoryFetcher` | `fetch(start, data_type, ...)` 返回原始 payload |
| `sync_history(client, days, on_spo2, on_activity, include_spo2_sleep=False, on_spo2_sleep=None)` | 高层循环，返回 `HistorySyncResult` |
| `summarize_history_result(result)` | 一行摘要（样本数、rounds、errors） |

## 数据流

```
cli.runner._sync_band_history → history.sync_history
   │  for each type (activity, spo2, [spo2_sleep]):
   │    BandHistoryFetcher.fetch() ──> 0x0004 start-date → 0x0005 chunks
   │                              └──> 10 02 01 完成帧 → 0x03 ACK
   │    parsers._parse_payload    ──> list[dict(ts, ...)]
   └──> callbacks ──> storage: activity_history / spo2 / spo2_sleep（三表隔离）
```

## 依赖

- 只依赖 `miband_monitor.config` 与标准库；`protocol/parsers` 不导入 BLE 后端，
  模拟器可直接复用同一套编解码；
- `fetcher` 接收任意具备 `client`/GATT 写/通知能力的对象（真实客户端或模拟手环）。

## 配置与常量

- `DEFAULT_TIMEOUT=30 s`、`DEFAULT_MAX_ROUNDS=12`、`MAX_PAYLOAD_BYTES=65536`；
- `SPO2_RECORD_SIZE=65`、`SPO2_SLEEP_RECORD_SIZE=30`、`ACTIVITY_SAMPLE_SIZE=8`、`SPO2_VERSION=2`；
- `--sync-history DAYS` 只是"最多问多久"：手环环形缓冲约 2400 分钟（40 小时），
  实际到手 ≤40 小时，且 App 取走的部分会留下永久空洞；
- 0x26 默认关闭（`--sync-sleep-spo2` 开启），尾部字段语义未定。

## 失败模式

| 情况 | 行为 |
|---|---|
| 手环不支持某类型 | 记录 `band rejected fetch type ...`，继续其它类型 |
| 30 s 内无 start-date / 数据分片 | `HistoryFetchError`，本类型本轮失败，重连后重试 |
| 分片序号缺口 | 记录 `packet counter mismatch` 并失败，避免拼接错位 |
| 完成帧不是 `10 02 01` | 记录 hex 并失败；保留可选 CRC32 校验分支 |
| 空窗口回 1970-01-01 | 视为"没有新数据"，回退请求窗口，不报错 |
| payload 长度不是整记录 | `_trim_to_records` 按数据类型裁剪（活动读完成帧为止） |
| 解析值越界（SpO₂<50 等） | `HistoryParseError` / 样本被丢弃并记录，不写入垃圾数据 |
| 历史同步失败 | 只记录 error，实时采集继续 |

## 测试

- `tests/test_history.py`（重构后拆为 `tests/history/test_codecs.py`、`test_parsers.py`、
  `test_fetcher.py`、`test_sync.py`、`test_report.py`）：时间编码、三种解析、
  0x26 30B 布局、真机前两条记录字节级回归、模拟 fetch 丢包、落盘、CLI。
- `tests/test_import_spo2_sleep.py`：0x26 `.bin` 导入双写与幂等去重。
- 真机证据：[validation/history-hardware.md](../validation/history-hardware.md)、
  [investigations/sleep-fields.md](../investigations/sleep-fields.md)。

## 相关文档

- [modules/storage.md](storage.md)、[modules/sleep.md](sleep.md)
- [guides/data-formats.md](../guides/data-formats.md)
- [guides/usage.md](../guides/usage.md)（历史同步命令）
