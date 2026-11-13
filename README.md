# 小米手环 6 健康数据实时监测（Mi Band 6 Health Monitor）

通过 **BLE 直连** 小米手环 6，绕过手机 App，把心率、实时步数和原始 IMU 加速度数据
实时传输到电脑，并带时间戳落地为 JSONL / SQLite，可一键导出 CSV，便于后续分析与二次开发。

支持 Linux / Windows / macOS；无硬件时可用内置模拟手环跑通全链路；代码按领域拆分在
`miband_monitor/` 包中，历史脚本保留为兼容入口。

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
| 睡眠推导 | `sleep_analysis.py`：用 `kind` 状态机判定睡眠窗口，再按心率水平与心率变异性排序区分深睡 / 浅睡（**推导值**，不输出 REM；手环不直接上报分期） |
| 离线历史看板 | `history_dashboard.py` 生成**单文件 HTML**：KPI、睡眠结构图、心率、活动节律、步数、整夜血氧曲线、点测血氧表；无外部资源，可离线打开或原样分享 |
| 时间戳 / 持久化 | 每条记录带 Unix 秒 + UTC ISO-8601 毫秒时间；JSONL 追加写 + SQLite WAL，可任选或同时 |
| CSV 导出 | 从 SQLite 导出 6 张 CSV 表 |
| 自动重连 | 会话失败/链路丢失后指数退避重连；心率保活失败也会触发重连 |
| 无硬件演示 | `--demo` 使用内置模拟手环跑通完整链路（认证、数据流、历史同步、落盘、导出） |
| 测试 | 270+ 单元/集成测试，含模拟 BLE 后端，**不需要真实手环**；CI 覆盖 Linux/Windows/macOS |

---

## 2. 工作原理

```
┌──────────────┐  1. 账号登录（可选，仅首次）  ┌───────────────────────────┐
│ 小米/Zepp 账号 │ ────────────────────────────> │ miband_monitor.auth       │
└──────────────┘   get_auth_key.py /           │ → 写入 .env 的 AUTH_KEY   │
                   fetch_by_token.py           └──────────┬────────────────┘
                                                           v
┌──────────────┐  2. BLE 扫描 / 连接 / 认证     ┌───────────────────────────┐
│   小米手环 6  │ <───────────────────────────> │ miband_monitor.ble        │
│              │  3. 三类实时 GATT 通知         │ ├─ bluez（Linux 兼容层）   │
│              │ ────────────────────────────> │ ├─ crypto（ECDH 双后端）   │
└──────────────┘                               │ └─ 心率/步数/IMU 解析      │
                                               └──────┬───────────────┬─────┘
                                                      │ 带时间戳       │ 历史 fetch
                                           ┌──────────v───────┐  ┌────v─────────────────┐
                                           │ storage          │  │ history（0x0004/5）   │
                                           │ JSONL + SQLite   │  │ parsers + sync       │
                                           │ 后台单写线程      │  └────┬─────────────────┘
                                           └──────┬─────┬─────┘       │
                                                  │     │             │
                                     ┌────────────v─┐ ┌─v──────────┐  │
                                     │ export_csv   │ │ dashboard  │  │
                                     │ → CSV        │ │ HTTP + SSE │  │
                                     └──────────────┘ └────────────┘  │
                                                                     v
                                                       sleep / offline_dashboard
                                                       reports（SQLite → 报告 / HTML）
```

认证流程（简化）：

1. 客户端发送自己的 B-163 公钥（内层消息 `04 02 00 02 + pubkey`，分块发送）。
2. 手环返回 `16 字节随机盐 + 48 字节公钥`（分块通知，带序号；客户端校验连续性并重组）。
3. 双方计算 48 字节 ECDH 共享密钥，取 `shared[8:24] XOR auth_key` 得到会话 AES key。
4. 客户端回发 `0x05 + AES-CBC(auth_key, 盐) + AES-CBC(session_key, 盐)`，手环确认后认证成功。

历史数据（`--sync-history`）通过手环私有 `0x0004`/`0x0005` 特征拉取：
活动数据按分钟存储，血氧按次存储，随后同样进入上面的存储/看板/导出链路。
协议细节与真机证据见 [docs/validation/history-hardware.md](docs/validation/history-hardware.md)。

---

## 3. 项目结构

```
miband_health_monitor/
├── miband_monitor/             # 第一方实现（按领域分包，每包见 docs/modules/）
│   ├── config/                 # 默认值、环境变量、路径、隐私与权限
│   ├── crypto/                 # ECDH 原生 + 纯 Python 双后端
│   ├── bluez/                  # Linux/BlueZ D-Bus 兼容层
│   ├── ble/                    # BLE 客户端、协议编解码、认证、数据流
│   ├── history/                # 手环历史 fetch、解析、同步
│   ├── storage/                # JSONL/SQLite schema、写线程、CSV 导出
│   ├── dashboard/              # 实时看板（HTTP + SSE + 前端资源）
│   ├── sleep/                  # 睡眠窗口/分期推导
│   ├── offline_dashboard/      # 离线单文件 HTML 历史看板
│   ├── simulator/              # 内存模拟手环 + 假 bleak 后端
│   ├── cli/                    # 命令行入口与会话监督
│   ├── diagnostics/            # --doctor 与扫描排障
│   ├── reports/                # 本地历史汇总报告
│   └── auth/                   # 账号获取 auth_key
├── miband_client.py            # 兼容入口：转发到 miband_monitor.cli
├── storage.py / dashboard.py / band_history.py / sleep_analysis.py / ...
│                               # 兼容 shim：旧 import 路径继续可用
├── ecdh.c / ecdh.h / libecdh.so              # 第三方 tiny-ECDH（C / 预编译）
├── vendor/tiny_ecdh/           # 第三方纯 Python ECDH 回退（MIT）
├── huami_token/                # 第三方华米云 API 客户端（MIT）
├── tools/                      # 诊断 / 取证 / 文档校验脚本
├── tests/                      # 无硬件测试套件（按领域拆分）
├── docs/                       # 文档中心，入口见 docs/README.md
├── .github/workflows/ci.yml    # Linux/Windows/macOS 测试矩阵
├── requirements.txt / pytest.ini / .env.example
└── README.md
```

> 旧脚本（`miband_client.py`、`storage.py`、`history_report.py` 等）保留为**兼容门面**，
> 行为与命令行参数不变；实际实现位于 `miband_monitor/<domain>/`。

---

## 4. 快速开始

### 4.1 安装

```bash
cd miband_health_monitor
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Windows 使用 `.venv\Scripts\python.exe`；分平台步骤见
[docs/guides/installation.md](docs/guides/installation.md)。

### 4.2 配置

```bash
cp .env.example .env          # 填入 DEVICE_ID（MAC 或 macOS UUID）与 AUTH_KEY
```

`.env` 已被 git 忽略并会被收紧到 0600。完整变量表见
[docs/guides/configuration.md](docs/guides/configuration.md)；没有 `AUTH_KEY` 时可用
`get_auth_key.py` / `fetch_by_token.py` 获取。

### 4.3 自检与无硬件演示

```bash
# 环境自检：适配器、配对状态、ECDH 后端、.env 权限（只读，不连接手环）
.venv/bin/python miband_client.py --doctor

# 无硬件跑通全链路（模拟手环，含 IMU、历史同步与 CSV 导出）
.venv/bin/python miband_client.py --demo 2 --imu --sync-history 1 \
    --output data/demo --export-csv
```

### 4.4 真机采集

```bash
.venv/bin/python miband_client.py 60                 # 60 秒心率 + 步数
.venv/bin/python miband_client.py 60 --imu           # 附加 25Hz IMU
.venv/bin/python miband_client.py --export-csv       # 一直采到 Ctrl+C，并导出 CSV
.venv/bin/python miband_client.py --storage none     # 只看日志不落盘
.venv/bin/python miband_client.py 60 --sync-history 7 --sync-sleep-spo2
```

真机要点：**关闭手机蓝牙**；手环无手机链路时每 40–60 秒才发一个广播脉冲，
`--scan-timeout` 建议 120；Linux 首次使用先 `bluetoothctl trust <MAC>`。
详见 [docs/guides/cross-platform.md](docs/guides/cross-platform.md) 与
[docs/guides/faq.md](docs/guides/faq.md)。

### 4.5 看板、历史与报告

```bash
.venv/bin/python miband_client.py --web-port 8080            # 实时看板/API
.venv/bin/python history_report.py --hours 24                # 文本汇总
.venv/bin/python sleep_analysis.py --db data/mi_band.db      # 睡眠推导
.venv/bin/python history_dashboard.py -o history.html --days 7   # 离线单文件看板
.venv/bin/python export_csv.py --db data/mi_band.db --output data/csv
```

完整命令与参数见 [docs/guides/usage.md](docs/guides/usage.md)。

### 4.6 兼容旧命令与旧 import

重构只改变实现位置，不改变用户接口：

- 所有历史命令（`miband_client.py`、`history_report.py`、`export_csv.py`、
  `sleep_analysis.py`、`history_dashboard.py`、`scan_ble.py` …）参数与输出格式不变；
- 旧 import 路径（`from storage import TelemetryStore`、`import sleep_analysis` 等）
  由根目录兼容门面继续提供；
- CI 会校验第一方文件 ≤300 行、隐私扫描、文档本地链接与完整测试套件。

---

## 5. 文档导航

| 入口 | 内容 |
|---|---|
| [docs/README.md](docs/README.md) | 文档总索引与阅读路线 |
| [docs/architecture.md](docs/architecture.md) | 分层架构、依赖方向、数据流、第三方豁免与限制 |
| [docs/modules/](docs/modules/) | 15 个领域模块的职责、公开 API、数据流、失败模式、测试 |
| [docs/guides/installation.md](docs/guides/installation.md) | 三平台安装与依赖说明 |
| [docs/guides/configuration.md](docs/guides/configuration.md) | `.env` 全量变量、CLI 参数、优先级 |
| [docs/guides/usage.md](docs/guides/usage.md) | 采集、看板、历史同步、报告、睡眠、离线看板 |
| [docs/guides/data-formats.md](docs/guides/data-formats.md) | 数据目录、JSONL/SQLite/CSV 字段与时间语义 |
| [docs/guides/cross-platform.md](docs/guides/cross-platform.md) | Linux/Windows/macOS 差异与验证清单 |
| [docs/guides/testing.md](docs/guides/testing.md) | 测试布局、CI、演示冒烟与文档/隐私校验 |
| [docs/guides/faq.md](docs/guides/faq.md) | 常见问题与排障 |
| [docs/guides/privacy.md](docs/guides/privacy.md) | 数据分级、权限、清理与提交前检查 |
| [docs/validation/](docs/validation/) | 真机验证证据：历史协议、Linux 连接 |
| [docs/investigations/sleep-fields.md](docs/investigations/sleep-fields.md) | 睡眠三字节取证与 Zepp 对照结论 |

---

## 6. 隐私与安全摘要

- `AUTH_KEY` 等价于手环的蓝牙配对凭据，请视同密码保管；`.env` 默认被忽略并收紧到 0600。
- 心率、血氧、活动、IMU 与睡眠结果都是**个人健康数据**，全部写入 `data/`（默认被 git 忽略）。
- 代码与文档中的设备地址一律使用占位符（如 `C0:FF:EE:C0:FF:EE`）；仓库不含真实 MAC、密钥或家庭路径。
- 看板/HTTP API 默认只绑定 `127.0.0.1` 且无鉴权，不要直接暴露到公网。
- 对外反馈问题前先脱敏：见 [docs/guides/privacy.md](docs/guides/privacy.md) 与
  `tools/check_privacy.py`。

---

## 7. 许可证与第三方

本项目以 [MIT License](LICENSE) 发布，Copyright © 2026 alex-j-zhang。

| 第三方 | 位置 | 许可证 / 说明 |
|---|---|---|
| `bleak` | 依赖（requirements.txt） | MIT，BLE 跨平台封装 |
| `huami_token` | `huami_token/` | MIT，© 2025 Kirill Snezhko，华米/Zepp 云 API 客户端 |
| `tiny-ECDH`（C） | `ecdh.c` / `ecdh.h` | 上游 C 实现，编译为 `libecdh.so` 加速 |
| `tiny-ECDH-python` | `vendor/tiny_ecdh/` | MIT，© 2023 Daniel Oliveira，纯 Python 回退 |
| Gadgetbridge（协议参考） | 无代码复制 | AGPL-3.0，仅参考公开逆向的协议事实 |

第三方代码保持上游原样、不参与 300 行预算；完整豁免表与原因见
[docs/architecture.md](docs/architecture.md#6-第三方与兼容豁免)。

### 7.1 开发与校验

```bash
.venv/bin/python -m pytest -q                    # 无硬件测试（含模拟手环）
.venv/bin/python tools/check_file_sizes.py       # 第一方文件 ≤300 行
.venv/bin/python tools/check_privacy.py          # 仓库无真实 MAC/密钥/路径
.venv/bin/python tools/check_docs_links.py       # 相对 Markdown 链接可解析
```

测试布局、CI 三平台矩阵与真机验证清单见
[docs/guides/testing.md](docs/guides/testing.md)。

---

## 8. 状态与已知限制

- 血氧/活动历史与睡眠血氧 `0x26` 的 30B 布局均已在 Mi Band 6 真机验证；`0x26` 的
  `duration` / `signal` / `extend` 语义未定，仍标 `[experimental]` 且默认不请求。
- 仅覆盖心率 / 步数 / IMU / 血氧(历史) / 活动(历史)；压力、训练详情、电量等尚未实现。
- 睡眠分期是**推导值**（腕部无 EEG），只用于趋势与结构，不作逐分钟医学判读。
- 手环分钟数据只保留最近约 40 小时，App 取走的部分会留下空洞；想留住某一夜需及时同步。
- Windows/macOS 已完成代码层兼容，真机行为验证状态见
  [docs/guides/cross-platform.md](docs/guides/cross-platform.md)。

---

## 9. 反馈与贡献

反馈问题时请附上：

1. 操作系统与版本、Python 版本、`bleak` 版本、蓝牙适配器型号；
2. 启动日志前几行（`ECDH backend`、`Discovery target`、MTU）；
3. `--log-level DEBUG` 的完整日志（**先脱敏 MAC 与 auth_key**）；
4. 失败发生在哪一步、能否稳定复现。

不要提交 `.env`、`data/`、`validation/` 或抓包文件；脱敏与清理步骤见
[docs/guides/privacy.md](docs/guides/privacy.md)。协议与硬件证据请优先整理到
[docs/validation/](docs/validation/) 或 [docs/investigations/](docs/investigations/)。
