# 数据格式

本文描述所有落盘格式与时间语义。表/列名的单一事实来源是
[miband_monitor/storage/schema.py](../../miband_monitor/storage/schema.py)。

## 1. 数据目录

默认 `data/`：

```
data/
├── heart_rate.jsonl
├── steps.jsonl
├── imu.jsonl
├── spo2.jsonl              # --sync-history 产生
├── activity_history.jsonl  # --sync-history 产生
├── spo2_sleep.jsonl        # --sync-sleep-spo2 产生（同时写 spo2_sleep 表）
├── mi_band.db              # 含独立 spo2_sleep 表
├── mi_band.db-wal / mi_band.db-shm
└── csv/                    # --export-csv 生成
    ├── heart_rate.csv
    ├── steps.csv
    ├── imu.csv
    ├── spo2.csv
    ├── activity_history.csv
    └── spo2_sleep.csv
```

## 2. 时间语义

| 数据 | `ts` 含义 |
|---|---|
| 实时流（心率/步数/IMU） | **电脑接收时间**（Unix 秒，float） |
| 历史同步（血氧/活动/睡眠血氧） | **手环记录的采样时间**（分钟/秒精度） |
| JSONL/SQLite | 都带 `ts`（float）+ `iso_time`（UTC ISO-8601 毫秒） |

跨源合并（如看板回填）按 `ts` 排序去重，但要意识到两类时间的含义不同：
长跨度分析请以 SQLite 的 `ts` 与 `iso_time` 为准。

## 3. JSONL

每行一个 JSON 对象，公共字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `ts` | float | Unix 时间戳（秒） |
| `iso_time` | string | UTC ISO-8601，毫秒精度，如 `2020-01-07T08:10:18.472+00:00` |

各流特有字段：

| 文件 | 字段 |
|---|---|
| `heart_rate.jsonl` | `bpm` |
| `steps.jsonl` | `steps`、`distance_m`、`calories` |
| `imu.jsonl` | `sensor_type`、`sequence`、`sample_index`、`x_raw/y_raw/z_raw`、`ax_g/ay_g/az_g`、`ax_ms2/ay_ms2/az_ms2`（每个加速度样本一行） |
| `spo2.jsonl` | `spo2`（%）、`auto_measure`（0/1） |
| `activity_history.jsonl` | `kind`、`intensity`、`steps`、`heart_rate`、`unknown1`、`sleep`、`deep_sleep`、`rem_sleep`（每分钟一行） |
| `spo2_sleep.jsonl` | `spo2`（无符号）、`duration`、`spo2_high/low`、`signal_quality`、`extend`（原始字节组）、`source` |

示例：

```json
{"ts": 1578384618.472809, "iso_time": "2020-01-07T08:10:18.472+00:00", "bpm": 72}
{"ts": 1578384618.462322, "iso_time": "2020-01-07T08:10:18.462+00:00", "sensor_type": 1, "sequence": 0, "sample_index": 0, "x_raw": 0, "y_raw": 0, "z_raw": 256, "ax_g": 0.0, "ay_g": 0.0, "az_g": 1.0, "ax_ms2": 0.0, "ay_ms2": 0.0, "az_ms2": 9.8066}
```

IMU 换算：int16 / 4096 LSB/g；`a*_ms2 = a*_g * 9.80665`。

## 4. SQLite

数据库 `data/mi_band.db`（WAL 模式），六张表均以 `id` 主键、`ts`/`iso_time` 索引：

| 表 | 列 |
|---|---|
| `heart_rate` | `id, ts, iso_time, bpm` |
| `steps` | `id, ts, iso_time, steps, distance_m, calories` |
| `imu` | `id, ts, iso_time, sensor_type, sequence, sample_index, x_raw, y_raw, z_raw, ax_g, ay_g, az_g, ax_ms2, ay_ms2, az_ms2` |
| `spo2` | `id, ts, iso_time, spo2, auto_measure` |
| `activity_history` | `id, ts, iso_time, kind, intensity, steps, heart_rate, unknown1, sleep, deep_sleep, rem_sleep` |
| `spo2_sleep` | `id, ts, iso_time, spo2, duration, spo2_high, spo2_low, signal_quality, extend` |

常用查询：

```sql
SELECT datetime(ts, 'unixepoch'), bpm FROM heart_rate ORDER BY ts;
SELECT * FROM steps ORDER BY ts;
SELECT sensor_type, sequence, sample_index, az_g FROM imu WHERE ts > :since;
SELECT datetime(ts, 'unixepoch'), spo2, auto_measure FROM spo2 ORDER BY ts;
SELECT datetime(ts, 'unixepoch'), steps, heart_rate FROM activity_history ORDER BY ts;
SELECT datetime(ts, 'unixepoch'), spo2, duration FROM spo2_sleep ORDER BY ts;
```

`spo2_sleep` 是**独立表**，不与点测 `spo2` 混用；四组未解明语义的字节以 JSON 文本存放。

## 5. CSV

```bash
python export_csv.py --db data/mi_band.db --output data/csv
```

导出上述六张表；表头为列名，UTF-8 编码，`newline=""`（Windows 兼容）。
`--export-csv` 在会话结束时自动执行同样导出；要求 `--storage sqlite|both`。

## 6. 历史 payload 格式（协议层）

| 类型 | 布局 |
|---|---|
| 普通血氧 `0x25` | 版本 2 + N×65B：`int32 LE unix_ts, int8 spo2, 60B 保留`；负值 = 自动测量，真实值 +128 |
| 活动 `0x01` | N×8B：`kind, intensity, steps, heart_rate, unknown1, sleep, deep_sleep, rem_sleep` |
| 睡眠血氧 `0x26` | 版本 2 + N×30B：`int32 LE ts, u8 spo2, u8 duration, 6B high, 6B low, 8B signal, 4B spare` |

活动三字节 `sleep/deep_sleep/rem_sleep` **不是阶段时长**，而是手环算出的判别分数；
本项目把它们原样保留为证据，睡眠分期另行推导（见 [modules/sleep.md](../modules/sleep.md)）。

## 7. 派生数据

睡眠结果不新增数据库表：`sleep_analysis.py` 从 `activity_history` 读取样本，
输出为报告/JSON/离线看板中的派生字段，并带"推导值"语义。离线看板 HTML 内嵌的
`DATA` 对象是展示层载荷，不作为长期存储格式。

## 8. 兼容性

- JSONL 只追加；SQLite 表结构变更必须提供迁移，不能静默丢列；
- 旧库缺表时，分析/看板按"无数据"降级而不是崩溃；
- 255 是心率"无测量"哨兵，所有统计需过滤；SpO₂ 合法区间 50–100。
