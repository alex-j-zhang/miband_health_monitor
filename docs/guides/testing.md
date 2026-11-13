# 测试与校验

## 1. 无硬件测试

```bash
.venv/bin/python -m pytest -q
```

不需要任何蓝牙硬件：模拟手环实现了认证、实时流与历史 fetch 的同一套线协议。
基线随重构推进而变化，以实际输出为准（重构前基线：270 collected / 266 passed / 4 skipped；
跳过项需要真实抓包数据）。CI 在 Linux / Windows / macOS × Python 3.10/3.12/3.13 上运行同一套测试。

## 2. 测试布局

当前与重构后的布局（重构期间根目录测试文件仍可用）：

| 领域 | 当前文件 | 重构后目录 |
|---|---|---|
| BLE 协议 | `tests/test_chunked_protocol.py`、`tests/test_device_matching.py` | 保持/并入 `tests/simulator` |
| BlueZ | `tests/test_bluez_support.py` | `tests/bluez/{test_platform,test_devices,test_link,test_pairing,test_errors,test_client_integration,test_doctor}.py` |
| ECDH | `tests/test_ecdh_backends.py` | 保持 |
| 历史 | `tests/test_history.py` | `tests/history/{test_codecs,test_parsers,test_fetcher,test_sync,test_report}.py` |
| 睡眠 | `tests/test_sleep.py` | `tests/sleep/{test_detection,test_classification,test_session,test_cli}.py` |
| 模拟手环 | `tests/test_simulated_band.py` | `tests/simulator/{test_auth,test_streams,test_monitor}.py` |
| 离线看板 | `tests/test_history_dashboard.py` | `tests/offline_dashboard/{test_collect,test_render}.py` |
| 实时看板 | `tests/test_dashboard.py` | `tests/dashboard/{test_hub,test_server,test_cli}.py` |
| 存储 | `tests/test_storage.py` | 保持 |
| 工具 | `tests/test_import_spo2_sleep.py`、`test_calibrate.py`、`test_probe_declared.py` | 保持 |
| 加密/解析基础 | `test_miband.py` | `tests/test_crypto_parsers.py` |

各模块文档的"测试"一节列出了该领域最关键的行为断言。

## 3. CI 流水线

[.github/workflows/ci.yml](../../.github/workflows/ci.yml) 的步骤：

1. 安装依赖；
2. `compileall` 编译 `miband_monitor`、根模块、`tests`、`tools`；
3. `tools/check_file_sizes.py`：第一方代码/资源 ≤300 行；
4. `tools/check_privacy.py`：Git 跟踪文件不得含真实 MAC/密钥/路径；
5. `tools/check_docs_links.py`：相对 Markdown 链接必须可解析；
6. `pytest -q`；
7. `MIBAND_ECDH_BACKEND=python` 的 ECDH 往返冒烟；
8. demo 全链路：`--demo 2 --imu --sync-history 1 --sync-sleep-spo2 --storage both --export-csv`；
9. 校验 demo 产物非空（JSONL、`mi_band.db`、CSV）。

本地可逐条运行这些命令；链接检查失败会直接打印 `FAIL <文件> -> <目标>`。

## 4. 演示冒烟（跨平台推荐）

```bash
.venv/bin/python miband_client.py --demo 2 --imu --sync-history 1 \
    --sync-sleep-spo2 --output data/ci-demo --storage both --export-csv
```

应生成 `heart_rate/steps/imu/spo2/spo2_sleep.jsonl`、`mi_band.db` 与 6 张 CSV。
它不依赖蓝牙，是 Windows/macOS 上验证安装是否成功的最快方式。

## 5. 真机验证

真机行为无法在 CI 覆盖，使用以下清单人工验证：

- 历史协议（活动/血氧/0x26）：[validation/history-hardware.md](../validation/history-hardware.md)；
- Linux 软件链路与真机：[validation/linux-validation.md](../validation/linux-validation.md)；
- Linux 连接稳定性根因与验证：[validation/linux-ble-connect-findings.md](../validation/linux-ble-connect-findings.md)；
- 三平台验证清单：[cross-platform.md](cross-platform.md)。

## 6. 取证与校准工具

```bash
# 保留原始 payload 作为硬证据
.venv/bin/python tools/capture_history_payload.py --days 7 --output validation/payloads

# 导入 0x26 取证数据（幂等）
.venv/bin/python tools/import_spo2_sleep.py --output data validation/payloads/history_26_*.bin

# 睡眠读法对照（可用夜不足时拒绝拟合）
.venv/bin/python tools/calibrate_sleep.py --zepp data/zepp_nights.json
```

这些脚本的测试见 `tests/test_import_spo2_sleep.py`、`tests/test_calibrate.py`、
`tests/test_probe_declared.py`。取证产物含个人数据，`validation/` 已被忽略，
对外反馈前必须脱敏（见 [privacy.md](privacy.md)）。

## 7. 写测试的约定

- 不接触真实蓝牙：用 `simulator` 的假后端或 monkeypatch 后端；
- 不依赖网络与真实账号；
- 时间相关用例用注入的 `now`/固定时间戳，避免 flaky；
- 断言公开行为与数据格式，不断言内部实现细节；
- 新增/修改数据格式时必须同时更新 [data-formats.md](data-formats.md) 与相关模块文档。
