# 小米手环 6 健康数据实时监测（Mi Band 6 Health Monitor）

通过 **BLE 直连** 小米手环 6，绕过手机 App，把心率、实时步数和原始 IMU 加速度数据
实时传输到电脑，并带时间戳落地为 JSONL / SQLite，可一键导出 CSV，便于后续分析与二次开发。

---

## 1. 功能概览

| 能力 | 说明 |
|---|---|
| 手环直连 | 基于 [`bleak`](https://github.com/hbldh/bleak)（Linux/BlueZ）扫描、连接、断连自动重连 |
| 安全认证 | 华米 Chunked Transfer + NIST B-163 ECDH + AES-CBC 挑战响应；分片重组带序号校验/越界保护/重复包处理 |
| 心率 | 标准 BLE SIG Heart Rate 服务（`0x2A37` / `0x2A39`），连续测量 + 12s 保活 |
| 实时步数 | 私有特征 `0x0007`：步数 / 距离 / 卡路里 |
| 原始 IMU | 私有特征 `0x0001`/`0x0002`，约 25 Hz 三轴加速度（需 `--imu`） |
| 实时看板 | `--web-port` 启动内嵌 HTTP 服务：SSE 实时推送 + 单页图表（心率/血氧/步数/IMU），**无第三方依赖** |
| 血氧 SpO₂ | 从手环侧历史记录拉取（**实验性**，需 `--sync-history`） |
| 历史活动数据 | 每分钟活动强度/步数/心率/睡眠标志（**实验性**，需 `--sync-history`） |
| 本地历史报告 | `history_report.py` 汇总 SQLite 中的心率/血氧/活动数据，支持文本与 JSON |
| 时间戳 | 每条记录带 Unix 秒 + UTC ISO-8601 毫秒时间；历史数据为手环采样时间 |
| 持久化 | JSONL（逐行追加，方便流式处理）与 SQLite（WAL，方便查询/分析）可任选或同时 |
| CSV 导出 | 从 SQLite 导出 5 张 CSV 表 |
| 自动重连 | 会话失败/链路丢失后指数退避重连；心率保活失败也会触发重连 |
| 无硬件演示 | `--demo` 使用内置模拟手环跑通完整链路（认证、数据流、历史同步、落盘、导出） |
| 测试 | 62 个单元/集成测试，含模拟 BLE 后端，**不需要真实手环** |

---

## 2. 工作原理

```
┌──────────────┐   1. 账号登录（可选，仅首次）   ┌─────────────────────┐
│ 小米/Zepp 账号 │ ─────────────────────────────> │ auth_key（16 字节）  │
└──────────────┘   get_auth_key.py / fetch_by_token.py                   │
                                                                        v
┌──────────────┐  2. BLE 扫描（屏幕点亮才广播）  ┌─────────────────────────┐
│   小米手环 6  │ <────────────────────────────> │ bleak / BlueZ           │
│              │  3. Chunked ECDH 认证           │  miband_client.py       │
│              │ <────────────────────────────> │  ├─ 0x0016/0x0017 分块  │
│              │  4. 三类 GATT 通知              │  ├─ ECDH(B-163)+AES     │
│              │ ─────────────────────────────> │  └─ 心率/步数/IMU 解析   │
└──────────────┘                                 └───────────┬─────────────┘
                                                             │ 带时间戳
                                                 ┌───────────v─────────────┐
                                                 │ storage.py              │
                                                 │ ├─ *.jsonl（追加写）     │
                                                 │ └─ mi_band.db（SQLite）  │
                                                 └──────┬───────────┬──────┘
                                                        │           │
                                        ┌───────────────v──┐   ┌────v─────────────────┐
                                        │ export_csv.py    │   │ dashboard.py         │
                                        │ → *.csv          │   │ HTTP API + SSE 推送  │
                                        └──────────────────┘   │ dashboard.html 图表  │
                                                               └──────────────────────┘
```

历史数据（`--sync-history`）通过手环私有 `0x0004`/`0x0005` 特征拉取：
活动数据按分钟存储，血氧按次存储，随后同样进入上面的存储/看板/导出链路。

认证流程（简化）：

1. 客户端发送自己的 B-163 公钥（内层消息 `04 02 00 02 + pubkey`，分块发送）。
2. 手环返回 `16 字节随机盐 + 48 字节公钥`（分块通知，带序号；客户端校验连续性并重组）。
3. 双方计算 48 字节 ECDH 共享密钥，取 `shared[8:24] XOR auth_key` 得到会话 AES key。
4. 客户端回发 `0x05 + AES-CBC(auth_key, 盐) + AES-CBC(session_key, 盐)`，手环确认后认证成功。

> 协议细节参考社区对华米 Chunked Transfer 的逆向结果（Gadgetbridge 等），
> 本项目在重组环节做了额外的健壮性加固。

---

## 3. 目录结构

```
miband_health_monitor/
├── miband_client.py        # 主程序：扫描/连接/认证/数据流/历史同步/重连/CLI（可执行）
├── storage.py              # 时间戳 + JSONL/SQLite 持久化（后台写线程）
├── export_csv.py           # SQLite → CSV 导出（也可单独运行）
├── dashboard.py            # 标准库 HTTP + SSE 实时看板/API
├── dashboard.html          # 单页看板（Canvas 图表，无外部依赖）
├── band_history.py         # 手环侧历史数据拉取（0x0004/0x0005，实验性）
├── history_report.py       # 本地 SQLite 历史汇总报告（CLI）
├── sim_band.py             # 内存版模拟手环 + 假 bleak 后端（--demo 与测试共用）
├── ecdh_wrapper.py         # ctypes 封装 libecdh.so（B-163 ECDH）
├── ecdh.c / ecdh.h         # tiny-ECDH C 实现（缺失 .so 时自动 gcc 编译）
├── libecdh.so              # 预编译产物
├── get_auth_key.py         # 交互式获取 auth_key（小米账号 / Zepp 账号）
├── fetch_by_token.py       # 用浏览器 app_token 获取 auth_key
├── huami_token/            # 华米/小米云 API 客户端（第三方，MIT）
├── scan_ble.py             # 排障：8 秒扫描列表
├── scan_live.py            # 排障：连续事件扫描（找 MAC）
├── test_miband.py          # 原有单元测试（加密/解析器）
├── tests/                  # 存储、chunked 协议、模拟手环、看板、历史数据测试
├── tools/                  # 诊断工具：capture_history_payload.py（真机 payload 抓取）
├── docs/                   # 文档：history_hardware_validation.md（⑦ 真机验证清单）
├── requirements.txt
├── pytest.ini
├── .env.example
└── README.md
```

---

## 4. 环境要求

| 项目 | 要求 |
|---|---|
| 操作系统 | **Linux（BlueZ）** 为官方支持路径；Windows/macOS 未验证 |
| Python | ≥ 3.10（使用 `X | Y` 类型注解；开发环境 3.13） |
| 蓝牙 | 主机自带或 USB 蓝牙适配器；WSL2 需 `usbipd` 直通并安装 BlueZ |
| 其他 | 首次自动编译 `ecdh.c` 需要 `gcc`（也可直接使用仓库内 `libecdh.so`） |

> **WSL2 注意**：默认 WSL2 没有蓝牙栈（无 `/sys/class/bluetooth`、无 `bluetooth.service`），
> 必须把 USB 蓝牙适配器直通进 WSL，或直接在原生 Linux 上运行。

---

## 5. 安装

```bash
cd miband_health_monitor

# 推荐：项目内虚拟环境（某些系统 site-packages 只读时尤其需要）
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

# 或者直接装进当前环境
python -m pip install -r requirements.txt
```

依赖说明：`bleak`、`cryptography`、`python-dotenv` 为运行必需；
`loguru`、`pycryptodome`、`requests` 供 `get_auth_key.py` / `fetch_by_token.py` 使用；
`pytest`、`pytest-asyncio` 供测试使用。

---

## 6. 配置（.env）

复制模板并填写：

```bash
cp .env.example .env
```

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `DEVICE_MAC` | 是 | — | 手环蓝牙 MAC，如 `C0:FF:EE:C0:FF:EE` |
| `AUTH_KEY` | 是 | — | 32 位十六进制认证密钥（16 字节） |
| `OUTPUT_DIR` | 否 | `data` | JSONL 与 `mi_band.db` 输出目录 |
| `STORAGE_BACKEND` | 否 | `both` | `jsonl` / `sqlite` / `both` / `none` |
| `SCAN_TIMEOUT` | 否 | `90` | 等待手环广播的秒数 |
| `LOG_LEVEL` | 否 | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `DEMO_MODE` | 否 | `0` | 设为 `1` 等价于 `--demo` |
| `WEB_PORT` | 否 | — | 实时看板端口；留空/注释关闭，`0` 表示随机空闲端口 |
| `WEB_HOST` | 否 | `127.0.0.1` | 看板绑定地址；默认仅本机可访问 |
| `HISTORY_DAYS` | 否 | — | 设为 N 等价于 `--sync-history N`（连接后同步手环历史） |

`.env` 已被 `.gitignore` 忽略，**不要提交真实 auth_key**。

### 获取 auth_key（三选一）

1. **小米账号**（推荐，交互式，密码不会落盘）：

   ```bash
   python get_auth_key.py
   # 选择 1：小米账号；输入账号/密码，脚本会列出绑定设备并写回 .env
   ```

2. **Zepp/Amazfit 账号**：

   ```bash
   python get_auth_key.py     # 选择 2
   ```

3. **浏览器 app_token**（已有网页登录态时）：

   ```bash
   python fetch_by_token.py   # 输入 user_id 和 app_token
   ```

4. **第三方方式**（如已有 `huami-token` 工具输出）：直接把 key 填进 `.env`。

> 认证密钥属于敏感凭据，等价于手环的蓝牙配对凭证，请勿外泄。

---

## 7. 运行

```bash
# 无硬件先跑通全链路（模拟手环，2 秒，含 IMU 与 CSV 导出）
.venv/bin/python miband_client.py --demo 2 --imu --output data/demo --export-csv

# 真机：采集 60 秒心率 + 步数
.venv/bin/python miband_client.py 60

# 真机：采集 60 秒并附加 25Hz IMU
.venv/bin/python miband_client.py 60 --imu

# 一直采集到 Ctrl+C，同时写 SQLite 并在结束后导出 CSV
.venv/bin/python miband_client.py --export-csv

# 只看日志不落盘
.venv/bin/python miband_client.py --storage none

# 打开实时看板（浏览器访问 http://127.0.0.1:8080，无需硬件可加 --demo）
.venv/bin/python miband_client.py --web-port 8080

# 连接后同步最近 7 天手环历史（活动 + 血氧，实验性）
.venv/bin/python miband_client.py 60 --sync-history 7

# 查看本地历史数据汇总（文本或 --json）
.venv/bin/python history_report.py --hours 24
.venv/bin/python history_report.py --hours 168 --json

# 排障：先确认能扫到手环
.venv/bin/python scan_live.py
```

### CLI 参数

```
python miband_client.py [duration] [选项]

duration              采集秒数；省略则一直运行到 Ctrl+C
--imu                 同时采集原始 IMU 加速度（约 25Hz）
--sync-history [DAYS] 连接后拉取最近 DAYS 天手环历史（默认 7；实验性）
--demo                使用内存模拟手环（无需蓝牙硬件）
--web-port PORT       启动实时看板/HTTP API（0 = 随机空闲端口）
--web-host HOST       看板绑定地址（默认 127.0.0.1）
--output DIR          输出目录（默认 $OUTPUT_DIR 或 data）
--storage MODE        jsonl | sqlite | both | none（默认 both）
--export-csv          会话结束后把 SQLite 导出为 CSV
--scan-timeout SEC    扫描等待秒数（默认 90）
--log-level LEVEL     DEBUG | INFO | WARNING | ERROR
--no-reconnect        出错/断连后不自动重连
```

真机使用要点：

- **手机蓝牙关闭 / 开飞行模式**，否则手环被手机占连，无法广播；
- 扫描期间**点亮并滑动/触摸手环屏幕**，手环屏幕熄灭会停止广播；
- 首次连接时手环可能震动或提示确认，按提示操作；
- 心率传感器在自动重连后会自动重新开启。

---

## 8. 输出数据格式

目录结构（默认 `data/`）：

```
data/
├── heart_rate.jsonl
├── steps.jsonl
├── imu.jsonl
├── spo2.jsonl              # --sync-history 产生
├── activity_history.jsonl  # --sync-history 产生
├── mi_band.db
├── mi_band.db-wal / mi_band.db-shm
└── csv/                    # --export-csv 生成
    ├── heart_rate.csv
    ├── steps.csv
    ├── imu.csv
    ├── spo2.csv
    └── activity_history.csv
```

### JSONL

每行一个 JSON 对象，均含：

| 字段 | 类型 | 说明 |
|---|---|---|
| `ts` | float | Unix 时间戳（秒，电脑接收时间） |
| `iso_time` | string | UTC ISO-8601，毫秒精度，如 `2026-11-07T08:10:18.472+00:00` |

各流特有字段：

| 文件 | 字段 |
|---|---|
| `heart_rate.jsonl` | `bpm` |
| `steps.jsonl` | `steps`、`distance_m`、`calories` |
| `imu.jsonl` | `sensor_type`、`sequence`、`sample_index`、`x_raw/y_raw/z_raw`、`ax_g/ay_g/az_g`、`ax_ms2/ay_ms2/az_ms2`（每个加速度样本一行） |
| `spo2.jsonl` | `spo2`（%）、`auto_measure`（0/1）；`ts` 为手环采样时间 |
| `activity_history.jsonl` | `kind`、`intensity`、`steps`、`heart_rate`、`unknown1`、`sleep`、`deep_sleep`、`rem_sleep`（每分钟一行） |

> 实时流（心率/步数/IMU）的 `ts` 是**电脑接收时间**；`--sync-history` 拉取的
> 血氧/活动数据 `ts` 是**手环记录的采样时间**（分钟精度/秒精度）。

示例：

```json
{"ts": 1794039018.472809, "iso_time": "2026-11-07T08:10:18.472+00:00", "bpm": 72}
{"ts": 1794039018.462322, "iso_time": "2026-11-07T08:10:18.462+00:00", "sensor_type": 1, "sequence": 0, "sample_index": 0, "x_raw": 0, "y_raw": 0, "z_raw": 256, "ax_g": 0.0, "ay_g": 0.0, "az_g": 1.0, "ax_ms2": 0.0, "ay_ms2": 0.0, "az_ms2": 9.8066}
```

### SQLite

数据库 `data/mi_band.db`（WAL 模式），五张表均以 `id` 主键、`ts`/`iso_time` 索引：

```sql
SELECT datetime(ts, 'unixepoch'), bpm FROM heart_rate ORDER BY ts;
SELECT * FROM steps ORDER BY ts;
SELECT sensor_type, sequence, sample_index, az_g FROM imu WHERE ts > :since;
SELECT datetime(ts, 'unixepoch'), spo2, auto_measure FROM spo2 ORDER BY ts;
SELECT datetime(ts, 'unixepoch'), steps, heart_rate FROM activity_history ORDER BY ts;
```

### CSV

```bash
python export_csv.py --db data/mi_band.db --output data/csv
```

（`--export-csv` 会在会话结束时自动做同样的事；会导出 `heart_rate`、`steps`、
`imu`、`spo2`、`activity_history` 五张表；CSV 表头为列名，UTF-8 编码。）

---

## 9. 实时看板（HTTP API + SSE）

```bash
.venv/bin/python miband_client.py --web-port 8080             # 真机
.venv/bin/python miband_client.py --demo 30 --web-port 8080   # 无硬件演示
```

浏览器打开 `http://127.0.0.1:8080`（用 `--web-host 0.0.0.0` 可局域网访问，注意安全）。
页面用 Canvas 绘制心率 / 血氧 / 步数 / IMU 合加速度趋势，SSE 实时推送，无需刷新。

| 端点 | 说明 |
|---|---|
| `GET /` | 单页看板（`dashboard.html`） |
| `GET /api/snapshot` | 最新值 + 最近序列 + 计数（JSON） |
| `GET /api/history?kind=heart_rate&limit=600` | 单个指标的内存序列 |
| `GET /events` | SSE 事件流（先发 snapshot，再持续推送 sample） |
| `GET /healthz` | 存活探针 |

- 纯 Python 标准库实现（asyncio HTTP + SSE），不引入 Web 框架依赖；
- 看板只维护内存环形缓冲，慢客户端丢事件而不拖慢 BLE；写入走 `storage.py` 后台线程；
- 默认只监听 `127.0.0.1`，不要把看板直接暴露到公网。

---

## 10. 实验性：血氧（SpO₂）与手环历史数据

小米手环 6 的**血氧不是实时通知流**，而是以历史记录形式存放在手环中，需要通过
私有 "fetch data" 协议拉取（与心率/步数/IMU 的实时流不同）。

```bash
# 连接后先拉取最近 7 天历史，再进入实时采集
.venv/bin/python miband_client.py 60 --sync-history 7

# 只做历史同步也可以（Ctrl+C 结束）
.venv/bin/python miband_client.py --sync-history 3
```

协议要点（依据 Gadgetbridge 的公开逆向结果重新实现，未复制其代码）：

- 控制/元数据特征 `00000004-...`，数据分片特征 `00000005-...`；
- 写 `[0x01, 数据类型] + 本地时间(6B) + [原因, 时区]` 启动；数据类型：活动 `0x01`、
  血氧 `0x25`。手环返回待传字节数与起始时间后，再写 `[0x02]` 请求数据；
- 数据分片带递增计数（每片首字节），完成后手环回 `10 02 01`，客户端回 `[0x03]` 确认；
- 血氧记录：版本 `2` + 每条 65 字节（int32 LE Unix 时间、1 字节 SpO₂、60 字节保留），
  负值表示自动测量（真实值 = 原始值 + 128）；
- 活动记录（Mi Band 6）：每分钟 8 字节：`kind, intensity, steps, heart_rate,
  unknown1, sleep, deep_sleep, rem_sleep`。

> ⚠️ **实验性**：以上流程仅在模拟手环上验证，尚未在真实手环上逐项校验。
> 真机若出现 `band rejected fetch type` / 超时 / 数据校验失败，请用
> `--log-level DEBUG` 抓日志；历史同步失败不会中断实时采集。
> 协议参考：[Gadgetbridge](https://codeberg.org/Freeyourgadget/Gadgetbridge)
> 的 `HuamiSupport` / `operations/fetch`（AGPL-3.0，仅作协议事实参考）。
>
> 📋 **真机验证**：请按 [docs/history_hardware_validation.md](docs/history_hardware_validation.md)
> 的清单执行（含逐阶段期望日志、失败速查、未验证假设与反馈模板）。
> 需要原始 payload 硬证据时运行：
> `python tools/capture_history_payload.py --days 7 --output validation/payloads`。

拉取到的数据会进入 `spo2.jsonl` / `activity_history.jsonl`、SQLite 的 `spo2` /
`activity_history` 表、CSV 导出，并实时推送到看板的血氧卡片/曲线。

本地历史汇总：

```bash
.venv/bin/python history_report.py --hours 24       # 最近 24 小时文本报告
.venv/bin/python history_report.py --hours 168 --json
```

---

## 11. 运行测试

无需任何蓝牙硬件：

```bash
.venv/bin/python -m pytest -q
```

覆盖内容：

- `test_miband.py`：AES 测试向量、心率/步数/IMU 解析、密钥格式；
- `tests/test_storage.py`：时间戳、JSONL/SQLite 写入（含血氧/活动历史）、CSV 导出、关闭后写入丢弃；
- `tests/test_chunked_protocol.py`：分块帧格式、MTU 协商、认证重组（序号缺口/重复/越界/非法长度）；
- `tests/test_simulated_band.py`：模拟手环端到端认证、三类实时数据流、断连续连、落盘与导出；
- `tests/test_dashboard.py`：遥测 Hub、HTTP API、SSE 实时推送、看板路由与回退页；
- `tests/test_history.py`：时间编码、血氧/活动解析、模拟 fetch 协议（含丢包）、历史落盘、历史报告、CLI。

预期结果：`62 passed`。

---

## 12. 常见问题（FAQ）

| 现象 | 原因/处理 |
|---|---|
| `No Bluetooth adapters found` / `org.bluez was not provided` | 本机无蓝牙栈（如 WSL2 未直通、`bluetooth` 服务未启动）。安装/启动 BlueZ，或直通 USB 蓝牙适配器。 |
| 90 秒内扫描不到手环 | 手机关蓝牙；点亮并触摸手环屏幕；确认 `.env` 的 MAC 正确；用 `scan_live.py` 验证。 |
| 认证超时 | 分块通知丢失或固件行为差异；查看 `--log-level DEBUG`。客户端会在下次会话自动重试。 |
| 认证成功但心率不动 | 手环需佩戴贴合；`0x2A39` 控制序列可能被固件拒绝；重连一次或重启手环。 |
| IMU 无数据 | 部分固件需先进入特定运动模式；`--imu` 为实验性功能。 |
| `--sync-history` 报 `band rejected fetch type` | 该固件/型号不支持对应历史类型，或 fetch 特征与预期不同；历史同步失败不影响实时采集。 |
| `--sync-history` 超时/校验失败 | 分片丢失或固件行为差异；重连会重试。真机请附 DEBUG 日志反馈。 |
| 看板打不开 / 端口占用 | `--web-port 0` 使用随机端口；看板默认仅本机可访问，远程查看需 `--web-host 0.0.0.0` 并注意防火墙。 |
| 看板有数据但曲线不动 | 浏览器 SSE 断线会自动重连；确认页面未被休眠，或刷新页面重新拉 `/api/snapshot`。 |
| 连接一段时间后断开 | 手环省电/离体/手机抢占。默认启用指数退避自动重连。 |
| 数据重复（步数） | 步数流会先读一次当前值再订阅通知，首条可能重复，属预期行为。 |
| 时间戳含义 | 实时流为**电脑接收时间**；历史同步数据为**手环采样时间**。 |

---

## 13. 已知限制与后续方向

- 血氧/活动历史读取为**实验性**，协议依据公开逆向结果实现，需真机验证；
- 仅覆盖心率 / 步数 / IMU / 血氧(历史) / 活动(历史)；压力、睡眠分期、训练详情、电量等尚未实现；
- IMU 为实验性私有协议，采样率与量程以固件为准；
- 看板为单机内存视图，刷新页面只保留最近若干点；长期历史请查 SQLite/CSV；
- Windows/macOS 的 BLE 后端未验证（代码使用 bleak 通用 API，理论上可移植）；
- 断连检测依赖连接回调与心率保活，极端情况下最长约 12 秒才发现链路丢失。

---

## 14. 安全与第三方

- `AUTH_KEY` 是手环的蓝牙配对凭据，请视同密码保管；`.env` 已被忽略。
- 看板/HTTP API 默认只绑定 `127.0.0.1` 且无鉴权，请勿直接暴露到公网。
- `huami_token/` 来自第三方 `huami-token` 项目（MIT License, © 2025 Kirill Snezhko），
  用于获取 auth_key；`ecdh.c/ecdh.h` 为 tiny-ECDH 的 C 实现。
- Chunked ECDH 与历史 fetch 协议参考 Gadgetbridge 项目（AGPL-3.0）的公开逆向成果，
  本项目为独立 Python 实现，未复制其源代码。
- 本项目仅用于个人健康数据的自主采集与分析，请遵守手环使用条款与当地隐私法规。

### 隐私与数据保护

本项目会产生并落盘大量个人数据，仓库只应保存代码与脱敏文档：

| 内容 | 位置 | 敏感信息 | git 状态 |
|---|---|---|---|
| 设备 MAC / auth_key | `.env` | 是（配对凭据） | 已忽略，权限 0600 |
| 实时/历史健康数据 | `data/*.jsonl`、`data/mi_band.db`、`data/csv/` | 是（个人健康数据） | 已忽略 |
| 真机验证材料 | `validation/`（run.log、payloads、btmon） | 是（MAC / 数据 / 空口包） | 已忽略 |
| 调试日志与抓包 | `*.log`、`*.btsnoop`、`*.pcap` | 是 | 已忽略 |
| 代码与文档 | 仓库根、`docs/` | 否（示例均为占位符） | 跟踪 |

- 代码与文档中的 MAC 一律使用占位符（如 `C0:FF:EE:C0:FF:EE`），不要写入真实设备地址。
- 对外反馈问题前，按 [`docs/history_hardware_validation.md`](docs/history_hardware_validation.md) §9
  对日志做 MAC 脱敏，并确认其中不含 `auth_key`。
- 提交前可自查：`git status --ignored`、`git ls-files -ci --exclude-standard`。
