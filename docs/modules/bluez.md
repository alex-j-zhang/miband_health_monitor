# bluez — Linux / BlueZ 兼容层

## 职责

Linux 下 BlueZ 与 WinRT/CoreBluetooth 有三处关键差异，本模块把差异封装成可测试的助手：

1. **D-Bus 设备对象是临时的**：未配对/未信任的设备断链后会从对象树消失，后续 GATT 写报
   `UnknownObject: Method "WriteValue" ... doesn't exist`；未绑定的手环还会在约 40 s 后断链。
2. **bleak 传 MAC 连接前会重新扫描**：已配对设备可直接通过 BlueZ 缓存对象连接，无需等广播。
3. **ATT MTU 不会自动获取**：不调用 `_acquire_mtu()` 时 `mtu_size` 恒为 23。

所有助手在非 BlueZ 平台优雅降级为空操作，因此 Windows/macOS 也能安全 import。

源码：[miband_monitor/bluez/](../../miband_monitor/bluez/)

## 模块拆分

| 文件 | 内容 |
|---|---|
| `constants.py` | 服务/接口/对象路径、默认 adapter、SMP 结算与超时 |
| `platform.py` | `is_bluez_platform`、地址规范化 / MAC 判定 |
| `dbus.py` | `_managed_objects` 与 D-Bus 基础调用 |
| `devices.py` | `list_known_devices`、`find_device`、`make_ble_device`、`connected_device`、`paired_device`、`active_cached_device` |
| `admin.py` | `set_adapter_pairable`、`set_device_trusted`、`disconnect_device` |
| `link.py` | `link_standby_budget`、`wait_for_bluez_link`（待命 Connect 循环） |
| `pairing.py` | `BlueZPairingAgent`（`NoInputNoOutput` 进程内 agent） |
| `mtu.py` | `try_acquire_mtu` |
| `scanner.py` | `scanner_kwargs`（`DuplicateData=False` 等） |
| `errors.py` | 瞬时错误判定（`le-connection-abort-by-local`、`InProgress`）与 `describe_ble_error` |

## 公开 API

`link_standby_budget(connect_timeout)`、`is_bluez_platform()`、`normalize_address()`、
`looks_like_mac()`、`device_path_for_address()`、`list_known_devices()`、
`find_device(address)`、`list_adapters()`、`make_ble_device(info)`、
`connected_device(address)`、`paired_device(address)`、`active_cached_device(...)`、
`is_transient_connect_error()`、`is_in_progress_error()`、`scanner_kwargs()`、
`set_adapter_pairable()`、`set_device_trusted()`、`disconnect_device()`、
`wait_for_bluez_link()`、`BlueZPairingAgent`、`try_acquire_mtu(client)`、
`describe_ble_error(exc)`。

## 数据流

```
ble.discovery
  ├─ paired/cached? ──paired_device()/active_cached_device()──> make_ble_device()
  │                                      │
  └─ 否 ── scanner_kwargs() + bleak 扫描 ─┘
                                          v
                              wait_for_bluez_link() 待命 Device1.Connect
                                          │ 成功
                                          v
                              try_acquire_mtu() ──> MTU 247（实测）
```

配对：`MIBAND_BLUEZ_AGENT=1` 时由 `BlueZPairingAgent` 在系统 D-Bus 注册
`org.bluez.Agent1`，自动接受目标手环的 SMP 请求；否则由用户 `bluetoothctl pair`。

## 依赖

- 运行时：Linux + BlueZ + `dbus-fast`（随 `bleak` 安装）；非 Linux 全部降级。
- 项目内：只依赖 `miband_monitor.config`，不导入 `ble`（避免循环）。
- `ble` 通过公共函数导入本包；`diagnostics` 用 `list_adapters/find_device` 做自检。

## 配置与常量

| 变量 | 默认 | 说明 |
|---|---|---|
| `MIBAND_BLUEZ_TRUST` | `1` | 连接前把设备设为 `Trusted`，换取 GATT 缓存与内核自动回连 |
| `MIBAND_BLUEZ_AGENT` | `0` | `1` 注册进程内配对 agent |
| `MIBAND_BLUEZ_DUPLICATE_DATA` | `0` | BT 5/6 上 `True` 会每 ~5 s 重启扩展扫描，易扫死适配器 |
| `MIBAND_SMP_SETTLE_DELAY` | `1.5` | `ServicesResolved` 后等待 SMP/profile 探测结算的秒数 |

关键常量：`BAND_PULSE_PERIOD ≈ 70 s`（待命连接预算下限）、`DBUS_TIMEOUT`、
`AGENT_MANAGER_*`。

## 失败模式

| 现象 | 原因 | 处理 |
|---|---|---|
| `failed to discover services, device disconnected`（约 9–10 s） | 未 trust，手环未认证连接看门狗到期（需枚举 ~150 个 GATT 句柄） | 自动重试；`bluetoothctl trust <MAC>` 后 BlueZ 缓存服务表 |
| `UnknownObject ... doesn't exist` | 未配对/已断链后 BlueZ 移除设备对象 | `describe_ble_error` 翻译为可操作提示；重连 |
| `le-connection-abort-by-local` | 内核 40 s 发起周期未等到脉冲 | `wait_for_bluez_link` 约 150 ms 后原地重挂，**不主动 Disconnect** |
| `-112 / host is down`、零广播 | BARROT `33fa:0012` 等适配器射频假死 | 只能 USB 复位：`usbreset` 或拔插 + `systemctl restart bluetooth`；D-Bus 电源循环无效 |
| `No agent available for request type 2` | 手环发起 SMP 而系统无 agent | `MIBAND_BLUEZ_AGENT=1` 或 `bluetoothctl` 手工配对 |
| MTU 恒 23 | 未调用 `_acquire_mtu()` | 当前版本主动协商；仍出现时查 `try_acquire_mtu` 日志 |

## 测试

- `tests/test_bluez_support.py`（重构后拆到 `tests/bluez/test_platform.py`、`test_devices.py`、
  `test_link.py`、`test_pairing.py`、`test_errors.py`、`test_client_integration.py`、
  `test_doctor.py`）：D-Bus 属性解析、地址/对象路径、待命预算、agent 生命周期、错误翻译、
  与客户端集成的 monkeypatch 路径。
- 真机证据：[validation/linux-validation.md](../validation/linux-validation.md)、
  [validation/linux-ble-connect-findings.md](../validation/linux-ble-connect-findings.md)。

## 相关文档

- [guides/cross-platform.md](../guides/cross-platform.md)
- [modules/ble.md](ble.md)（谁调用这些助手）
- [modules/diagnostics.md](diagnostics.md)（`--doctor` 如何展示适配器状态）
