# simulator — 内存模拟手环

## 职责

在没有任何蓝牙硬件的情况下，用纯内存对象冒充 Mi Band 6，使 `--demo` 与集成测试能跑通
完整链路（认证 → 实时流 → 历史同步 → 落盘 → 导出）：

- 实现 chunked 帧、ECDH + AES-CBC 认证、心率/步数/IMU/历史通知；
- 提供假的 bleak 客户端与扫描器，可直接注入 `MiBand6Client`；
- 可配置真实世界故障：认证拒绝、丢认证分片、连接后断链。

源码：[miband_monitor/simulator/](../../miband_monitor/simulator/)

## 模块拆分

| 文件 | 内容 |
|---|---|
| `constants.py` | 假 MAC/名称、RSSI、chunked 头与 AUTH 常量 |
| `payloads.py` | 心率/步数/IMU/血氧/活动/睡眠血氧报文构造器 |
| `devices.py` | `FakeDevice`、`FakeAdvertisement` |
| `auth.py` | `AuthMixin`：认证状态机与响应 |
| `history.py` | `HistoryMixin`：fetch start-date / 分片 / 完成 / ACK |
| `streams.py` | `StreamMixin`：三类实时通知的产生与停止 |
| `band.py` | `SimulatedBand`：连接生命周期 + GATT 分发（组合上述 mixin） |
| `fake_bleak.py` | `FakeBleakClient`、`FakeBleakScanner` |
| `factory.py` | `simulated_factories`、`make_simulated_client` |

## 公开 API

`SimulatedBand(auth_key=..., fail_auth=..., drop_auth_seqs=..., disconnect_after=...)`、
`FakeDevice`、`FakeAdvertisement`、`FakeBleakClient`、`FakeBleakScanner`、
`simulated_factories(band)`、`make_simulated_client(band, ...)`；
测试还使用 `_steps_packet`、`_imu_packet`、`_activity_payload` 等报文构造器
（`__init__` 已重导出）。

## 数据流

```
cli.runner.run_demo
  └─ SimulatedBand(auth_key) ──> simulated_factories() ──> fake BleakClient/Scanner
                                    │
                     MiBand6Client(真实认证/解析代码)
                                    │
                 StreamCallbacks ──> storage / dashboard（与真机相同）
```

## 依赖

- `miband_monitor.ble`（客户端只用真实实现）、`history`（协议常量/编解码可复用）；
- 标准库 `asyncio`、`struct`；AES 使用 `cryptography`（与客户端一致）；
- 不被生产代码导入，只有 CLI demo 与测试使用。

## 配置与常量

- `DEMO_DEVICE_ID`（`C0:FF:EE:C0:FF:EE`）、`DEMO_DEVICE_NAME`、`DEMO_AUTH_KEY_HEX`
  来自 [config/demo.py](../../miband_monitor/config/demo.py)；
- 模拟数据是合成值，不含任何真实健康数据；
- `--demo [duration]` 与 `DEMO_MODE=1` 触发。

## 失败模式

| 模拟项 | 用途 |
|---|---|
| `fail_auth` | 验证认证失败路径与重连/报错 |
| `drop_auth_seqs` | 验证分片缺口检测与重组健壮性 |
| `disconnect_after` | 验证断链检测与指数退避重连 |
| 历史 fetch 丢包 | 验证完成帧/ACK 与重试 |
| 未知 GATT 操作 | 抛异常，帮助发现客户端对特征能力的假设 |

## 测试

- `tests/test_simulated_band.py`（重构后 `tests/simulator/{test_auth,test_streams,test_monitor}.py`）：
  端到端认证、三类数据流、断连续连、落盘与导出、认证期间断连快速失败
  （重连用例按 ECDH 后端自适应时序窗口）。
- CI 直接运行 `python miband_client.py --demo 2 --imu --sync-history 1 --sync-sleep-spo2 ...`
  并校验产物非空。

## 相关文档

- [modules/ble.md](ble.md)
- [guides/testing.md](../guides/testing.md)
- [guides/usage.md](../guides/usage.md)（demo 用法）
