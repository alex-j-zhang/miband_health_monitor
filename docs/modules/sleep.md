# sleep — 睡眠推导

## 职责

从手环活动历史（每分钟 `kind / intensity / steps / heart_rate`）**推导**睡眠窗口与分期：

- 睡眠窗口：以手环自己的 `kind` 睡眠家族为锚点，向两侧按"静止且未佩戴为否"延伸；
- 清醒：分钟内有步数（`steps >= 1`）即清醒；
- 深睡/浅睡：窗口内按心率水平排名 + 短时心率变异性排名切分，无心率则 `unknown`；
- 未佩戴：`kind & 0x0F == 3`（`0xF3`）的连续区间，不生成睡眠段；
- 输出全部是 `derived` 值，与手环原始字段严格分开。

源码：[miband_monitor/sleep/](../../miband_monitor/sleep/)

## 模块拆分

| 文件 | 内容 |
|---|---|
| `constants.py` | 阶段名、`kind` 码、窗口/分期阈值（每个数字都带真机或 Zepp 依据注释） |
| `samples.py` | `kind_code`、`is_nonwear`、`is_sleep_kind`、`is_movement`、`is_awake`、`usable_heart_rate`、`load_samples`、本地时间格式化 |
| `scoring.py` | 对三字节"阶段分数"的实验性读法：`score_value`、`samples_have_scores`、`score_estimates` |
| `detection.py` | `detect_sessions`、`detect_nonwear_spans` |
| `classification.py` | `classify_session` + `_rank_within`、`_rolling_sd`、`_smooth` |
| `session.py` | `SleepSession`、`build_session`、`analyze`、觉醒/最长睡眠段统计 |
| `report.py` | `_format_report` 文本报告 |
| `cli.py` | `main`（`sleep_analysis.py` 入口） |
| `calibration.py` | 四种候选睡眠字节读法与 Zepp Life 参考值对比（供 `tools/calibrate_sleep.py` 调用） |

## 公开 API

`analyze(samples) -> dict` 是主入口，返回 `sessions`、每夜摘要与 `derived: true` 标记；
`build_session(samples)`、`detect_sessions`、`detect_nonwear_spans`、`classify_session`
可单独测试。样本工具：`load_samples(db_path, ...)`、`kind_code`、`is_nonwear`、
`is_sleep_kind`、`is_awake`、`usable_heart_rate`、`local_iso`、`local_date`。
常量随包导出（`STAGE_*`、`SLEEP_KIND_MASK`、`DEEP_HR_RANK=0.20`、`DEEP_SD_RANK=0.45` 等），
阈值集中在 [constants.py](../../miband_monitor/sleep/constants.py)。

## 数据流

```
SQLite activity_history ──load_samples()──> [{ts, kind, intensity, steps, heart_rate, ...}]
      │
      ├─ detect_nonwear_spans() ──> 0xF3 连续区间（不生成睡眠）
      ├─ detect_sessions()      ──> 锚点(睡眠 kind) + 静止延伸 ±60min，桥接 ≤20min 空隙
      └─ classify_session()     ──> 步数判醒 + 心率/HRV 排名切深睡，短段平滑
                                   │
                              SleepSession ──> 文本报告 / 离线看板 / JSON
```

## 依赖

- `miband_monitor.config.health`（心率合法区间、255 哨兵）；
- `miband_monitor.storage.schema`（只读表名，不启动写线程）；
- 标准库 `sqlite3`、`statistics`；
- 被 `offline_dashboard` 与 `tools/calibrate_sleep.py` 复用。

## 配置与常量

| 常量 | 值 | 依据 |
|---|---|---|
| `SLEEP_KIND_MASK/VALUE` | 0xF0 | 真机 27 种 kind 中只有 0xF0 家族与睡眠相关 |
| `KIND_NONWEAR` | 0x03 | Gadgetbridge `TYPE_NONWEAR`；679 分钟 0 心率/0 步数 |
| `AWAKE_STEPS` | 1 | Zepp 20 分钟清醒 ≈ 捕获的 17 个有步数分钟 |
| `ANCHOR_MINUTES` | 60 | 锚点最多向两侧延伸 60 分钟 |
| `MIN_ANCHOR_MINUTES / MIN_SESSION_MINUTES` | 10 / 20 | 丢弃孤立锚点与短片段 |
| `SESSION_GAP_MINUTES` | 20 | 桥接夜间短暂醒来 |
| `HR_WINDOW_MINUTES` | 7 | 短时 HRV 滚动窗口 |
| `DEEP_HR_RANK / DEEP_SD_RANK` | 0.20 / 0.45 | 对 2020-01-06 一夜校准，**临时值** |
| `MIN_STAGE_MINUTES` | 3 | 平滑过短阶段段 |

**不输出 REM**：`0x48` 在本固件声明 0 条；三字节 `rem_sleep` 与 App 的 REM 分钟数方向相反
（App 报 0 时 458/485 分钟非零，App 报 87 分钟时 396/396 为 0），把它当时长会是发明而非测量。
完整取证见 [investigations/sleep-fields.md](../investigations/sleep-fields.md)。

## 失败模式

| 情况 | 行为 |
|---|---|
| 数据库不存在/无 `activity_history` 表 | 报错或返回空结果；CLI 退出码非 0 |
| 目标夜晚无样本 | 报告"无数据"，不猜睡眠 |
| 整夜 `kind` 全为 `0xF3` | 判定未佩戴，不生成睡眠段、不进"最近一晚" |
| 有睡眠段但整夜无心率 | 深睡/浅睡为 `unknown`，睡眠时长仍可给出 |
| 三字节分数缺失 | 只作为实验性旁证输出，不参与主分期 |
| 阈值只有一夜样本 | 代码注释与文档均标注"临时值"，等更多 Zepp 夜校准 |

## 测试

- `tests/test_sleep.py`（重构后 `tests/sleep/{test_detection,test_classification,test_session,test_cli}.py`）：
  `kind` 家族判定（含 `0xF3`）、锚点+静止延伸窗口、按步数判清醒、深睡分期、
  无心率夜返回全 `unknown`、未佩戴夜不生成睡眠段、真机数据与 Zepp 的回归对齐、
  分数积分、CLI 文本/JSON/指定夜。
- `tests/test_calibrate.py`：`tools/calibrate_sleep.py` 四种读法与"可用夜不足拒绝拟合"。

## 相关文档

- [investigations/sleep-fields.md](../investigations/sleep-fields.md)
- [guides/usage.md](../guides/usage.md)（睡眠命令）
- [modules/offline-dashboard.md](offline-dashboard.md)、[modules/history.md](history.md)
