# config — 配置、路径与隐私常量

## 职责

集中所有"随环境变化"的值，避免默认值散落在各领域模块里：

- 默认值/超时/端口/MTU 边界/重连参数；
- `.env` 与进程环境变量的读取、布尔/整数/浮点解析；
- 项目路径（包根、仓库根、默认 data/db/csv/.env 位置）；
- 手机号/地址/密钥的脱敏与日志清洗；
- 文件与目录权限（0600/0700）；
- 生理与物理常量（心率/血氧合法区间、标准重力）；
- 演示模式使用的假设备身份。

源码：[miband_monitor/config/](../../miband_monitor/config/)

## 模块拆分

| 文件 | 内容 |
|---|---|
| [defaults.py](../../miband_monitor/config/defaults.py) | 环境变量名常量、默认超时、重连退避、MTU 边界、看板默认值 |
| [env.py](../../miband_monitor/config/env.py) | `load_env`、`secure_env_file`、`env_str/float/int/optional_int/flag`、`read_env_file`、`update_env_file` |
| [paths.py](../../miband_monitor/config/paths.py) | `PACKAGE_ROOT`、`PROJECT_ROOT`、`default_data_dir`、`default_db_path`、`default_csv_dir`、`default_env_path`、ECDH 源码/构建目录 |
| [privacy.py](../../miband_monitor/config/privacy.py) | `SENSITIVE_ENV_KEYS`、`mask_secret`、`mask_address`、`redact_log_value` |
| [security.py](../../miband_monitor/config/security.py) | `DIRECTORY_MODE=0o700`、`FILE_MODE=0o600`、`secure_path`、`secure_directory`、`secure_file` |
| [health.py](../../miband_monitor/config/health.py) | `HR_UNAVAILABLE=255`、`HR_MIN/HR_MAX`、`SPO2_MIN/SPO2_MAX`、`STANDARD_GRAVITY` |
| [demo.py](../../miband_monitor/config/demo.py) | `DEMO_DEVICE_ID`、`DEMO_DEVICE_NAME`、`DEMO_AUTH_KEY_HEX/BYTES` |

## 公开 API

包级 `__init__` 重导出上述名称（`__all__` 为所有非下划线全局名），因此
`from miband_monitor.config import DEFAULT_SCAN_TIMEOUT` 与
`from miband_monitor.config.defaults import ...` 等价。常用：

| 名称 | 类型 | 说明 |
|---|---|---|
| `DEFAULT_SCAN_TIMEOUT` | float | 90 s，等待首个广播脉冲 |
| `DEFAULT_CONNECT_TIMEOUT` | float | 75 s；Linux 会按 70 s 广播周期抬升 |
| `DEFAULT_DIRECT_CONNECT_TIMEOUT` | float | 10 s，BlueZ 直连快路径预算 |
| `DEFAULT_AUTH_TIMEOUT` | float | 30 s，chunked 认证总预算 |
| `DEFAULT_MTU_MIN/MAX` | int | 23 / 517 |
| `DEFAULT_WEB_PORT/HOST` | int/str | 端口默认关闭，地址 `127.0.0.1` |
| `DEFAULT_MAX_POINTS/SNAPSHOT_POINTS` | int | 看板内存序列上限 |
| `env_flag(name, default)` | func | 识别 `1/true/yes/on` 与 `0/false/no/off` |
| `secure_env_file()` | func | 把工作目录 `.env` 收紧到 0600（失败仅记录，不中断） |
| `mask_address()` / `mask_secret()` | func | 日志与终端输出脱敏 |

## 数据流

```
进程环境 / .env ──load_env()──> os.environ ──env_*()──> 各模块参数
                      └──secure_env_file()──> chmod 0600
defaults.py ──> cli/args.py 的 argparse 默认值
           └─> bluez/ble/history/dashboard 的超时与上限
paths.py   ──> storage 的 db/jsonl/csv 路径、auth 的 .env 路径、crypto 的构建目录
privacy.py ──> 日志、doctor 输出、auth 终端输出
```

## 依赖

- 仅标准库（`os`、`pathlib`、`typing`）；不导入任何其它 `miband_monitor` 领域包。
- `env.py` 可选导入 `python-dotenv`；缺失时退化为只读进程环境，不报错。

## 配置与常量

环境变量名单一来源是 [defaults.py](../../miband_monitor/config/defaults.py)：
`DEVICE_ID`、`DEVICE_MAC`、`DEVICE_NAME`、`AUTH_KEY`、`BLE_MTU`、`OUTPUT_DIR`、
`STORAGE_BACKEND`、`SCAN_TIMEOUT`、`CONNECT_TIMEOUT`、`LOG_LEVEL`、`DEMO_MODE`、
`WEB_PORT`、`WEB_HOST`、`HISTORY_DAYS`、`MIBAND_ECDH_BACKEND`、`MIBAND_ECDH_LIB`、
`MIBAND_BLUEZ_TRUST`、`MIBAND_BLUEZ_AGENT`、`MIBAND_BLUEZ_DUPLICATE_DATA`、
`MIBAND_SMP_SETTLE_DELAY`。完整含义与默认值见
[guides/configuration.md](../guides/configuration.md)。

## 失败模式

| 情况 | 行为 |
|---|---|
| `.env` 不存在 | `load_env` 静默跳过，只用进程环境与默认值 |
| `.env` 权限过宽 | `secure_env_file` 尝试 chmod 0600；无权限时记录 warning，不中断启动 |
| 环境变量不是合法数字 | `env_int/env_float` 记录 warning 并回退默认值 |
| 未知布尔写法 | 按默认值处理，不抛异常 |
| 输出目录不可创建 | 由 `storage` 捕获并报错；config 只负责给路径 |
| 包被移动/以 zip 导入 | `PACKAGE_ROOT` 基于 `__file__`；安装方式建议正常解包 |

## 测试

- 环境变量解析、`DEVICE_MAC` 兼容与占位符：`tests/test_device_matching.py`
  （重构后位于 `tests/bluez/`、`tests/history/` 等拆分目录）。
- 隐私/权限相关断言散见于 `tests/test_storage.py`（`secure_path`）与
  `tests/test_bluez_support.py`（`.env` 处理）。
- CI 额外执行 `tools/check_privacy.py`，确保仓库不出现真实地址/密钥/路径。

## 相关文档

- [guides/configuration.md](../guides/configuration.md)
- [guides/privacy.md](../guides/privacy.md)
- [architecture.md](../architecture.md)
