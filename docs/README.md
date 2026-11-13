# 文档中心

这里是 Mi Band 6 Health Monitor 的全部文档入口。仓库根目录的
[README](../README.md) 只保留功能概览、快速开始与导航；具体细节按下面的分类查阅。

## 阅读路线

| 你的目标 | 从这里开始 |
|---|---|
| 第一次安装并跑起来 | [guides/installation.md](guides/installation.md) → [guides/configuration.md](guides/configuration.md) → [guides/usage.md](guides/usage.md) |
| 只是连不上手环 | [guides/cross-platform.md](guides/cross-platform.md) → [guides/faq.md](guides/faq.md) → [validation/linux-ble-connect-findings.md](validation/linux-ble-connect-findings.md) |
| 想理解代码结构 | [architecture.md](architecture.md) → [modules/](modules/) 逐个模块 |
| 要改协议 / 加新数据流 | [architecture.md](architecture.md) + [modules/ble.md](modules/ble.md) + [modules/history.md](modules/history.md) |
| 处理数据 / 做分析 | [guides/data-formats.md](guides/data-formats.md) + [modules/storage.md](modules/storage.md) + [modules/sleep.md](modules/sleep.md) |
| 想知道睡眠字段结论 | [investigations/sleep-fields.md](investigations/sleep-fields.md) |
| 提交代码 / 写测试 | [guides/testing.md](guides/testing.md) + [guides/privacy.md](guides/privacy.md) |

## 架构与模块

| 文档 | 内容 |
|---|---|
| [architecture.md](architecture.md) | 分层架构、依赖方向、运行时数据流、第三方代码豁免、兼容门面与全局限制 |
| [modules/config.md](modules/config.md) | 默认值、环境变量、路径、隐私与权限 |
| [modules/crypto.md](modules/crypto.md) | ECDH 原生 / 纯 Python 双后端与库加载回退 |
| [modules/bluez.md](modules/bluez.md) | Linux D-Bus 设备发现、待命建链、配对 agent、MTU |
| [modules/ble.md](modules/ble.md) | GATT 协议、chunked 认证、实时数据流与客户端生命周期 |
| [modules/history.md](modules/history.md) | 手环历史 fetch 协议、三种 payload 解析与同步循环 |
| [modules/storage.md](modules/storage.md) | JSONL/SQLite schema、后台写线程、CSV 导出 |
| [modules/dashboard.md](modules/dashboard.md) | 实时看板 hub、HTTP/SSE API 与前端资源 |
| [modules/sleep.md](modules/sleep.md) | 睡眠窗口检测、深睡/浅睡推导与报告 |
| [modules/offline-dashboard.md](modules/offline-dashboard.md) | 离线单文件 HTML 历史看板 |
| [modules/simulator.md](modules/simulator.md) | 内存模拟手环与假 bleak 后端 |
| [modules/cli.md](modules/cli.md) | 命令行参数、会话监督、回调接线 |
| [modules/diagnostics.md](modules/diagnostics.md) | `--doctor` 环境自检与扫描排障 |
| [modules/reports.md](modules/reports.md) | 本地 SQLite 汇总报告 |
| [modules/auth.md](modules/auth.md) | 账号获取 auth_key 的交互流程 |
| [modules/tools.md](modules/tools.md) | `tools/` 诊断、取证与校验脚本 |

## 使用指南

| 文档 | 内容 |
|---|---|
| [guides/installation.md](guides/installation.md) | Linux / Windows / macOS 安装、依赖、ECDH 编译（可选） |
| [guides/configuration.md](guides/configuration.md) | `.env` 全量变量、CLI 参数、优先级与自检 |
| [guides/usage.md](guides/usage.md) | 实时采集、看板、历史同步、导出、报告、睡眠、离线看板 |
| [guides/data-formats.md](guides/data-formats.md) | 数据目录、JSONL/SQLite/CSV 字段、时间戳语义 |
| [guides/cross-platform.md](guides/cross-platform.md) | 三平台差异、Linux 配对/trust、Windows/macOS 验证清单 |
| [guides/testing.md](guides/testing.md) | 测试布局、CI 矩阵、demo 冒烟、文档/隐私/体积校验 |
| [guides/faq.md](guides/faq.md) | 常见报错、扫描/连接/认证/历史/看板问题 |
| [guides/privacy.md](guides/privacy.md) | 数据分级、0600/忽略规则、对外反馈脱敏、清理 |

## 验证与调查（证据存档）

这些文档保留原始实验记录、日志片段与结论；正文除脱敏与链接外不做改写。

| 文档 | 内容 |
|---|---|
| [validation/history-hardware.md](validation/history-hardware.md) | 血氧 / 活动 / 睡眠血氧 `0x26` 的真机验证清单与结果 |
| [validation/linux-validation.md](validation/linux-validation.md) | Ubuntu/BlueZ 软件链路 + 真机验证报告（L1–L10） |
| [validation/linux-ble-connect-findings.md](validation/linux-ble-connect-findings.md) | 2026-11-12 Linux 连接不稳定专项排查与根因清单 |
| [investigations/sleep-fields.md](investigations/sleep-fields.md) | 睡眠三字节字段取证、与 Zepp Life / Gadgetbridge 的对照 |

## 工具与规范

- 工具说明与脚本清单：[../tools/README.md](../tools/README.md)
- 配置模板：[../.env.example](../.env.example)
- 许可与第三方：[../LICENSE](../LICENSE)、[architecture.md#6-第三方与兼容豁免](architecture.md#6-第三方与兼容豁免)

> 文档中的设备地址一律为占位符（例如 `C0:FF:EE:C0:FF:EE`），日期统一为占位日期；
> 若发现链接失效或内容过期，先跑 `python tools/check_docs_links.py` 再提 issue。
