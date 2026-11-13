# tools — 诊断、取证与校验脚本

## 职责

`tools/` 下的可选脚本不参与正常采集；它们用于真机取证、协议探测、数据导入与 CI 校验。
脚本只保留参数解析与面向硬件的诊断流程，共享逻辑直接从领域包导入
（如 `history.parsers.parse_spo2_sleep`、`storage.TelemetryStore`、`sleep.calibration`），
没有单独的 `miband_monitor.tools` 实现包。

源码：[tools/](../../tools/)（脚本清单与用法见 [tools/README.md](../../tools/README.md)）

## 脚本清单

| 脚本 | 用途 | 是否接触手环 |
|---|---|---|
| `capture_history_payload.py` | 抓取原始历史 payload（`.bin` + SHA-256 元数据），用于离线复核 | 是 |
| `import_spo2_sleep.py` | 解码 0x26 `.bin` 并写入 `spo2_sleep` JSONL + SQLite（按时间戳去重） | 否 |
| `probe_declared.py` | 只问手环"从某时刻起还有多少分钟"，不取数据、不发 ACK | 是 |
| `probe_fetch_types.py` | 查询任意历史类型码的 start-date 回复 | 是 |
| `probe_sleep_session.py` | 探测 Huami `SLEEP_SESSION`（0x48）类型 | 是 |
| `calibrate_sleep.py` | 把四种睡眠字节读法并排比较；可用夜不足时拒绝拟合 | 否 |
| `check_file_sizes.py` | CI：第一方代码/资源文件 ≤300 行 | 否 |
| `check_privacy.py` | CI：Git 跟踪文件里不得有真实 MAC/密钥/家庭路径 | 否 |
| `check_docs_links.py` | CI：相对 Markdown 链接必须可解析 | 否 |

## 公开 API

根 `tools/*.py` 通过 `sys.path` 或包导入使用主实现（例如
`history.parsers.parse_spo2_sleep`、`storage.TelemetryStore`、`sleep.calibration`）。

## 数据流

```
真机 ──capture_history_payload.py──> validation/payloads/history_*.bin + .json
                                              │
                       import_spo2_sleep.py ──┴──> spo2_sleep.jsonl + mi_band.db
probe_*.py ──> 只读查询（无 ACK，不扰动手环缓冲）
calibrate_sleep.py ──> data/zepp_nights.json + SQLite → 对照表（不自动调参）
check_*.py ──> CI 失败/通过（文件体积、隐私、文档链接）
```

## 依赖

- `miband_monitor.ble/history/storage/sleep` 与 `config`；`bleak`、`cryptography`；
- 取证脚本输出到 `validation/`，该目录已被 `.gitignore` 忽略。

## 配置与常量

- 脚本从 `.env`/环境读取设备标识与密钥，不硬编码；`--scan-timeout` 建议 90–120；
- `calibrate_sleep.py` 使用 `data/zepp_nights.json`（个人数据，已忽略）；
- `check_file_sizes.py` 的排除列表就是 [architecture.md](../architecture.md#6-第三方与兼容豁免)
  中的第三方清单。

## 失败模式

| 情况 | 行为 |
|---|---|
| 无硬件/未配置凭据 | 探针脚本报错退出，不修改数据 |
| payload 版本/长度不符 | `import_spo2_sleep.py` 拒绝并说明原因 |
| 重复导入 | 按时间戳去重，幂等 |
| 取证数据含真实 MAC/健康数据 | 输出目录被忽略；文档与 issue 必须脱敏 |
| 文档链接失效 | `check_docs_links.py` 返回非 0 |
| 文件超 300 行 | `check_file_sizes.py` 返回非 0 |

## 测试

- `tests/test_import_spo2_sleep.py`：0x26 `.bin` 导入双写、幂等、dry-run、非法 payload；
- `tests/test_calibrate.py`：四种读法与"可用夜不足拒绝拟合"；
- `tests/test_probe_declared.py`：只写起始命令、绝不取数/回执，零填充日历不被误判；
- CI 直接运行三个 `check_*.py`。

## 相关文档

- [tools/README.md](../../tools/README.md)
- [guides/testing.md](../guides/testing.md)、[guides/privacy.md](../guides/privacy.md)
- [validation/history-hardware.md](../validation/history-hardware.md)
