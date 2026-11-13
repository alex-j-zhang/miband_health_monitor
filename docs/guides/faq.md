# 常见问题（FAQ）

按现象查表；平台细节见 [cross-platform.md](cross-platform.md)，配置见 [configuration.md](configuration.md)。

## 连接与扫描

| 现象 | 原因/处理 |
|---|---|
| `No Bluetooth adapters found` / `org.bluez was not provided` | 本机无蓝牙栈（如 WSL2 未直通、`bluetooth` 服务未启动）。安装/启动 BlueZ，或直通 USB 适配器。 |
| 90 秒内扫描不到手环 | 手机关蓝牙；用 Zepp Life 连一次手环恢复"公共广播"易失状态；确认 MAC/UUID；手环每 40–60 秒才发一个脉冲，`--scan-timeout 120`。 |
| 扫描器在跑、硬件计数在涨，但 25 秒内**连别的 BLE 设备都没有一个包** | USB 适配器射频假死（常见于 BARROT `33fa:0012`）：`sudo usbreset <vid:pid>` 或拔插 USB 后 `sudo systemctl restart bluetooth`。D-Bus 电源循环实测无效。见 [validation/linux-ble-connect-findings.md](../validation/linux-ble-connect-findings.md)。 |
| Linux 建链后 9–10 秒被断，`failed to discover services, device disconnected` | 手环未认证连接看门狗（首次要枚举 ~150 个 GATT 句柄）。客户端自动重试；先 `bluetoothctl trust <MAC>`。 |
| 连上 40 秒左右就断，`UnknownObject ... doesn't exist` | 未 trust：BlueZ 在断链后移除设备对象。`bluetoothctl trust <MAC>`，必要时配对。 |
| 连接时 BlueZ 日志 `No agent available for request type 2` | 手环发起 SMP 而系统无 agent：`MIBAND_BLUEZ_AGENT=1` 或手工 `bluetoothctl pair`。 |
| 每次都要等几十秒才连上 | 正常：无手机链路的手环每 40–60 秒才发一个脉冲。trust 后 BlueZ 缓存 GATT 表并自动回连。 |
| Windows：`BleakError ... not found` / 连接被拒绝 | "设置 → 蓝牙和其他设备"确认适配器；删除设备旧记录；关手机蓝牙；用 `scan_ble.py` 核对地址。 |
| Windows：`OSError: [WinError 193]` / 加载 `libecdh.so` 失败 | 当前版本会自动回退纯 Python；确认启动日志 `ECDH backend: python`。 |
| macOS：扫描不到 / 地址对不上 | 不暴露 MAC：用 `scan_ble.py` 输出的 UUID 填 `DEVICE_ID`，或配置 `DEVICE_NAME`；系统设置允许终端访问蓝牙。 |

## 认证与数据流

| 现象 | 原因/处理 |
|---|---|
| 认证超时 | 分块通知丢失或固件差异；`--log-level DEBUG` 查看；客户端下次会话自动重试。 |
| 认证成功但心率不动 | 手环需佩戴贴合；`0x2A39` 控制序列可能被固件拒绝；重连或重启手环。 |
| IMU 无数据 | 部分固件需先进入特定运动模式；`--imu` 为实验性功能。 |
| 数据重复（步数） | 步数流先读一次当前值再订阅通知，首条可能重复，属预期。 |
| 时间戳含义 | 实时流为电脑接收时间；历史同步为手环采样时间（见 [data-formats.md](data-formats.md)）。 |

## 历史同步

| 现象 | 原因/处理 |
|---|---|
| `--sync-history` 报 `band rejected fetch type` | 该固件/型号不支持对应历史类型；失败不影响实时采集。 |
| `--sync-history` 超时/校验失败 | 分片丢失或固件差异；重连会重试；真机请附 DEBUG 日志。 |
| `--sync-history 7` 却只拿到 40 小时 | 正常。参数只是"我最多问多久"，手环环形缓冲约 2400 分钟；起点按 1:1 前移。 |
| Zepp 上有数据，手环里取不到 | 三种原因：① 已被 App 取走（40 小时外有永久空洞或超窗）；② 手环从不产这类数据（深睡/REM/得分是 App 现算）；③ 那一夜未佩戴（`0xF3`），App 显示的是同账号另一台设备。见 [investigations/sleep-fields.md](../investigations/sleep-fields.md)。 |
| 想确认手环还留着多早的数据 | `python tools/probe_declared.py 168 24`：只问不取、不发 ACK，不扰动缓冲。 |
| 0x26 睡眠血氧导入后没看到 | 确认写入独立 `spo2_sleep` 表；离线看板会优先读表、表空回退 JSONL。 |

## 看板与报告

| 现象 | 原因/处理 |
|---|---|
| 看板打不开 / 端口占用 | `--web-port 0` 用随机端口；默认仅本机，远程需 `--web-host 0.0.0.0` 并注意防火墙。 |
| 看板有数据但曲线不动 | SSE 断线会自动重连；确认页面未休眠或刷新页面重新拉 snapshot。 |
| 看板刷新后历史为空 | 需要 `--storage sqlite/both` 且库中有数据；`source=db` 在缺库/缺表时静默降级。 |
| `history_report.py` 报数据库不存在 | 先生成数据：`miband_client.py 60` 或 `--demo`。 |
| 睡眠报告某夜显示 `unknown` | 该夜无心率：只能给睡眠时长，不能猜分期。 |
| 整夜未佩戴却被算成睡眠 | 当前版本用 `kind & 0x0F == 3` 判定未佩戴，不生成睡眠段；若仍异常请附数据。 |

## 退出与稳定性

| 现象 | 原因/处理 |
|---|---|
| 连接一段时间后断开 | 手环省电/离体/手机抢占；默认指数退避自动重连。 |
| Ctrl+C 后没有汇总 | 当前版本会打印 `Storage summary`；若没有，可能进程被强杀或日志被重定向。 |
| 采集窗口结束但没有 "Reconnecting" 日志 | 属预期：`duration` 用尽后正常返回，不是重连失效。 |

## 历史问题状态（Linux，2026-11-08 记录）

[validation/linux-validation.md](../validation/linux-validation.md) 的 L1–L10 中：

- **L9（未配对约 36 秒断链 + 误导性 D-Bus 报错）已修复**：文档补配对/trust 步骤，
  D-Bus 错误被翻译为可操作提示；
- **L10（ATT MTU 从未协商）已修复**：连接后显式 `_acquire_mtu()`，实测 MTU 23 → 247；
- 其余为文档/脚本可用性/静默行为类问题，按对应章节处理。

## 仍然无解/待验证

- Windows/macOS 真机行为仍需在对应系统验证（代码层兼容已完成）；
- 手环三字节"阶段分数"与 Zepp 时长的映射未定案，睡眠分期为推导值；
- 0x26 的 `duration/signal/extend` 语义未定，仍标 `[experimental]`。
