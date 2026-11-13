# ble — BLE 客户端与协议

## 职责

实现与 Mi Band 6 的完整 BLE 会话：

- 设备发现（MAC / CoreBluetooth UUID / 广播名子串）与 BlueZ 直连快路径；
- Chunked Transfer 认证（0x0016 写 / 0x0017 通知）上的 ECDH + AES-CBC 挑战响应，
  带序号校验、重复包处理、越界保护与重组上限；
- 三类实时通知：标准心率 `0x2A37`、实时步数 `0x0007`、原始 IMU `0x0001/0x0002`；
- 心率保活、断链事件、MTU 协商与重连所需的状态复位。

源码：[miband_monitor/ble/](../../miband_monitor/ble/)

## 模块拆分

| 文件 | 内容 |
|---|---|
| `protocol.py` | UUID、`CHUNKED_*` 头长度、AUTH 尺寸、原始传感器命令与类型码 |
| `parsers.py` | `parse_heart_rate`、`parse_realtime_steps`、`parse_raw_sensor_data` |
| `identity.py` | `normalize_auth_key`、`is_placeholder_device_id`、`normalize_device_identifier`、`device_matches`、`_env_device_id`、`_secure_env_file` |
| `crypto.py` | `_aes_cbc_encrypt`、`encrypt_challenge` |
| `chunked.py` | `write_chunked_value`、`_write_with_in_progress_retry`（全局写锁） |
| `backends.py` | bleak 客户端/扫描器工厂选择 |
| `discovery.py` | 扫描、心跳、按名称/MAC/UUID 匹配 |
| `link.py` | 连接预算、瞬时错误重试、BlueZ 待命路径 |
| `mtu.py` | `_resolve_mtu` / `_apply_mtu` / `_settle_after_link` |
| `listener.py` | chunked 通知订阅与分发 |
| `auth_state.py` / `auth_connect.py` / `auth_handshake.py` | 认证事件、连接编排、挑战响应状态机 |
| `notifications.py` / `streams.py` | 心率/步数/IMU 通知处理与流启停 |
| `client.py` | `MiBand6Client` 组合上述 mixin，保持公开类身份与属性 |

## 公开 API

- 常量：全部 `UUID_*`（`0xfee0` 服务、心率服务、`0x0001/2/7/0x0016/17`）、
  `CHUNKED_*`、`AUTH_*`、`RAW_SENSOR_*`、`CMD_RAW_SENSOR_*`；
- 解析：`parse_heart_rate(data) -> int`、`parse_realtime_steps(data) -> dict`、
  `parse_raw_sensor_data(data) -> dict`；
- 认证/工具：`encrypt_challenge(auth_key, challenge_bytes)`、`normalize_auth_key(hex)`、
  `write_chunked_value(client, char_uuid, data, mtu)`、`device_matches(...)`、
  `_env_device_id()`、`_secure_env_file()`；
- 数据类型：`StreamCallbacks`（heart_rate/steps/imu/history_* 六个回调）；
- 客户端：`MiBand6Client(device_id, auth_key_hex, *, device_name, mtu, client_factory,
  scanner_factory, scan_timeout, connect_timeout, auth_timeout, direct_connect)`，
  主要方法 `connect_and_authenticate()`、`start_heart_rate_stream()`、
  `start_steps_stream()`、`start_raw_sensor_stream()`、
  `wait_until_disconnected()`、`disconnect()`。

## 数据流

```
connect_and_authenticate()
  ├─ (Linux) paired_device() 直连 → 否则扫描（最长 scan_timeout）
  ├─ _apply_mtu() / _settle_after_link()（Linux 等 SMP 结算）
  ├─ _start_chunked_listener()（订阅 0x0017）
  ├─ 认证：公钥 → 16B 盐 + 48B 公钥 → shared[8:24]^auth_key → AES 响应
  └─ authenticated=True
随后 runner 依次：start_raw_sensor_stream（可选）→ start_heart_rate_stream()
              → start_steps_stream()
通知回调 ──> StreamCallbacks ──> cli.callbacks ──> storage / dashboard
```

> 顺序很重要：真机上先启动原始传感器流、再启动心率流；反了会让标准心率特征静默饿死
> （实测 20 s 内 0 条通知 vs 正常约 1 条/秒）。

## 依赖

- `miband_monitor.bluez`（Linux 兼容）、`crypto`（ECDH）、`config`（默认值与脱敏）；
- 第三方 `bleak` + `cryptography`；
- 不依赖 `storage/dashboard`：所有输出通过回调交给调用方。

## 配置与常量

| 项 | 默认 | 说明 |
|---|---|---|
| `BLE_MTU` / `--mtu` | 自动 | 手动覆盖 ATT MTU（23–517），无依据时不要设置 |
| `SCAN_TIMEOUT` / `--scan-timeout` | 90 s | 等待孤立广播脉冲，建议 120 |
| `CONNECT_TIMEOUT` / `--connect-timeout` | 75 s | Linux 至少覆盖 70 s 广播周期 |
| `--no-direct-connect` | 直连开启 | Linux 强制走扫描路径排障 |
| `CHUNKED_*` | — | 首片 11B 头 / 后续 5B 头，按 MTU 计算分片载荷 |
| `AUTH_STEP1_*` | 16+48 | 随机盐 + 公钥；重组上限 `MAX_REASSEMBLY_BYTES=1024` |
| `RAW_SENSOR_ACCEL_LSB_PER_G` | 4096 | int16 加速度换算 |

## 失败模式

| 情况 | 行为 |
|---|---|
| 扫描超时 | 打印发现目标与"每 40–60 s 一个脉冲"提示，会话失败并按退避重连 |
| 认证分片缺口/重复/越界 | 记录序号错误并失败；下次会话重试，不会拼接错位数据 |
| 认证超时 | `auth_timeout`（默认 30 s）后重连；DEBUG 日志保留帧摘要（不含密钥） |
| 心跳保活写失败 | 视为链路丢失，触发重连 |
| GATT `InProgress` | `_write_with_in_progress_retry` 串行化并重试 |
| 设备名/MAC 不匹配 | 明确日志"发现目标"；macOS 用 UUID 或 `DEVICE_NAME` |
| IMU 无数据 | 部分固件需特定运动模式；`--imu` 为实验性功能 |
| 回调抛异常 | 在通知处理内记录并继续，不阻塞事件循环 |

## 测试

- `tests/test_chunked_protocol.py`：分块帧格式、MTU 协商与透传、认证重组（缺口/重复/越界/非法长度）；
- `tests/test_device_matching.py`：MAC/UUID/名称匹配、占位符与优先级、CLI 参数；
- `tests/test_simulated_band.py`：模拟手环端到端认证、三类数据流、重连、认证期间断连快速失败；
- `tests/test_ecdh_backends.py`：认证所用 ECDH 后端的字节级一致性；
- 重构后按 `tests/simulator/{test_auth,test_streams,test_monitor}.py` 组织。

## 相关文档

- [modules/crypto.md](crypto.md)、[modules/bluez.md](bluez.md)、[modules/simulator.md](simulator.md)
- [modules/cli.md](cli.md)（会话监督与重连策略）
- [guides/cross-platform.md](../guides/cross-platform.md)、[guides/data-formats.md](../guides/data-formats.md)
