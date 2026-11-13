# offline-dashboard — 离线单文件历史看板

## 职责

把 SQLite 中的活动历史、心率、点测血氧与整夜睡眠血氧**烘焙成一个自包含 HTML 文件**：

- 无 CDN、无字体、无外部图片、无网络请求，可直接双击打开、邮件发送或长期存档；
- 服务端渲染 SVG 睡眠结构图 + 内嵌 Canvas 曲线；
- 数据源容错：缺表、旧库、只有取证 JSONL 时都能给出可解释的结果。

源码：[miband_monitor/offline_dashboard/](../../miband_monitor/offline_dashboard/)

## 模块拆分

| 文件 | 内容 |
|---|---|
| `constants.py` | 时间窗口、阶段顺序/标签、数据源标记、`SPO2_SLEEP_JSONL` |
| `strings.py` | KPI 标签与页面文案（保持历史输出字符串不变） |
| `sources.py` | 探测/读取 SQLite 表与 `spo2_sleep.jsonl`，列名来自 storage schema |
| `collect.py` | 组装活动/心率/血氧/睡眠序列并做时间窗口过滤 |
| `packaging.py` | 打包 `DATA`、阶段样式等模板变量 |
| `kpis.py` | KPI 计算（最近一晚、深睡占比、睡眠效率、步数、心率区间、血氧最低） |
| `svg.py` | 睡眠分期图（run-length 压缩、按真实时间戳定位） |
| `render.py` | 读取 HTML 模板并注入数据 |
| `cli.py` | `main`（`history_dashboard.py` 入口） |
| `assets/` | `history_dashboard.html`、`history_dashboard.css`、`history_dashboard.js`、`history_charts.js`、`history_dashboard_tables.js` |

## 公开 API

| 名称 | 说明 |
|---|---|
| `collect(db_path, *, days=7, spo2_sleep_path=None)` | 返回渲染所需的序列与元数据 |
| `render(payload)` / `build(...)` | 生成完整 HTML 字符串 |
| `hypnogram_svg(stages, height=44, lane_gap=1.0)` | 分期图 SVG |
| `main(argv)` | CLI：`--db`、`-o/--output`、`--days`、`--spo2-sleep-jsonl` |

## 数据流

```
data/mi_band.db ──sources.collect()──> activity / heart_rate / spo2 / spo2_sleep
   │（spo2_sleep 表空时回退同目录或 --spo2-sleep-jsonl 指定的 JSONL）
   v
collect ──> kpis + svg ──> packaging ──> render(模板) ──> 单个 .html
```

## 依赖

- `miband_monitor.sleep`（会话检测与分期）、`storage.schema`（表/列名）、`config.health`；
- 标准库 `sqlite3`、`json`、`statistics`、`pathlib`；
- 不依赖 BLE、不启动线程、不发网络请求。

## 配置与常量

| 项 | 默认 | 说明 |
|---|---|---|
| `--db` | `data/mi_band.db` | 输入数据库 |
| `-o/--output` | `dashboard_history.html` | 输出文件 |
| `--days` | 7 | 展示时间窗口 |
| `--spo2-sleep-jsonl` | 自动回退 | 表为空时读取取证 JSONL |

KPI 标签与阶段颜色是输出契约的一部分，集中在 `strings.py` / `constants.py`。

## 失败模式

| 情况 | 行为 |
|---|---|
| 数据库不存在 | 早期版本会生成空看板并 exit 0（L4，见 Linux 验证报告）；当前实现应报错，使用前先确认库已生成 |
| 缺表/旧库 | `sources` 逐表探测，缺表按空序列处理，不崩溃 |
| 整夜血氧表为空 | 回退同目录 `spo2_sleep.jsonl`，仍无则只显示点测血氧 |
| 心率缺失 | 分期图位置画卧床灰带，而不是四条空泳道伪装有数据 |
| 活动历史不连续 | 按真实时间戳定位节律条，避免缺分钟导致整体错位 |
| 超过 5 分钟血氧空档 | 曲线断线，不伪造连续线段 |
| 输出目录不可写 | CLI 报错并返回非 0 |

## 测试

- `tests/test_history_dashboard.py`（重构后 `tests/offline_dashboard/{test_collect,test_render}.py`）：
  空库/缺表容错、时间窗口过滤、255 哨兵不进均值、活动条真实时间戳与会话着色、
  SVG 覆盖率与泳道顺序、整夜血氧表/JSONL 回退、模板注入无残留占位符、单文件自包含、CLI。
- 真机看板效果与 KPI 见 [validation/history-hardware.md](../validation/history-hardware.md) 附录。

## 相关文档

- [guides/usage.md](../guides/usage.md)
- [guides/data-formats.md](../guides/data-formats.md)
- [modules/sleep.md](sleep.md)、[modules/dashboard.md](dashboard.md)
