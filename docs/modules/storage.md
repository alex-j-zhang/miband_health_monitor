# storage — 遥测持久化

## 职责

把 BLE 回调里的每个样本变成带时间戳的持久化行，并保证 BLE 线程永不被磁盘阻塞：

- JSONL：一行一个 JSON 对象，按流分文件、追加写；
- SQLite：单个 `mi_band.db`（WAL），一流一表、按 `ts` 建索引；
- 后台单写线程：生产者只入队，消费者批量提交；
- CSV 导出：从 SQLite 按固定列序导出六张表；
- 权限：数据库/JSONL/目录在 POSIX 上收紧到 0600/0700。

源码：[miband_monitor/storage/](../../miband_monitor/storage/)

## 模块拆分

| 文件 | 内容 |
|---|---|
| `schema.py` | **单一事实来源**：表名/列名、`JSONL_FILENAMES`、`_SCHEMA`、`_INSERT_SQL`、`TABLE_COLUMNS`、`STORAGE_BACKENDS` |
| `store.py` | `TelemetryStore`：公开 sink API、时间戳、队列、`stats`、`close` |
| `writer.py` | `run_writer_loop`（批量提交、失败后排空队列）、`QUEUE_POLL_SECONDS` |
| `sqlite.py` | `open_database`（WAL）、`insert_record`、`secure_database_files` |
| `readers.py` | `utc_iso`、`normalize_backend`、`read_jsonl`、`table_exists`、`secure_path` |
| `export_csv.py` | `export_table`、`export_database`、`main`（`export_csv.py` 入口） |

## 公开 API

| 名称 | 说明 |
|---|---|
| `TelemetryStore(output_dir="data", backend="both")` | backend ∈ `jsonl/sqlite/both/none`；`db_path` 属性 |
| `record_heart_rate(bpm, timestamp=None)` | 写 `bpm` |
| `record_steps(info, timestamp=None)` | `steps`、`distance_m`、`calories` |
| `record_imu(info, timestamp=None)` | 每个加速度样本一行 |
| `record_spo2(spo2, auto_measure, timestamp=None)` | 点测血氧（可指定手环采样时间） |
| `record_activity_history(sample, timestamp=None)` | 每分钟活动记录 |
| `record_spo2_sleep(sample)` | 整夜血氧，独立表 |
| `stats()` / `close()` / 上下文管理器 | 计数、排空队列、关闭 |
| `export_database(db_path, output_dir)` / `export_table(...)` | CSV 导出 |
| schema 常量（`HEART_RATE_TABLE` 等） | 供 sleep/offline_dashboard/reports 只读导入 |

## 数据流

```
BLE/历史回调 ──> TelemetryStore.record_*() ──> queue.Queue
                                                  │
                                   writer 线程 ───┴─> JSONL append
                                                  └─> SQLite 批量 commit（WAL）
close() ──> 入队 sentinel ──> join（最长 10s）──> 统计
--export-csv ──> export_database() ──> data/csv/*.csv
```

## 依赖

- `miband_monitor.config`（`DEFAULT_DB_FILENAME`、`secure_path`）；
- 标准库 `threading`、`queue`、`sqlite3`、`json`；
- 被 `cli/callbacks`、`dashboard.history`、`reports`、`sleep`、`offline_dashboard` 使用；
- schema 无 I/O，可被分析包安全导入而不启动线程。

## 配置与常量

| 项 | 默认 | 说明 |
|---|---|---|
| `--storage` / `STORAGE_BACKEND` | `both` | `jsonl` / `sqlite` / `both` / `none` |
| `DB_FILENAME` | `mi_band.db` | 输出目录下的 SQLite 文件名 |
| `JSONL_FILENAMES` | 六个流文件 | 与表名一一对应 |
| `COMMIT_BATCH_SIZE` | 200 | SQLite 批量提交大小 |
| `QUEUE_POLL_SECONDS` | 0.5 | writer 队列轮询间隔 |
| `CLOSE_JOIN_TIMEOUT` | 10.0 s | `close()` 等待 writer 退出的上限 |
| 权限 | 0600 / 0700 | 文件 / 目录（POSIX） |

## 表与落点

| 表 / JSONL | 字段要点 |
|---|---|
| `heart_rate` | `ts, iso_time, bpm` |
| `steps` | `ts, iso_time, steps, distance_m, calories` |
| `imu` | `sensor_type, sequence, sample_index, x/y/z_raw, a*_g, a*_ms2` |
| `spo2` | `ts, iso_time, spo2, auto_measure` |
| `activity_history` | `kind, intensity, steps, heart_rate, unknown1, sleep, deep_sleep, rem_sleep` |
| `spo2_sleep` | `spo2, duration, spo2_high/low, signal_quality, extend`（尾部字节 JSON 文本） |

字段级格式见 [guides/data-formats.md](../guides/data-formats.md)。

## 失败模式

| 情况 | 行为 |
|---|---|
| 目录不可创建/只读 | 构造时抛 `OSError` 并给出路径；CLI 捕获后退出码 1 |
| 队列满/写入磁盘失败 | writer 记录异常并继续排空队列，生产者不会在 BLE 回调里抛异常 |
| `close()` 后继续写 | 记录被丢弃并计数（`stats` 可见），不抛异常 |
| SQLite 被其它进程锁 | WAL + 超时重试；持续失败记录日志，JSONL 仍可用 |
| 数据库文件已存在但损坏 | 打开失败并报错；不静默删除（保护数据） |
| `--storage none` | 不创建文件，只打日志 |
| CSV 表缺失 | `export_table` 跳过/报错；`--export-csv` 要求 sqlite/both |

## 测试

- `tests/test_storage.py`：时间戳、JSONL/SQLite 写入（含血氧/活动历史/睡眠血氧）、
  CSV 导出、关闭后写入丢弃、权限设置。
- `tests/test_dashboard.py` 与 `tests/test_history_dashboard.py` 覆盖 `source=db`
  和历史看板对 schema 的读取容错。

## 相关文档

- [guides/data-formats.md](../guides/data-formats.md)
- [modules/dashboard.md](dashboard.md)、[modules/sleep.md](sleep.md)
- [architecture.md](../architecture.md#7-数据契约)
