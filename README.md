# 小米手环 6 健康数据实时监测（Mi Band 6 Health Monitor）

通过 **BLE 直连** 小米手环 6，绕过手机 App，把心率、实时步数和原始 IMU 加速度数据
实时传输到电脑，并带时间戳落地为 JSONL / SQLite，可一键导出 CSV，便于后续分析与二次开发。

---

## 1. 功能概览

| 能力 | 说明 |
|---|---|
| 手环直连 | 基于 [`bleak`](https://github.com/hbldh/bleak) 扫描、连接、断连自动重连（Linux/BlueZ、Windows/WinRT、macOS/CoreBluetooth） |
| 跨平台 | Linux / Windows 10+ / macOS；ECDH 原生库不可用时自动回退纯 Python（无需编译器） |
| 安全认证 | 华米 Chunked Transfer + NIST B-163 ECDH + AES-CBC 挑战响应；分片重组带序号校验/越界保护/重复包处理 |
| 心率 | 标准 BLE SIG Heart Rate 服务（`0x2A37` / `0x2A39`），连续测量 + 12s 保活 |
| 实时步数 | 私有特征 `0x0007`：步数 / 距离 / 卡路里 |
| 原始 IMU | 私有特征 `0x0001`/`0x0002`，约 25 Hz 三轴加速度（需 `--imu`）；仅 `0x00` 类型帧是加速度（int16，4096 LSB/g），同特征上的 `0x01`/`0x07` 帧会被忽略 |
| 实时看板 | `--web-port` 启动内嵌 HTTP 服务：SSE 实时推送 + 单页图表（心率/血氧/步数/IMU），**无第三方依赖**；刷新页面自动从 SQLite 回填历史 |
| 血氧 SpO₂ | 从手环侧历史记录拉取（需 `--sync-history`，已真机验证） |
| 历史活动数据 | 每分钟活动强度/步数/心率/睡眠标志（需 `--sync-history`，已真机验证） |
| 睡眠血氧 0x26 | `--sync-sleep-spo2` 拉取整夜血氧（30B 布局**已真机验证**，一夜 430 条），写入**独立** `spo2_sleep` 表 + JSONL + CSV；离线看板画整夜曲线，与点测 `0x25` 完全隔离 |
| 本地历史报告 | `history_report.py` 汇总 SQLite 中的心率/血氧/活动数据，支持文本与 JSON |
| 睡眠推导 | `sleep_analysis.py`：用 `kind` 状态机判定睡眠窗口，再按心率水平与心率变异性排序区分深睡 / REM（**推导值**，手环不直接上报分期） |
| 离线历史看板 | `history_dashboard.py` 生成**单文件 HTML**：KPI、睡眠结构图、心率、活动节律、步数、整夜血氧曲线、点测血氧表；无外部资源，可离线打开或原样分享 |
| 时间戳 | 每条记录带 Unix 秒 + UTC ISO-8601 毫秒时间；历史数据为手环采样时间 |
| 持久化 | JSONL（逐行追加，方便流式处理）与 SQLite（WAL，方便查询/分析）可任选或同时 |
| CSV 导出 | 从 SQLite 导出 5 张 CSV 表 |
| 自动重连 | 会话失败/链路丢失后指数退避重连；心率保活失败也会触发重连 |
| 无硬件演示 | `--demo` 使用内置模拟手环跑通完整链路（认证、数据流、历史同步、落盘、导出） |
| 测试 | 241 个单元/集成测试，含模拟 BLE 后端，**不需要真实手环**；CI 覆盖 Linux/Windows/macOS |

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
├── band_history.py         # 手环侧历史数据拉取（0x0004/0x0005，真机已验证）
├── history_report.py       # 本地 SQLite 历史汇总报告（CLI）
├── sleep_analysis.py       # 睡眠窗口/分期推导（kind 状态机 + 心率/HRV 排序，CLI）
├── history_dashboard.py    # 离线历史看板：单文件 HTML（无外部资源，CLI）
├── sim_band.py             # 内存版模拟手环 + 假 bleak 后端（--demo 与测试共用）
├── ecdh_wrapper.py         # ECDH 双后端：原生 tiny-ECDH（ctypes）+ 纯 Python 回退
├── ecdh.c / ecdh.h         # tiny-ECDH C 实现（Linux 优先使用，缺库时自动编译）
├── libecdh.so              # Linux x86-64 预编译产物（其他平台自动回退）
├── vendor/tiny_ecdh/       # 纯 Python B-163 ECDH（第三方，MIT，跨平台回退实现）
├── get_auth_key.py         # 交互式获取 auth_key（小米账号 / Zepp 账号）
├── fetch_by_token.py       # 用浏览器 app_token 获取 auth_key
├── huami_token/            # 华米/小米云 API 客户端（第三方，MIT）
├── scan_ble.py             # 排障：8 秒扫描列表（输出 MAC / macOS UUID）
├── scan_live.py            # 排障：连续事件扫描（找 MAC）
├── test_miband.py          # 原有单元测试（加密/解析器）
├── tests/                  # 存储、chunked 协议、模拟手环、看板、历史、睡眠、ECDH 双后端测试
├── tools/                  # 诊断/导入工具：payload 抓取与导入、fetch 类型探测、保留窗口探针、Zepp 数字校准
├── docs/                   # 文档：跨平台指南、真机验证清单、睡眠字段取证
├── .github/workflows/ci.yml # GitHub Actions：Linux/Windows/macOS 测试矩阵
├── requirements.txt
├── pytest.ini
├── .env.example
└── README.md
```

---

## 4. 环境要求

| 项目 | 要求 |
|---|---|
| 操作系统 | **Linux（BlueZ）**、**Windows 10 19041+（WinRT）**、**macOS 12+（CoreBluetooth）** |
| Python | ≥ 3.10（使用 `X | Y` 类型注解；开发环境 3.13） |
| 蓝牙 | 主机自带或 USB 蓝牙适配器；WSL2 需 `usbipd` 直通并安装 BlueZ（见下） |
| 编译器 | **可选**。Linux 有 `gcc`/`clang` 时自动编译加速；没有也可用纯 Python 回退 |
| 依赖 | `bleak` 会按平台自动安装 WinRT / pyobjc 后端，无需手工处理 |

> **WSL2 注意**：默认 WSL2 没有蓝牙栈（无 `/sys/class/bluetooth`、无 `bluetooth.service`），
> 必须把 USB 蓝牙适配器直通进 WSL，或直接在原生 Linux 上运行。
>
> Windows、Linux 的分步安装/排障/真机验证清单见 [`docs/cross_platform.md`](docs/cross_platform.md)。

三平台的关键差异（代码已处理，排障时对照）：

| 平台 | 设备标识 | BLE 后端 | 备注 |
|---|---|---|---|
| Linux | 蓝牙 MAC（`DEVICE_ID`） | BlueZ（`dbus-fast`） | 需要 `bluetooth` 服务运行 |
| Windows 10/11 | 蓝牙 MAC（`DEVICE_ID`） | WinRT | 首次使用需在“设置 → 蓝牙”里确认适配器可用 |
| macOS | **CoreBluetooth UUID**（`DEVICE_ID`）或 `DEVICE_NAME` | CoreBluetooth | 系统不暴露 MAC；终端首次需授予蓝牙权限 |

---

## 5. 安装

Linux / macOS：

```bash
cd miband_health_monitor

# 推荐：项目内虚拟环境（某些系统 site-packages 只读时尤其需要）
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

# 或者直接装进当前环境
python3 -m pip install -r requirements.txt
```

Windows（PowerShell / cmd）：

```powershell
cd miband_health_monitor

# 推荐用 python.org 的 Python 3.10+；Windows 下虚拟环境入口是 Scripts\python.exe
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

# 激活虚拟环境（可选）
.\.venv\Scripts\Activate.ps1        # PowerShell
.venv\Scripts\activate.bat          # cmd
```

依赖说明：`bleak`、`cryptography`、`python-dotenv` 为运行必需；
`loguru`、`pycryptodome`、`requests` 供 `get_auth_key.py` / `fetch_by_token.py` 使用；
`pytest`、`pytest-asyncio` 供测试使用。`bleak` 会按平台自动拉取 WinRT（Windows）
或 pyobjc（macOS）后端依赖。

> 为简洁起见，下文示例统一写 `.venv/bin/python`；**Windows 请替换为
> `.venv\Scripts\python.exe`**（路径分隔符同理）。

> 不需要编译器：纯 Python 回退在没有 `libecdh` 的平台上自动启用；
> 若想在 Windows 上使用更快的原生实现，可自行编译 `ecdh.dll` 并放到项目根目录
> （或用 `MIBAND_ECDH_LIB` 指定路径），详见 [`docs/cross_platform.md`](docs/cross_platform.md)。

---

## 6. 配置（.env）

复制模板并填写：

```bash
cp .env.example .env
```

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `DEVICE_ID` | 是 | — | 手环标识：Linux/Windows 用 MAC（如 `C0:FF:EE:C0:FF:EE`）；macOS 用 CoreBluetooth UUID |
| `DEVICE_MAC` | 否 | — | 旧版别名，仍兼容；同时配置时 `DEVICE_ID` 优先（占位符会被忽略） |
| `DEVICE_NAME` | 否 | — | 广播名子串匹配（大小写不敏感），如 `Mi Smart Band`；macOS/多设备时很实用 |
| `AUTH_KEY` | 是 | — | 32 位十六进制认证密钥（16 字节） |
| `BLE_MTU` | 否 | 自动 | 手动覆盖 ATT MTU（23–517）；一般留空，自动探测 |
| `OUTPUT_DIR` | 否 | `data` | JSONL 与 `mi_band.db` 输出目录 |
| `STORAGE_BACKEND` | 否 | `both` | `jsonl` / `sqlite` / `both` / `none` |
| `SCAN_TIMEOUT` | 否 | `90` | 等待手环广播的秒数 |
| `LOG_LEVEL` | 否 | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `DEMO_MODE` | 否 | `0` | 设为 `1` 等价于 `--demo` |
| `WEB_PORT` | 否 | — | 实时看板端口；留空/注释关闭，`0` 表示随机空闲端口 |
| `WEB_HOST` | 否 | `127.0.0.1` | 看板绑定地址；默认仅本机可访问 |
| `HISTORY_DAYS` | 否 | — | 设为 N 等价于 `--sync-history N`（连接后同步手环历史） |
| `MIBAND_ECDH_BACKEND` | 否 | `auto` | 诊断用：`auto` / `native` / `python`（强制 ECDH 实现） |

`.env` 已被 `.gitignore` 忽略，**不要提交真实 auth_key**。

> Windows 上复制模板用 `copy .env.example .env`，改完可用
> `.venv\Scripts\python.exe scan_ble.py` 确认能扫到手环并核对地址。

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

Linux / macOS（虚拟环境内）：

```bash
# 环境自检：适配器、配对状态、ECDH 后端、.env 权限
.venv/bin/python miband_client.py --doctor

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

# 额外抓取 0x26 整夜血氧（写入独立 spo2_sleep 表 + JSONL）
.venv/bin/python miband_client.py 60 --sync-history 7 --sync-sleep-spo2

# 把取证工具保存的 .bin 原始 payload 导入正式数据链路（可重复运行，自动按时间戳去重）
.venv/bin/python tools/import_spo2_sleep.py --output data validation/payloads-26/history_26_*.bin

# 查看本地历史数据汇总（文本或 --json）
.venv/bin/python history_report.py --hours 24
.venv/bin/python history_report.py --hours 168 --json

# 排障：先确认能扫到手环（macOS 会打印 UUID）
.venv/bin/python scan_ble.py
.venv/bin/python scan_live.py
```

Windows（PowerShell，把 `python` 换成虚拟环境路径即可）：

```powershell
.venv\Scripts\python.exe miband_client.py --demo 2 --imu --output data\demo --export-csv
.venv\Scripts\python.exe miband_client.py 60
.venv\Scripts\python.exe miband_client.py --web-port 8080
.venv\Scripts\python.exe miband_client.py 60 --sync-history 7
.venv\Scripts\python.exe history_report.py --hours 24
.venv\Scripts\python.exe scan_ble.py
```

### CLI 参数

```
python miband_client.py [duration] [选项]

duration              采集秒数；省略则一直运行到 Ctrl+C
--imu                 同时采集原始 IMU 加速度（约 25Hz；会先于心率流启动，
                      真机上顺序反了会抢掉标准心率特征的通知）
--sync-history [DAYS] 连接后拉取最近 DAYS 天手环历史（默认 7；实验性）
--sync-sleep-spo2     额外拉取 0x26 整夜血氧（需配合 --sync-history）；30B 布局已真机验证，
                      写入独立 spo2_sleep 表 + JSONL，尾部字段语义未定
--demo                使用内存模拟手环（无需蓝牙硬件）
--device-id ID        覆盖 DEVICE_ID（MAC；macOS 为 UUID）
--device-name NAME    广播名子串匹配（额外发现条件，macOS 友好）
--mtu BYTES           手动覆盖 ATT MTU（23–517；默认自动探测）
--web-port PORT       启动实时看板/HTTP API（0 = 随机空闲端口）
--web-host HOST       看板绑定地址（默认 127.0.0.1）
--output DIR          输出目录（默认 $OUTPUT_DIR 或 data）
--storage MODE        jsonl | sqlite | both | none（默认 both）
--export-csv          会话结束后把 SQLite 导出为 CSV
--scan-timeout SEC    扫描等待秒数（默认 90）
--log-level LEVEL     DEBUG | INFO | WARNING | ERROR
--no-reconnect        出错/断连后不自动重连
```

启动日志的前两行会打印当前 ECDH 后端与设备发现目标，反馈问题时请一并附上，例如：

```
[INFO] MiBand6Client: ECDH backend: python
[INFO] MiBand6Client: Discovery target: C0:FF:EE:C0:FF:EE
```

真机使用要点：

- **手机蓝牙关闭 / 开飞行模式**，否则手环被手机占连，无法广播；
- 扫描期间**点亮并滑动/触摸手环屏幕**，手环屏幕熄灭会停止广播；
- 首次连接时手环可能震动或提示确认，按提示操作；
- 心率传感器在自动重连后会自动重新开启；
- **Windows**：确认“设置 → 蓝牙和其他设备”中蓝牙已开启且适配器支持 BLE；如果设备之前
  被系统连接过，先在“蓝牙和其他设备”里“删除设备”再重试；
- **macOS**：`DEVICE_ID` 要填 CoreBluetooth UUID（`scan_ble.py` 输出），或改用
  `DEVICE_NAME` 匹配；首次运行终端会弹窗请求蓝牙权限，需允许。

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
├── spo2_sleep.jsonl        # --sync-sleep-spo2 产生（同时写 spo2_sleep 表）
├── mi_band.db              # 含独立 spo2_sleep 表
├── mi_band.db-wal / mi_band.db-shm
└── csv/                    # --export-csv 生成
    ├── heart_rate.csv
    ├── steps.csv
    ├── imu.csv
    ├── spo2.csv
    ├── activity_history.csv
    └── spo2_sleep.csv
```

### JSONL

每行一个 JSON 对象，均含：

| 字段 | 类型 | 说明 |
|---|---|---|
| `ts` | float | Unix 时间戳（秒，电脑接收时间） |
| `iso_time` | string | UTC ISO-8601，毫秒精度，如 `2020-01-07T08:10:18.472+00:00` |

各流特有字段：

| 文件 | 字段 |
|---|---|
| `heart_rate.jsonl` | `bpm` |
| `steps.jsonl` | `steps`、`distance_m`、`calories` |
| `imu.jsonl` | `sensor_type`、`sequence`、`sample_index`、`x_raw/y_raw/z_raw`、`ax_g/ay_g/az_g`、`ax_ms2/ay_ms2/az_ms2`（每个加速度样本一行） |
| `spo2.jsonl` | `spo2`（%）、`auto_measure`（0/1）；`ts` 为手环采样时间 |
| `activity_history.jsonl` | `kind`、`intensity`、`steps`、`heart_rate`、`unknown1`、`sleep`、`deep_sleep`、`rem_sleep`（每分钟一行） |
| `spo2_sleep.jsonl` | `spo2`（无符号）、`duration`、`spo2_high/low`、`signal_quality`、`extend`（原始字节组）、`source`；SQLite 中四组字节以 JSON 文本存在 `spo2_sleep` 表，CSV 同名导出 |

> 实时流（心率/步数/IMU）的 `ts` 是**电脑接收时间**；`--sync-history` 拉取的
> 血氧/活动数据 `ts` 是**手环记录的采样时间**（分钟精度/秒精度）。

示例：

```json
{"ts": 1578384618.472809, "iso_time": "2020-01-07T08:10:18.472+00:00", "bpm": 72}
{"ts": 1578384618.462322, "iso_time": "2020-01-07T08:10:18.462+00:00", "sensor_type": 1, "sequence": 0, "sample_index": 0, "x_raw": 0, "y_raw": 0, "z_raw": 256, "ax_g": 0.0, "ay_g": 0.0, "az_g": 1.0, "ax_ms2": 0.0, "ay_ms2": 0.0, "az_ms2": 9.8066}
```

### SQLite

数据库 `data/mi_band.db`（WAL 模式），六张表均以 `id` 主键、`ts`/`iso_time` 索引：

```sql
SELECT datetime(ts, 'unixepoch'), bpm FROM heart_rate ORDER BY ts;
SELECT * FROM steps ORDER BY ts;
SELECT sensor_type, sequence, sample_index, az_g FROM imu WHERE ts > :since;
SELECT datetime(ts, 'unixepoch'), spo2, auto_measure FROM spo2 ORDER BY ts;
SELECT datetime(ts, 'unixepoch'), steps, heart_rate FROM activity_history ORDER BY ts;
SELECT datetime(ts, 'unixepoch'), spo2, duration FROM spo2_sleep ORDER BY ts;
```

`spo2_sleep` 是**独立表**，不与点测 `spo2` 混用；四组未解明语义的字节以 JSON 文本存放。

### CSV

```bash
python export_csv.py --db data/mi_band.db --output data/csv
```

（`--export-csv` 会在会话结束时自动做同样的事；会导出 `heart_rate`、`steps`、
`imu`、`spo2`、`activity_history`、`spo2_sleep` 六张表；CSV 表头为列名，UTF-8 编码。）

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
| `GET /api/history?kind=heart_rate&limit=600` | 单个指标的内存序列；加 `&source=db` 改读 SQLite 历史 |
| `GET /events` | SSE 事件流（先发 snapshot，再持续推送 sample） |
| `GET /healthz` | 存活探针 |

**历史回填**：页面加载时先并行请求四条 `?source=db` 序列，再打开 SSE；每个
snapshot（含 SSE 首帧）都会把持久化点与实时点按时间戳合并去重，所以刷新页面
不再从空图开始。数据库不存在、缺表或使用 `--storage jsonl|none` 时静默降级为
纯实时视图。DB 查询走线程池，不阻塞 BLE 所在的事件循环。

- 纯 Python 标准库实现（asyncio HTTP + SSE），不引入 Web 框架依赖；
- 实时部分仍只维护内存环形缓冲，慢客户端丢事件而不拖慢 BLE；写入走 `storage.py` 后台线程；
- 默认只监听 `127.0.0.1`，不要把看板直接暴露到公网。

> 这是**实时**看板。想回看已同步进 SQLite 的历史数据，用第 11.4 节的
> `history_dashboard.py` 生成离线单文件看板。

---

## 10. 实验性：血氧（SpO₂）与手环历史数据

小米手环 6 的**血氧不是实时通知流**，而是以历史记录形式存放在手环中，需要通过
私有 “fetch data” 协议拉取（与心率/步数/IMU 的实时流不同）。

```bash
# 连接后先拉取最近 7 天历史，再进入实时采集
.venv/bin/python miband_client.py 60 --sync-history 7

# 只做历史同步也可以（Ctrl+C 结束）
.venv/bin/python miband_client.py --sync-history 3
```

协议要点（依据 Gadgetbridge 的公开逆向结果重新实现，未复制其代码）：

- 控制/元数据特征 `00000004-...`，数据分片特征 `00000005-...`；
- 写 `[0x01, 数据类型] + 本地时间(6B) + [原因, 时区]` 启动；数据类型：活动 `0x01`、
  血氧 `0x25`。手环先回 `10 01 01 <计数 u32> <起始时间>`，再写 `[0x02]` 请求数据；
- 数据分片带递增计数（每片首字节，**每次拉取不重置**），完成后手环回 `10 02 01`，
  客户端回 `[0x03]` 确认；
- 血氧记录：版本 `2` + 每条 65 字节（int32 LE Unix 时间、1 字节 SpO₂、60 字节保留），
  负值表示自动测量（真实值 = 原始值 + 128）；
- 活动记录（Mi Band 6）：每分钟 8 字节：`kind, intensity, steps, heart_rate,
  unknown1, sleep, deep_sleep, rem_sleep`。

### 真机实测的两个协议陷阱

这两点只有真实手环才会暴露，模拟器最初也照着错误假设造数据，所以 CI 全绿却抓不到：

1. **`<计数 u32>` 对活动数据是「分钟数」，不是字节数**。请求 16 小时窗口时手环报
   960，实际却传 7680 字节。按字节数截断会丢尾或截出半条记录，因此客户端改成
   「一直读到完成响应 `10 02 01` 为止」，再按整记录裁剪。
2. **窗口内无新数据时，手环回填的是 1970-01-01**（`10 01 01 00 00 00 00 b2 07 01 01
   00 00 00 00`），而不是请求时间。直接 `datetime.fromtimestamp()` 在 Windows 上会抛
   `OSError: [Errno 22]`，看起来像拉取失败，实际只是「没有新数据」。客户端现在把
   2000 年之前的时间视为无效并回退到请求窗口。

> ✅ **真机验证结果**（Mi Band 6，Windows / WinRT，MTU 247）：
> 2 天窗口一次拿到 **1920 条活动记录**（2020-01-06 22:03 → 01-08 14:02 本地时间，
> 零重复、时间单调、合计 10675 步），血氧取到 1 条记录（100%，自动测量关闭）；
> 1 天窗口正确返回空并正常确认，不再报错。重复运行结果一致。
> 协议参考：[Gadgetbridge](https://codeberg.org/Freeyourgadget/Gadgetbridge)
> 的 `HuamiSupport` / `operations/fetch`（AGPL-3.0，仅作协议事实参考）。
>
> 📋 逐项核对清单见 [docs/history_hardware_validation.md](docs/history_hardware_validation.md)。
> 需要原始 payload 硬证据时运行：
> `python tools/capture_history_payload.py --days 7 --output validation/payloads`。

拉取到的数据会进入 `spo2.jsonl` / `activity_history.jsonl`、SQLite 的 `spo2` /
`activity_history` 表、CSV 导出，并实时推送到看板的血氧卡片/曲线。

### 睡眠血氧（0x26）：独立表 + 整夜曲线

2020-01-08 在 Mi Band 6 / Windows WinRT 上完成真机验证：一次请求拿到
**12901 字节 = 1 + 430×30**，版本字节 `2`，430 条记录时间戳严格递增、覆盖
21:39 → 次日 05:39（主要间隔 60 秒），SpO₂ 全部落在 80–100、均值 95.5，
无一条越界。前两条记录已作为字节级回归用例固化在 `tests/test_history.py`。

```bash
# 主程序：需配合 --sync-history；写入独立 spo2_sleep 表 + spo2_sleep.jsonl
python miband_client.py 60 --sync-history 7 --sync-sleep-spo2

# 取证工具：把 0x26 原始 payload 存成 .bin + .json（含 SHA-256）
python tools/capture_history_payload.py --days 7 --spo2-sleep --output validation/payloads

# 把取证 .bin 导入正式数据链路（手环 ACK 后可能清数据，这常是唯一取回途径）
python tools/import_spo2_sleep.py --output data validation/payloads/history_26_*.bin
```

布局依据 Gadgetbridge 的 `FetchSpo2SleepOperation`（AGPL-3.0，仅参考协议事实），
并已由上述真机 payload 确认：版本字节 `2` + N×30B 记录，每条为
`int32 LE 时间戳 / uint8 SpO₂ / uint8 duration / 6B high / 6B low /
8B signal / 4B spare`。它与普通血氧（65B、有符号值 + 自动测量标志）**不同**，
所以 `_trim_to_records()` 对 0x26 按 30B 裁剪。真机上 `high` / `low` 两组确实是
99–100 的 SpO₂ 采样值，佐证了字段划分；但 `duration` 集中在 16/32/48/64，
`signal` / `extend` 亦无确定语义，上游处理器仍标 `TODO save`，因此日志仍标
`[experimental]`，且默认不请求（不影响常规采集）。

**数据落点**：样本经 `on_spo2_sleep` 回调进入 `TelemetryStore`，同时写入
`spo2_sleep.jsonl`、**独立** `spo2_sleep` SQLite 表和 `spo2_sleep.csv`，
绝不混入点测 `spo2` 表。四组尾部字节以 JSON 文本原样保留。
`tools/import_spo2_sleep.py` 可把取证 `.bin` 补导入，按时间戳去重，可重复运行。

**离线看板**：`history_dashboard.py` 把整夜血氧画成独立曲线（与点测血氧分成两节），
优先读 `spo2_sleep` 表；表为空时自动回退同目录的 `spo2_sleep.jsonl`，也可用
`--spo2-sleep-jsonl` 指定任意位置。KPI 的「血氧最低」取两种来源的合并最小值，
hint 里分别标明条数，避免被读成同一组连续测量。

本地历史汇总：

```bash
.venv/bin/python history_report.py --hours 24       # 最近 24 小时文本报告
.venv/bin/python history_report.py --hours 168 --json
```

---

## 11. 睡眠推导与离线历史看板

### 11.1 三个字节是评分，不是阶段时长

活动历史里的 `sleep` / `deep_sleep` / `rem_sleep` **不是各阶段分钟数**，也不是任何可以
换算成分钟的量。三种候选读法都拿真实的 Zepp 数字对过表（01-06 夜：深睡 **44 分钟 = 8%**）：

| 读法 | 深睡 | 结论 |
| --- | --- | --- |
| `分数 / 128` 积分 | 96 分钟（27%） | 差 52 分钟，且把 REM 算成 82 分钟（那一夜 App 报 0） |
| Gadgetbridge 阈值（REM>55 / 深睡>42） | 128 分钟（35%） | 差 84 分钟 |
| 心率排名启发式 | 50 分钟（11%） | 最接近，采用 |

三个字节本身确实在编码某种“分数”（bit7 是已评分标记，低 7 位 0–127），但它与 App 显示的
阶段分钟数**没有对应关系**，而且证据是**倒挂**的：

| 夜 | App 报 REM | `rem_sleep` 低 7 位 | App 报深睡 | `deep_sleep` 低 7 位 |
| --- | --- | --- | --- | --- |
| 01-06（心率 485/485） | **0 分钟** | 458/485 分钟非零 | 44 分钟 | 458/485 分钟非零 |
| 01-07（心率 0/396） | **87 分钟** | 396/396 全为 0 | 74 分钟 | 396/396 全为 0 |

App 报 0 时字节几乎分钟分钟非零，App 报 87 分钟时字节全零——不存在任何单调缩放能让两列
同时成立。全库统计也支持这个读法：`rem_sleep` 非零的 463 分钟**全部**当分钟有心率，有心率
但字节为 0 的 755 分钟**全部**落在白天非睡眠段。所以两列的语义是“睡眠窗口内、有心率时算出
的一个判别分数”，不是阶段时长。本项目因此**不输出 REM**，也不把三列积分成分钟；它们留在
数据库里作为原始证据。

顺带一个反例：`rem_sleep` 与心率正相关（45–49 bpm → 27 分，65–69 → 50 分），`deep_sleep`
负相关（38 → 20），两者 `r = -0.43`。这是一对竞争判别项的输出，不是两个独立测量的时长。

手环真正可靠的睡眠窗口信号是 `kind`——它自己的活动状态机：

| `kind` | 含义 | 判定 |
| --- | --- | --- |
| `0xF0` / `0xF9` / `0xFA`（240/249/250） | 睡眠 | `kind & 0xF0 == 0xF0` |
| `0xF3`（243） | **未佩戴** | 低四位为 `3`，Gadgetbridge 的 `TYPE_NONWEAR` |
| `0x01` / `0x10` / `0x11` | 步行/活动 | 非睡眠、有位移 |
| `0x50` / `0x60` / `0x70` | 静止/睡前 | 非睡眠、无位移 |

`0xF3` 是唯一需要小心的一格：它落在睡眠家族里，但手环用它表示「没戴在手腕上」。
判据不是猜的——那 679 分钟**一条心率都没有**（其余 kind 约 100% 有心率），且全部零强度
零步数；详见 [`docs/sleep_fields_investigation.md`](docs/sleep_fields_investigation.md) §6。

所以本项目的睡眠结果是**推导值**，分两步，每一步都对过 Zepp 的数字：

1. **窗口**：`kind` 的睡眠家族为**锚点**，向两侧按“静止（无步数）且未佩戴为否”延伸，
   最多延伸 60 分钟；锚点少于 10 分钟或睡眠不足 20 分钟的段丢弃。
   依据：Zepp 的清醒时长（20 分钟）与 `steps > 0` 的分钟数（17）几乎相等，
   而用 `kind` 直接当窗口会前后丢 124 分钟。
2. **分期**：窗口内按心率水平排名 + 短时心率变异性排名切深睡，其余为浅睡；
   无心率时为 `unknown`。阈值集中在 `sleep_analysis.py` 顶部常量里。

腕部没有 EEG，**结论用于看趋势，不作逐分钟判读**。若某夜有睡眠段但无心率，深睡/浅睡
显示为 `unknown`，而**睡眠时长仍然给得出**（它由体动决定，不需要心率）；若那一夜 `kind`
全是 `0xF3`，则直接判为**未佩戴**，不生成睡眠段、也不进看板的“最近一晚”。

```bash
.venv/bin/python sleep_analysis.py --db data/mi_band.db            # 文本报告
.venv/bin/python sleep_analysis.py --db data/mi_band.db --json     # 结构化
.venv/bin/python sleep_analysis.py --db data/mi_band.db --night 2020-01-07
```

### 11.2 与 Zepp Life 对表

App 上六夜的数字（原始记录见 `data/zepp_nights.json`）：

| 日期 | 得分 | 卧床 | 睡眠 | 深睡 | 浅睡 | REM | 清醒 | 手环有这一夜 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 01-01 | 75（胜 38%） | 23:06 → 07:36 | 7h57 | 1h02（13%） | 6h55（87%） | 无此栏 | 33 | 没有 |
| 01-02 | 86（胜 71%） | 22:07 → 06:35 | 8h27 | 58（11%） | 7h29（89%） | 无此栏 | 1 | 没有 |
| 01-03 | 88（胜 78%） | 21:45 → 06:34 | 8h40 | 1h08（13%） | 7h32（87%） | 无此栏 | 9 | 没有 |
| 01-05 | 75（胜 38%） | 21:16 → 05:42 | 8h05 | 44（9%） | 7h21（91%） | 无此栏 | 21 | 没有 |
| 01-06 | 73（胜 34%） | 21:06 → 06:06 | 8h40 | 44（8%） | 7h56（92%） | 无此栏 | 20 | **有**（缺一小时） |
| 01-07 | 92（胜 90%） | 22:44 → 05:11 | 6h27 | 1h14（19%） | 3h46（58%） | **1h27（23%）** | 0 | 没有（未佩戴） |

三件事从这张表里定下来：

1. **REM 只出现在 01-07，而那一夜手环没戴。** 手环佩戴的五夜全都没有 REM 栏，所以"这块
   手环的夜晚没有 REM"（n=5）成立；唯一有 REM 的那页来自同一账号下的另一台设备
   （它的窗口在库里是 396 分钟 `0xF3`、0 条心率、两个睡眠字节全是 `0x80`）。这也意味着
   **Zepp 的睡眠页不总是这块手环的数据**，用它当基准必须逐夜核对来源。
2. **"深睡固定比例"的怀疑排除。** 六夜深睡是 62/58/68/44/44/74 分钟（占睡眠 8%–19%），
   既不是常数也不是固定百分比——两次 44 只是巧合。
3. **得分主要惩罚觉醒，不是奖励时长。** 得分与清醒分钟 `r = -0.87`、与睡眠效率 `+0.86`、
   与睡眠时长 `-0.47`（得分最高那夜只睡 6h27m）。清醒 ≤9 分钟的三夜 86/88/92，≥20 分钟的
   三夜 73/75/75。所以**分数不能当校验基准**，它看重的东西和我们能可靠测的不是一回事。

有用的副产品是一个先验：手环五夜的深睡占睡眠 8%–13%，本项目 01-06 报 11%，落在区间内，
**不需要为了让某个数字对上而去拧阈值**。

按重叠区间对表：

| 指标 | 我们 | Zepp Life（同区间折算） |
| --- | --- | --- |
| 卧床 | 22:06 → 05:55，470 分钟 | 22:06 → 06:06，480 分钟 |
| 睡眠 | 453 分钟 | ~460 分钟 |
| 清醒 | 17 分钟 | 20 分钟 |
| 深睡 | 50 分钟（11%） | 44 分钟（8%） |

反推出来的三条规则就是 11.1 里写的实现：**清醒按步数、窗口按锚点 + 静止延伸、不输出 REM**。
深睡的两个阈值（`DEEP_HR_RANK` / `DEEP_SD_RANK`）是按这一夜调的，**只有一夜样本，属于临时值**。

样本不够时不猜，交给工具算：

```bash
.venv/bin/python tools/calibrate_sleep.py --zepp data/zepp_nights.json
```

它把四种读法放在同一张表里（`integral` / `gb` / `heuristic` / 逐阈值扫描），并在可用夜少于
两夜时**拒绝给出拟合结论**。补样本只要三步：**戴着睡 → 当天同步 → 当天把 Zepp 数字记进
JSON**；第二步是硬前提，手环收到 `03` ack 后会清掉已上传的分钟，01-05 就是这么丢的。
完整取证见 [`docs/sleep_fields_investigation.md`](docs/sleep_fields_investigation.md) §7–§8。

### 11.3 与 Gadgetbridge 交叉核对

Gadgetbridge 实现了同一套 Huami 协议，源码可核对。四条结论（细节与代码引用见
[`docs/sleep_fields_investigation.md`](docs/sleep_fields_investigation.md) §6）：

| 问题 | 结论 |
| --- | --- |
| 8 字节布局 | `FetchActivityOperation.createExtendedSample()` 逐字段同名同序，`MiBand6Support` 的 `getActivitySampleSize()` 就是 8 |
| 三字节是不是时长 | Gadgetbridge 也**不**当时长：`& 127` 取分数后与阈值比较（REM > 55、深睡 > 42），作者自注 “thresholds are arbitrary … not accurate” |
| Zepp 的时长从哪来 | 另一条命令 `SLEEP_SESSION (0x48)`，594 字节/条，含分期段列表、四类总分钟数和 `0x16` 处的**睡眠得分** |
| 手环 6 能拿到吗 | **不能**。`supportsSleepScore()` 默认 false 且 `MiBand6Coordinator` 未覆盖；真机实测 `0x48` 返回 status 成功但 0 条记录 |

真机实测（`tools/probe_fetch_types.py`）：

```
type 0x48: 10010100000000b207010100000000   accepted, declares 0
type 0x01: 100101db070000ea070b0117070020   accepted, declares 2011
```

同一次核对还纠正了一个真实错误：`kind` 的低四位才是活动码（`rawKind &= 0x0f`），
`3` 是 **未佩戴**。这解释了 01-07 那一夜为何一条心率都没有——手环根本不在手腕上，
而在此之前本项目把它算成了“卧床 11.3 小时”。

Gadgetbridge 对拿不到会话记录的设备也有推导实现（`util/sleep/SleepStageEstimator.kt`），
用 Cole-Kripke + Webster 判醒睡、用相对滑动基线的心率偏移与变异度分期，并靠
“深睡 10–25% / REM 15–30%”的闭环自适应调阈值——比本项目的固定分位更稳，值得后续借鉴。

### 11.4 离线历史看板（单文件 HTML）

`history_dashboard.py` 把 SQLite 里的活动历史、心率、点测血氧与整夜睡眠血氧
烘焙成一个 HTML 文件，**没有任何外部资源**（无 CDN、无字体、无图片），
可以直接双击打开、发邮件、存档：

```bash
.venv/bin/python history_dashboard.py --db data/mi_band.db -o dashboard_history.html --days 7

# 只有取证 JSONL、没导入数据库时，可显式指定来源
.venv/bin/python history_dashboard.py --db data/mi_band.db -o dashboard_history.html \
    --spo2-sleep-jsonl validation/payloads-26/spo2_sleep.jsonl
```

内容：KPI 卡片（最近一晚、深睡占比、睡眠效率、今日步数、心率区间、血氧最低）、
睡眠结构（服务端渲染的 SVG 分期图，按 run-length 压缩，任意宽度都清晰）、
心率曲线、按天分行的活动节律条（紫=睡眠/绿=活动/深灰=静止）、每日步数、
**整夜睡眠血氧曲线**与**点测血氧记录表**（分成两节）。

三个刻意的设计：

- 活动节律条按**真实时间戳**定位而不是按序号，因为手环历史不是每分钟连续网格，
  缺一分钟就会让后面所有格子整体错位；
- 心率缺失的夜晚在“分期图”位置画一条卧床时段灰带，而不是四条空泳道假装有数据；
- 整夜血氧（0x26，每分钟连续流）与点测血氧（0x25，偶发单次）**分开成两节**；
  曲线按真实时间戳绘制，超过 5 分钟的空档断线，不伪造连续线段。

---

## 12. 运行测试

无需任何蓝牙硬件：

```bash
.venv/bin/python -m pytest -q
```

覆盖内容：

- `test_miband.py`：AES 测试向量、心率/步数/IMU 解析、密钥格式；
- `tests/test_ecdh_backends.py`：原生 C 与纯 Python ECDH 的**逐字节交叉校验**、后端选择、各平台库文件名；
- `tests/test_device_matching.py`：MAC/UUID/名称匹配、占位符与环境变量优先级、CLI 参数、macOS 式按名称发现；
- `tests/test_storage.py`：时间戳、JSONL/SQLite 写入（含血氧/活动历史）、CSV 导出、关闭后写入丢弃；
- `tests/test_chunked_protocol.py`：分块帧格式、MTU 协商与透传、认证重组（序号缺口/重复/越界/非法长度）；
- `tests/test_simulated_band.py`：模拟手环端到端认证、三类实时数据流、断连续连、落盘与导出、认证期间断连快速失败（重连用例按 ECDH 后端自适应时序窗口）；
- `tests/test_dashboard.py`：遥测 Hub、HTTP API、SSE 实时推送、看板路由与回退页、SQLite 历史 provider 与 CLI 接线；
- `tests/test_history.py`：时间编码、血氧/活动解析、0x26 30B 布局与裁剪、模拟 fetch 协议（含丢包）、历史落盘、历史报告、CLI；
- `tests/test_sleep.py`：`kind` 家族判定（含 `0xF3`=未佩戴）、锚点 + 静止延伸的窗口、按步数判清醒、深睡分期、无心率夜返回全 `unknown`、未佩戴夜不生成睡眠段、真机数据与 Zepp 的回归对齐、逐分钟分数积分、CLI 文本/JSON/指定夜；
- `tests/test_history_dashboard.py`：空库与缺表的容错、时间窗口过滤、255 哨兵不进均值、活动条保留真实时间戳且按会话归属着色、分期图 SVG 覆盖率与泳道顺序、整夜血氧表/JSONL 回退与图表、模板注入无残留占位符、单文件自包含、CLI；
- `tests/test_import_spo2_sleep.py`：0x26 `.bin` 导入双写、幂等去重、dry-run 与非法 payload 拒绝；
- `tests/test_calibrate.py`：`tools/calibrate_sleep.py` 的四种读法（1/128 积分、Gadgetbridge 阈值、启发式、逐阈值扫描）与"可用夜不足时拒绝拟合"的护栏；
- `tests/test_probe_declared.py`：`tools/probe_declared.py` 只写起始命令、绝不发取数/回执，零填充的回显日历不被误当成锚点，固件拒绝（`0x02`）如实上报。

预期结果：`213 passed`。`--demo` 全链路（含 ECDH、存储、导出、0x26 采集与导入）不依赖蓝牙硬件，
可作为 Windows/macOS 上的冒烟测试；`.github/workflows/ci.yml` 会在 Linux/Windows/macOS
三平台运行同一套测试。

---

## 13. 常见问题（FAQ）

| 现象 | 原因/处理 |
|---|---|
| `No Bluetooth adapters found` / `org.bluez was not provided` | 本机无蓝牙栈（如 WSL2 未直通、`bluetooth` 服务未启动）。安装/启动 BlueZ，或直通 USB 蓝牙适配器。 |
| Windows：`OSError: [WinError 193]` 或加载 `libecdh.so` 失败 | 已修复：Windows 会跳过 Linux 的 `.so` 并自动回退纯 Python。若仍看到，请升级到当前版本或设 `MIBAND_ECDH_BACKEND=python`。 |
| Windows：`BLE device not found` / 连接被拒绝 | 在“设置 → 蓝牙和其他设备”确认适配器可用；删除该设备的旧配对记录；关闭手机蓝牙；必要时用 `scan_ble.py` 确认地址。 |
| macOS：扫描不到 / 地址对不上 | macOS 不暴露 MAC：用 `scan_ble.py` 输出的 **UUID** 填 `DEVICE_ID`，或配置 `DEVICE_NAME`；并在“系统设置 → 隐私与安全性 → 蓝牙”中允许终端。 |
| 90 秒内扫描不到手环 | 手机关蓝牙；点亮并触摸手环屏幕；确认 `.env` 的 MAC/UUID 正确；用 `scan_ble.py` 验证。 |
| 认证超时 | 分块通知丢失或固件行为差异；查看 `--log-level DEBUG`。客户端会在下次会话自动重试。 |
| 认证成功但心率不动 | 手环需佩戴贴合；`0x2A39` 控制序列可能被固件拒绝；重连一次或重启手环。 |
| IMU 无数据 | 部分固件需先进入特定运动模式；`--imu` 为实验性功能。 |
| `--sync-history` 报 `band rejected fetch type` | 该固件/型号不支持对应历史类型，或 fetch 特征与预期不同；历史同步失败不影响实时采集。 |
| `--sync-history` 超时/校验失败 | 分片丢失或固件行为差异；重连会重试。真机请附 DEBUG 日志反馈。 |
| Zepp 上有数据，手环里取不到 | 三种原因完全不同：① 已被 App 取走——手环只保留最近 **40 小时（2400 分钟）**的分钟数据，每分钟被新数据挤掉一行，`0x03` 收据之后 App 取走的那几段还会变成永久空洞；② 手环从不产这类数据（深睡/REM/得分，是 App 拿同一批原始数据现算的，`0x48` 声明 0 条为证）；③ 那一夜未佩戴（`0xF3`），App 显示的是同账号下的另一台设备。见 `docs/sleep_fields_investigation.md` §9–§10。 |
| `--sync-history 7` 却只拿到 40 小时 | 正常。参数只是"我最多问多久"，手环的环形缓冲就那么大。01-08 实测：两次抓取跨度都是精确的 2399 分钟，起点按 1:1 前移。想留住某一夜，必须在那夜结束后 40 小时内同步。 |
| 想确认手环还留着多早的数据 | `python tools/probe_declared.py 168 24`：只问"从某时刻起还有多少分钟"，不取数据、不回执，问完即断，不会扰动缓冲。 |
| 看板打不开 / 端口占用 | `--web-port 0` 使用随机端口；看板默认仅本机可访问，远程查看需 `--web-host 0.0.0.0` 并注意防火墙。 |
| 看板有数据但曲线不动 | 浏览器 SSE 断线会自动重连；确认页面未被休眠，或刷新页面重新拉 `/api/snapshot`。 |
| 连接一段时间后断开 | 手环省电/离体/手机抢占。默认启用指数退避自动重连。 |
| 数据重复（步数） | 步数流会先读一次当前值再订阅通知，首条可能重复，属预期行为。 |
| 时间戳含义 | 实时流为**电脑接收时间**；历史同步数据为**手环采样时间**。 |

---

## 14. 已知限制与后续方向

- 血氧/活动历史与睡眠血氧 `0x26` 的 30B 布局均已在 Mi Band 6 真机验证；`0x26` 已写入
  独立表与离线看板，但 `duration` / `signal` / `extend` 语义未定，仍标 `[experimental]`
  且默认不请求（需 `--sync-sleep-spo2`）；整夜曲线只作趋势观察，不作医学判读；
- 仅覆盖心率 / 步数 / IMU / 血氧(历史) / 活动(历史)；压力、训练详情、电量等尚未实现；
- 睡眠分期是**推导值**：窗口来自手环 `kind`，深睡/REM 由心率水平与心率变异性排序得出。
  腕部设备没有 EEG，不能替代多导睡眠图；某夜无心率时只报卧床时长，不猜分期；
  手环三字节「阶段分数」的积分读法仍未定案，等与 Zepp 同一夜时长对照后裁决；
- IMU 为实验性私有协议，采样率与量程以固件为准；`0x01` 光学/PPG 与 `0x07` 状态帧只识别不解析；
- 看板刷新时可从 SQLite 回填历史，但仍是单机视图：合并窗口上限 300 点，且实时流 `ts`
  是电脑接收时间、历史 `ts` 是手环采样时间；长跨度分析请用 SQLite/CSV 或
  `history_dashboard.py` 的离线单文件看板；
- Windows/macOS 已完成代码层兼容（BLE 通用 API + ECDH 纯 Python 回退 + 设备标识抽象），
  但**真机行为仍需在对应系统上验证**，验证清单见 [`docs/cross_platform.md`](docs/cross_platform.md)；
- 断连检测依赖连接回调与心率保活，极端情况下最长约 12 秒才发现链路丢失。

---

## 15. 安全与第三方

- `AUTH_KEY` 是手环的蓝牙配对凭据，请视同密码保管；`.env` 已被忽略。
- 看板/HTTP API 默认只绑定 `127.0.0.1` 且无鉴权，请勿直接暴露到公网。
- `huami_token/` 来自第三方 `huami-token` 项目（MIT License, © 2025 Kirill Snezhko），
  用于获取 auth_key；`ecdh.c/ecdh.h` 为 tiny-ECDH 的 C 实现。
- `vendor/tiny_ecdh/` 为第三方 `tiny-ECDH-python` 0.1.0（MIT License,
  © 2023 Daniel Oliveira），作为无原生库平台上的纯 Python ECDH 回退；
  与本项目 C 实现逐字节交叉校验（`tests/test_ecdh_backends.py`）。
- Chunked ECDH 与历史 fetch 协议参考 Gadgetbridge 项目（AGPL-3.0）的公开逆向成果，
  本项目为独立 Python 实现，未复制其源代码。
- 本项目仅用于个人健康数据的自主采集与分析，请遵守手环使用条款与当地隐私法规。

### 隐私与数据保护

本项目会产生并落盘大量个人数据，仓库只应保存代码与脱敏文档（文档日期统一为占位日期，示例数据均为合成值或聚合统计）：

| 内容 | 位置 | 敏感信息 | git 状态 |
|---|---|---|---|
| 设备 MAC / auth_key | `.env` | 是（配对凭据） | 已忽略，权限 0600 |
| 实时/历史健康数据 | `data/*.jsonl`、`data/mi_band.db`、`data/csv/` | 是（个人健康数据） | 已忽略 |
| 真机验证材料 | `validation/`（run.log、payloads、btmon） | 是（MAC / 数据 / 空口包） | 已忽略 |
| 调试日志与抓包 | `*.log`、`*.btsnoop`、`*.pcap` | 是 | 已忽略 |
| 代码与文档 | 仓库根、`docs/` | 否（示例均为占位符） | 跟踪 |

- 代码与文档中的 MAC 一律使用占位符（如 `C0:FF:EE:C0:FF:EE`），不要写入真实设备地址。
- 文档中的采集日期统一为占位日期；测试用例里的日期仅是与本地数据/时间窗对齐的功能性基准，不代表采集记录。
- 对外反馈问题前，按 [`docs/history_hardware_validation.md`](docs/history_hardware_validation.md) §9
  对日志做 MAC 脱敏，并确认其中不含 `auth_key`。
- 提交前可自查：`git status --ignored`、`git ls-files -ci --exclude-standard`。

---

## 16. 许可证

本项目以 [MIT License](LICENSE) 发布，Copyright © 2026 alex-j-zhang。

第三方组件的许可证归属见上文 §15「安全与第三方」及其自带声明：

| 路径 | 来源 | 许可证 |
|---|---|---|
| `huami_token/` | huami-token（© 2025 Kirill Snezhko） | MIT |
| `vendor/tiny_ecdh/` | tiny-ECDH-python 0.1.0（© 2023 Daniel Oliveira） | MIT |
| `ecdh.c` / `ecdh.h` | tiny-ECDH C 实现（Kenneth MacKay） | Public domain |
