# 安装

## 1. 环境要求

| 项目 | 要求 |
|---|---|
| 操作系统 | Linux（BlueZ）、Windows 10 19041+（WinRT）、macOS 12+（CoreBluetooth） |
| Python | ≥ 3.10（代码使用 `X | Y` 类型注解；开发环境 3.13） |
| 蓝牙 | 主机自带或 USB 蓝牙适配器；WSL2 需 `usbipd` 直通并安装 BlueZ |
| 编译器 | **可选**。Linux 有 `gcc`/`clang` 时自动编译原生 ECDH；没有则纯 Python 回退 |
| 依赖 | `bleak` 会按平台自动安装 WinRT / pyobjc 后端，无需手工处理 |

运行时依赖（[requirements.txt](../../requirements.txt)）：

- `bleak`：BLE 扫描/连接；
- `cryptography`：AES-CBC；
- `python-dotenv`：读取 `.env`；
- `loguru` / `pycryptodome` / `requests`：仅 `get_auth_key.py` / `fetch_by_token.py` 使用；
- `pytest` / `pytest-asyncio`：测试。

## 2. Linux

```bash
# Debian/Ubuntu 示例
sudo apt-get install -y python3-venv python3-pip bluez
sudo systemctl enable --now bluetooth

cd miband_health_monitor
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

WSL2 默认没有蓝牙栈（无 `/sys/class/bluetooth`、无 `bluetooth.service`），
必须把 USB 适配器直通进 WSL，或直接在原生 Linux 上运行。

验证：

```bash
.venv/bin/python -m pytest -q
.venv/bin/python miband_client.py --demo 2 --imu --output data/demo --export-csv
```

## 3. Windows 10/11

推荐从 [python.org](https://www.python.org/downloads/) 安装 Python 3.10+，安装时勾选
"Add python.exe to PATH"。

```powershell
cd miband_health_monitor
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe miband_client.py --demo 2 --imu --output data\demo --export-csv
```

命令行示例统一写 `.venv/bin/python`；Windows 请替换为 `.venv\Scripts\python.exe`。
Windows 不需要 Visual Studio 或 gcc；`bleak` 会自动安装 WinRT 依赖。

## 4. macOS

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python scan_ble.py          # 首次会请求蓝牙权限
```

macOS 不暴露 MAC 地址：扫描结果是 CoreBluetooth UUID，填到 `DEVICE_ID`，
或改用 `DEVICE_NAME` 匹配（见 [configuration.md](configuration.md)）。
系统设置 → 隐私与安全性 → 蓝牙 中需要允许终端/IDE。

## 5. ECDH 原生库（可选）

默认 `auto`：优先加载项目根下当前平台的库，失败则尝试现场编译，再失败回退纯 Python。
两种后端已逐字节交叉校验，功能等价，原生只是更快。

```bash
# Linux
gcc -O3 -shared -fPIC ecdh.c -o libecdh.so
# macOS
clang -O3 -dynamiclib ecdh.c -o libecdh.dylib
# Windows（MinGW-w64）
gcc -O3 -shared -DECDH_BUILD_DLL ecdh.c -o libecdh.dll
# Windows（MSVC 开发者命令行）
cl /nologo /O2 /LD /DECDH_BUILD_DLL ecdh.c /Fe:libecdh.dll
```

也可以 `MIBAND_ECDH_LIB=C:\path\to\libecdh.dll` 指定任意位置。
启动日志显示 `ECDH backend: native` 或 `python`；后者不影响功能。

## 6. 安装后检查

```bash
# 1) 依赖与测试
.venv/bin/python -m pytest -q

# 2) 无硬件全链路
.venv/bin/python miband_client.py --demo 2 --imu --sync-history 1 \
    --output data/demo --storage both --export-csv

# 3) 环境自检（配置/ECDH/输出目录/BlueZ，只读）
.venv/bin/python miband_client.py --doctor
```

demo 应生成 `data/demo/*.jsonl`、`mi_band.db` 与 `csv/`；这两步通过说明
Python、依赖、ECDH 回退、存储与看板链路正常，剩下的只有真实 BLE 链路。

## 7. 下一步

- 配置 `.env`：[configuration.md](configuration.md)
- 真机采集与看板：[usage.md](usage.md)
- 平台差异与配对：[cross-platform.md](cross-platform.md)
- 问题排查：[faq.md](faq.md)
