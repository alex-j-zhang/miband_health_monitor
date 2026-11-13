# 架构

本文描述 `miband_monitor/` 的分层、依赖方向、一次会话的数据流、兼容门面、第三方代码豁免
以及整个项目的限制。模块级细节见 [modules/](modules/)。

## 1. 设计目标

1. **一条命令跑通**：`python miband_client.py` 负责发现、认证、采集、落盘、可选看板；
2. **无硬件可测试**：模拟手环实现同一套线协议，CI 不接触真实蓝牙；
3. **可替换的边界**：BLE 后端、ECDH 后端、存储后端、看板数据源都通过参数注入；
4. **协议证据可追溯**：所有逆向后与真机相关的常量、超时、字段布局在代码注释和
   [validation/](validation/) 中保留出处；
5. **隐私优先**：凭据与健康数据只落在本地，权限收紧、默认 git 忽略。

## 2. 分层

```
                         ┌────────────────────────────────────────────┐
   入口 / 编排            │ cli        diagnostics   auth     tools    │
                         └───────┬────────────┬──────────┬────────────┘
                                 │            │          │
                         ┌───────v────────────v──────────v────────────┐
   领域服务               │ ble   history   storage   dashboard       │
                         │ sleep offline_dashboard reports simulator  │
                         └───────┬────────────┬──────────┬────────────┘
                                 │            │          │
                         ┌───────v────────────v──────────v────────────┐
   基础设施               │ bluez        crypto          config       │
                         └────────────────────────────────────────────┘
```

| 层 | 包 | 规则 |
|---|---|---|
| 基础设施 | [config](../miband_monitor/config/)、[crypto](../miband_monitor/crypto/)、[bluez](../miband_monitor/bluez/) | 不导入领域包；config 只用标准库；crypto/bluez 可互相独立 |
| 领域服务 | [ble](../miband_monitor/ble/)、[history](../miband_monitor/history/)、[storage](../miband_monitor/storage/)、[dashboard](../miband_monitor/dashboard/)、[sleep](../miband_monitor/sleep/)、[offline_dashboard](../miband_monitor/offline_dashboard/)、[reports](../miband_monitor/reports/)、[simulator](../miband_monitor/simulator/) | 只向下依赖基础设施；分析类包（sleep / offline_dashboard）不启动线程或网络 |
| 编排层 | [cli](../miband_monitor/cli/)、[diagnostics](../miband_monitor/diagnostics/)、[auth](../miband_monitor/auth/) + 根目录 `tools/` 脚本 | 可以依赖任意领域包；负责参数、交互、生命周期与退出码 |

**依赖方向必须单向**：`cli → {ble, history, storage, dashboard, simulator, diagnostics}`，
`ble → {bluez, crypto, config}`，`history → config`，`simulator → {ble, history}`。
禁止领域包反向导入 `cli`，禁止 `config` 导入其它项目包。

## 3. 运行时数据流

### 3.1 实时采集

1. [cli/app](../miband_monitor/cli/app.py) 解析参数、加载 `.env`、配置日志；
2. [cli/runner](../miband_monitor/cli/runner.py) 创建 [ble.MiBand6Client](../miband_monitor/ble/)，
   调用 `connect_and_authenticate()`；
3. 发现与建链：Linux 走 [bluez](../miband_monitor/bluez/) 的待命 `Connect` 路径，
   Windows/macOS 走 bleak 扫描；认证用 [crypto](../miband_monitor/crypto/) 的 ECDH 后端；
4. 认证成功后依次启动 IMU（可选）、心率、步数通知；
5. [cli/callbacks](../miband_monitor/cli/callbacks.py) 把每个样本转给
   [storage.TelemetryStore](../miband_monitor/storage/) 与可选的
   [dashboard.TelemetryHub](../miband_monitor/dashboard/)；
6. 退出时 `store.close()` 排空写队列，`--export-csv` 再导出 SQLite。

### 3.2 历史同步

`--sync-history` 在实时流启动**之前**执行：`history.sync_history()` 反复调用
`BandHistoryFetcher`，把活动/血氧/睡眠血氧 payload 交给
[modules/history.md](modules/history.md) 中的解析器，再由回调写入
`activity_history`、`spo2`、`spo2_sleep` 三个互不混用的落点。失败只记录日志，
不阻塞后续实时采集。

### 3.3 离线分析

SQLite 是分析与展示的共同数据源：

```
data/mi_band.db ──> reports.history      → 文本 / JSON 汇总
               ├──> sleep.analyze        → 睡眠窗口、分期、报告
               ├──> offline_dashboard    → 单文件 HTML（内嵌 SVG/Canvas 数据）
               └──> dashboard 的 source=db → 实时页面刷新回填
```

## 4. 包与模块清单

| 包 | 模块拆分 | 文档 |
|---|---|---|
| config | defaults / env / paths / privacy / security / health / demo | [modules/config.md](modules/config.md) |
| crypto | constants / native / pure_python / facade | [modules/crypto.md](modules/crypto.md) |
| bluez | constants / platform / dbus / devices / admin / link / pairing / mtu / scanner / errors | [modules/bluez.md](modules/bluez.md) |
| ble | protocol / parsers / identity / crypto / chunked / backends / discovery / link / mtu / listener / auth_* / notifications / streams / client | [modules/ble.md](modules/ble.md) |
| history | protocol / models / parsers / fetcher / sync | [modules/history.md](modules/history.md) |
| storage | schema / store / writer / sqlite / readers / export_csv | [modules/storage.md](modules/storage.md) |
| dashboard | constants / history / hub / http / page / server + assets | [modules/dashboard.md](modules/dashboard.md) |
| sleep | constants / samples / scoring / detection / classification / session / report / cli / calibration | [modules/sleep.md](modules/sleep.md) |
| offline_dashboard | constants / strings / sources / collect / packaging / kpis / svg / render / cli + assets | [modules/offline-dashboard.md](modules/offline-dashboard.md) |
| simulator | constants / payloads / devices / band / history / auth / streams / fake_bleak / factory | [modules/simulator.md](modules/simulator.md) |
| cli | args / app / callbacks / logging / runner | [modules/cli.md](modules/cli.md) |
| diagnostics | report / checks / doctor / scanning / live_scan | [modules/diagnostics.md](modules/diagnostics.md) |
| reports | history | [modules/reports.md](modules/reports.md) |
| auth | constants / extractor / token | [modules/auth.md](modules/auth.md) |

## 5. 兼容门面（root shims）

重构后仓库根目录仍保留以下文件，它们的职责只有"转发 + 保持公开名字"：

| 文件 | 转发目标 |
|---|---|
| `miband_client.py` | `miband_monitor.cli.app.main`（CLI 参数与行为不变） |
| `storage.py` | `miband_monitor.storage`（`TelemetryStore`、schema、reader） |
| `dashboard.py` | `miband_monitor.dashboard` |
| `band_history.py` | `miband_monitor.history` |
| `sleep_analysis.py` | `miband_monitor.sleep` |
| `history_dashboard.py` | `miband_monitor.offline_dashboard` |
| `history_report.py` | `miband_monitor.reports` |
| `export_csv.py` | `miband_monitor.storage.export_csv` |
| `sim_band.py` | `miband_monitor.simulator` |
| `ecdh_wrapper.py` | `miband_monitor.crypto` |
| `bluez_support.py` | `miband_monitor.bluez` |
| `doctor.py` / `scan_ble.py` / `scan_live.py` | `miband_monitor.diagnostics` |
| `get_auth_key.py` / `fetch_by_token.py` | `miband_monitor.auth` |

旧文档、旧脚本、第三方示例里的 `from storage import TelemetryStore` 等写法继续可用；
新代码应直接导入 `miband_monitor.<domain>`。

## 6. 第三方与兼容豁免

`tools/check_file_sizes.py` 要求**第一方**每个代码/资源文件 ≤300 行。以下内容保持上游原样，
有意豁免，且不参与重构：

| 内容 | 位置 | 许可证 | 豁免原因 |
|---|---|---|---|
| tiny-ECDH（C） | `ecdh.c` / `ecdh.h` | 上游 tiny-ECDH-c | 逐字节线格式依赖，改动会破坏与手环的兼容；编译产物 `libecdh.so` 为 Linux x86-64 预编译 |
| tiny-ECDH-python | [vendor/tiny_ecdh/](../vendor/tiny_ecdh/) | MIT © 2023 Daniel Oliveira | 无原生库平台（Windows/macOS/ARM/CI）的纯 Python 回退，必须与上游一致 |
| huami-token | [huami_token/](../huami_token/) | MIT © 2025 Kirill Snezhko | 华米/Zepp 云 API 客户端，用于获取 auth_key；只读使用 |
| 预编译库 | `libecdh.so` | 由上述 C 源码编译 | 二进制产物，不按行数审查；无库时自动编译或回退纯 Python |

**协议参考不等于代码复制**：Chunked ECDH 与历史 fetch 的流程参考 Gadgetbridge
（AGPL-3.0）的公开逆向结论，本项目为独立 Python 实现，没有引入其源码或链接其库。
新增第三方代码时必须同步更新本表、`tools/check_file_sizes.py` 的排除列表与
[guides/privacy.md](guides/privacy.md) 的许可证说明。

## 7. 数据契约

- 时间戳：实时流是**电脑接收时间**，历史同步是**手环采样时间**，统一为 Unix 秒 + UTC ISO-8601；
- 队列/线程：BLE 回调绝不直接做磁盘 I/O，只入队；
- 落盘格式由 [miband_monitor/storage/schema.py](../miband_monitor/storage/schema.py) 单一来源定义，
  改动前必须考虑既有数据迁移；
- `spo2_sleep` 与点测 `spo2` 是两套数据，禁止合并；字段语义未定部分原样保留。

字段级说明见 [guides/data-formats.md](guides/data-formats.md)。

## 8. 全局限制与设计取舍

- **手环不是通用传感器平台**：只实现已验证的心率/步数/IMU/历史血氧/活动；其它服务不猜测；
- **腕部无 EEG**：睡眠分期是启发式推导，输出带 `derived: true` 语义，不做医学声明；
- **手环缓冲只有约 40 小时**：同步是"抢救数据"，不是长期存储；
- **BLE 行为依平台而变**：BlueZ 需要 trust/配对与专门的待命连接逻辑，Windows/macOS 由系统栈处理；
- **单机、无鉴权**：实时看板默认仅回环地址，不提供多用户或远程部署；
- **零 Web 框架依赖**：HTTP/SSE/图表全部用标准库与手写前端，换取最小依赖面。
