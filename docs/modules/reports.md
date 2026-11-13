# reports — 本地历史汇总

## 职责

读取 SQLite 中最近 N 小时的心率/血氧/活动数据，输出人类可读摘要（文本或 JSON）：

- 心率条数、最小/最大/均值；
- 血氧条数、均值、自动测量条数；
- 活动分钟数、步数合计；
- 缺失表/空库时的可解释输出。

源码：[miband_monitor/reports/](../../miband_monitor/reports/)

## 模块拆分

| 文件 | 内容 |
|---|---|
| `history.py` | `summarize`、`format_report`、`main`、`DEFAULT_DB`；表探测与标量查询助手 |

## 公开 API

| 名称 | 说明 |
|---|---|
| `summarize(db_path, hours=24.0, now=None) -> dict` | 聚合结果；库不存在抛 `FileNotFoundError` |
| `format_report(summary) -> str` | 文本报告 |
| `main(argv=None) -> int` | CLI：`--db`、`--hours`、`--json` |
| `DEFAULT_DB` | `data/mi_band.db`（来自 config.paths） |

## 数据流

```
data/mi_band.db ──summarize()──> dict ──format_report()──> 文本
                                     └─ json.dumps() ──> --json
```

## 依赖

- `miband_monitor.config.paths`；标准库 `sqlite3`、`json`；
- 只读，不写库、不启动线程；根入口 `history_report.py` 为兼容门面。

## 配置与常量

- 默认窗口 24 小时，`--hours 168` 为 7 天；
- 时间窗口基于 UTC 时间戳，报告里按本地时间展示；
- 255 心率哨兵与越界值不计入统计（与看板/睡眠口径一致）。

## 失败模式

| 情况 | 行为 |
|---|---|
| 数据库不存在 | `FileNotFoundError` → CLI 错误信息 + 退出码 1 |
| 缺某张表 | 对应指标显示"无数据"，其余照常 |
| 时间窗口内无记录 | 输出 0 条与提示，不报错 |
| 数据库损坏/被锁 | sqlite 异常向上抛出，CLI 打印错误 |

## 测试

- `tests/test_history.py`（重构后 `tests/history/test_report.py`）：摘要字段、空库/缺表、
  JSON 输出、CLI 退出码。
- 真机报告样例见 [validation/linux-validation.md](../validation/linux-validation.md)。

## 相关文档

- [guides/usage.md](../guides/usage.md)
- [guides/data-formats.md](../guides/data-formats.md)
- [modules/storage.md](storage.md)
