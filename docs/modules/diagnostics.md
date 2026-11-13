# diagnostics — 环境自检与扫描排障

## 职责

把"为什么连不上"从日志猜测变成可复制的检查步骤：

- `miband_client.py --doctor`：只读检查配置、ECDH 后端、输出目录、以及（Linux）BlueZ
  适配器/设备状态；不连接手环、不修改系统设置；失败时打印可复制的修复命令。
- `scan_ble.py`：一次性扫描并按"手环/其它设备"分类列出地址与 RSSI。
- `scan_live.py`：连续扫描并逐包打印，用于抓 40–60 s 才出现一次的孤立广播脉冲。

源码：[miband_monitor/diagnostics/](../../miband_monitor/diagnostics/)

## 模块拆分

| 文件 | 内容 |
|---|---|
| `report.py` | `_Report`、`OK/INFO/WARN/FAIL` 级别与统一输出格式 |
| `checks.py` | 单检查项：`.env` 权限、依赖版本、ECDH 后端、输出目录可写、设备标识格式 |
| `doctor.py` | `run_doctor(device_id, auth_key, output)` 编排，返回 0/1 |
| `scanning.py` | `scan_devices()`（`scan_ble.py` 的实现） |
| `live_scan.py` | 连续扫描主循环（`scan_live.py` 的实现），Linux 复用 `bluez.scanner_kwargs()` |

## 公开 API

| 名称 | 说明 |
|---|---|
| `run_doctor(device_id, auth_key, output) -> int` | 异步自检；0 = 全部通过，1 = 有 FAIL |
| `_Report` | 收集检查结果并格式化输出（供测试断言） |
| `scan_devices(duration_seconds=90)` | bleak 扫描，区分手环与其它 BLE 设备 |
| `main()`（live scan） | 连续打印 `[FOUND] address | name | rssi` |

## 数据流

```
cli.args --doctor ──> cli.app.main ──> diagnostics.doctor.run_doctor
                                         ├─ config.env / config.privacy（脱敏）
                                         ├─ crypto.facade.backend_name()
                                         ├─ storage.secure_path / 输出目录探测
                                         └─ bluez.list_adapters()/find_device()
scan_ble.py / scan_live.py ──> diagnostics.scanning / live_scan ──> bleak
```

## 依赖

- `miband_monitor.config`、`crypto`、`bluez`；扫描脚本还依赖 `bleak`。
- 不依赖 `ble.client`，因此自检不需要认证代码路径。

## 配置与常量

- 扫描默认 90 s（`SCAN_TIMEOUT`）；手环无手机链路时每 40–60 s 才发一个脉冲，
  短窗口几乎必然扫不到；
- `--doctor` 使用 `--output`、`DEVICE_ID`/`DEVICE_MAC`、`AUTH_KEY`（只检查格式，不打印明文）；
- Linux 检查 trusted/paired/connected、适配器 powered，以及 `.env` 实际权限。

## 失败模式

| 情况 | 输出/行为 |
|---|---|
| 未配置设备标识或密钥 | `[FAIL]` + 指向 `.env.example` 的提示，退出码 1 |
| `.env` 权限过宽 | `[WARN]`（或 FAIL，取决于平台）并给出 `chmod 600` 命令 |
| 适配器未上电/无适配器 | `[FAIL]`，提示启动 `bluetooth` 服务 |
| 设备未 trust/pair | `[WARN]`，附 `bluetoothctl trust/pair` 命令 |
| 输出目录不可写 | `[FAIL]`，提示换 `--output` |
| 非 Linux 平台 | 跳过 BlueZ 检查，其余照常 |
| 扫描 90 s 无结果 | 打印"手环可能未广播"与手机蓝牙/脉冲周期提示，退出码非 0 |

## 测试

- `tests/test_bluez_support.py::test_doctor_*`（重构后 `tests/bluez/test_doctor.py`）：
  报告级别、脱敏、输出目录失败、BlueZ 检查的 mock。
- `tests/test_probe_declared.py` 覆盖 `tools/` 探针的参数与安全行为（不发 ACK）。
- 真机排障记录：[validation/linux-validation.md](../validation/linux-validation.md)（L1–L10、P0 复验）。

## 相关文档

- [guides/faq.md](../guides/faq.md)
- [guides/cross-platform.md](../guides/cross-platform.md)
- [modules/bluez.md](bluez.md)
