# 配置

配置来源优先级：**命令行参数 > 进程环境变量 > `.env` > 内建默认值**。
默认值集中在 `miband_monitor/config/defaults.py`，`.env` 模板见
[.env.example](../../.env.example)。

## 1. 准备 .env

```bash
cp .env.example .env
```

`.env` 已被 `.gitignore` 忽略；程序在启动时会尝试把它收紧到 0600。
**不要提交真实 auth_key**。

## 2. 变量表

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `DEVICE_ID` | 是 | — | 手环标识：Linux/Windows 用 MAC（如 `C0:FF:EE:C0:FF:EE`）；macOS 用 CoreBluetooth UUID |
| `DEVICE_MAC` | 否 | — | 旧版别名，仍兼容；同时配置时 `DEVICE_ID` 优先（占位符会被忽略） |
| `DEVICE_NAME` | 否 | — | 广播名子串匹配（大小写不敏感），如 `Mi Smart Band`；macOS/多设备时实用 |
| `AUTH_KEY` | 是 | — | 32 位十六进制认证密钥（16 字节） |
| `BLE_MTU` | 否 | 自动 | 手动覆盖 ATT MTU（23–517）；一般留空，自动探测 |
| `OUTPUT_DIR` | 否 | `data` | JSONL 与 `mi_band.db` 输出目录 |
| `STORAGE_BACKEND` | 否 | `both` | `jsonl` / `sqlite` / `both` / `none` |
| `SCAN_TIMEOUT` | 否 | `90` | 等待广播的秒数；无手机链路的手环每 40–60 秒才发一个脉冲，建议 ≥120 |
| `CONNECT_TIMEOUT` | 否 | `75` | 单次连接预算；Linux 上会被抬到至少 70 秒（覆盖一个广播周期） |
| `LOG_LEVEL` | 否 | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `DEMO_MODE` | 否 | `0` | 设为 `1` 等价于 `--demo` |
| `WEB_PORT` | 否 | — | 实时看板端口；留空关闭，`0` 表示随机空闲端口 |
| `WEB_HOST` | 否 | `127.0.0.1` | 看板绑定地址；默认仅本机可访问 |
| `HISTORY_DAYS` | 否 | — | 设为 N 等价于 `--sync-history N` |
| `MIBAND_ECDH_BACKEND` | 否 | `auto` | 诊断用：`auto` / `native` / `python` |
| `MIBAND_ECDH_LIB` | 否 | — | 显式指定原生 ECDH 库路径 |
| `MIBAND_BLUEZ_TRUST` | 否 | `1` | Linux：连接前把设备设为 `Trusted`；`0` 仅用于 A/B 实验 |
| `MIBAND_BLUEZ_DUPLICATE_DATA` | 否 | `0` | Linux：`1` 恢复 `DuplicateData=True`（BT 5/6 上易扫死适配器） |
| `MIBAND_SMP_SETTLE_DELAY` | 否 | `1.5` | Linux：`ServicesResolved` 后等待 SMP/profile 结算的秒数 |
| `MIBAND_BLUEZ_AGENT` | 否 | `0` | Linux：`1` 注册进程内 `NoInputNoOutput` 配对 agent |

## 3. 获取 auth_key

三选一：

1. **小米账号**（交互式，密码不落盘）：

   ```bash
   .venv/bin/python get_auth_key.py     # 选择 1
   ```

2. **Zepp/Amazfit 账号**：

   ```bash
   .venv/bin/python get_auth_key.py     # 选择 2
   ```

3. **浏览器 app_token**（已有网页登录态）：

   ```bash
   .venv/bin/python fetch_by_token.py   # 输入 user_id 和 app_token
   ```

已有第三方 `huami-token` 输出时，直接填入 `.env` 亦可。
`AUTH_KEY` 等价于手环配对凭据，请视同密码保管；详见 [privacy.md](privacy.md)。

## 4. 命令行参数

```
python miband_client.py [duration] [选项]

duration              采集秒数；省略则一直运行到 Ctrl+C
--imu                 同时采集原始 IMU 加速度（约 25Hz；会先于心率流启动）
--sync-history [DAYS] 连接后拉取最近 DAYS 天手环历史（默认 7；实验性）
--sync-sleep-spo2     额外拉取 0x26 整夜血氧（需配合 --sync-history）
--demo                使用内存模拟手环（无需蓝牙硬件）
--device-id ID        覆盖 DEVICE_ID（MAC；macOS 为 UUID）
--device-name NAME    广播名子串匹配（额外发现条件）
--mtu BYTES           手动覆盖 ATT MTU（23–517；默认自动探测）
--web-port PORT       启动实时看板/HTTP API（0 = 随机空闲端口）
--web-host HOST       看板绑定地址（默认 127.0.0.1）
--output DIR          输出目录（默认 $OUTPUT_DIR 或 data）
--storage MODE        jsonl | sqlite | both | none（默认 both）
--export-csv          会话结束后把 SQLite 导出为 CSV
--scan-timeout SEC    扫描等待秒数（默认 90）
--connect-timeout SEC 单次连接预算（默认 75）
--log-level LEVEL     DEBUG | INFO | WARNING | ERROR
--no-reconnect        出错/断连后不自动重连
--doctor              只做环境自检，不连接手环
--no-direct-connect   Linux：强制走扫描路径（排查直连缓存问题）
```

## 5. 常用组合

```bash
# 无硬件演示（含历史同步/睡眠血氧/CSV）
python miband_client.py --demo 2 --imu --sync-history 1 --sync-sleep-spo2 \
    --output data/demo --export-csv

# 长时间采集 + 看板 + 结束后导出
python miband_client.py --web-port 8080 --export-csv

# 只同步历史，不落实时流
python miband_client.py 1 --sync-history 3 --storage both

# 研究 ECDH 原生后端问题
MIBAND_ECDH_BACKEND=native python miband_client.py --doctor
```

## 6. 校验与故障

- 启动日志前两行会打印 `ECDH backend` 与 `Discovery target`，反馈问题时请一并附上；
- 配置错误通常被明确捕获：未配置设备/密钥、`--mtu` 越界、`--sync-sleep-spo2` 缺前置参数；
- 权限/环境问题先跑 `--doctor`；
- 平台专项说明见 [cross-platform.md](cross-platform.md)，报错速查见 [faq.md](faq.md)。
