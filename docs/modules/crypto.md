# crypto — ECDH 双后端

## 职责

为手环的 Chunked ECDH 认证提供 NIST B-163（sect163r2）曲线运算。两条可互换的实现：

- **native**：`ecdh.c` 编译出的 `libecdh.so` / `libecdh.dll` / `libecdh.dylib`，通过 `ctypes` 调用；
- **pure Python**：`vendor/tiny_ecdh/`（MIT）纯 Python 实现，任何平台可用。

两端线格式必须完全一致：24 字节小端私钥、48 字节小端公钥（`x||y`，各 24 字节）、
48 字节小端共享点。两个后端在 Linux 上做了逐字节交叉校验。

源码：[miband_monitor/crypto/](../../miband_monitor/crypto/)

## 模块拆分

| 文件 | 内容 |
|---|---|
| `constants.py` | 库文件名候选、编译参数、曲线/字节长度常量 |
| `native.py` | `load_native_library`、`NativeEcdhBackend`、编译兜底 |
| `pure_python.py` | `PurePythonEcdhBackend`，包装 `vendor/tiny_ecdh/` |
| `facade.py` | 后端选择与对外函数：`select_backend`、`backend_name`、`generate_ecdh_keys`、`compute_shared_secret` |

## 公开 API

| 名称 | 说明 |
|---|---|
| `native_library_candidates() -> list[Path]` | `MIBAND_ECDH_LIB`、项目根下按平台命名的库、构建目录 |
| `compiled_library_path() -> Path` | 现场编译输出位置 |
| `load_native_library() -> CDLL | None` | 加载现有库；失败返回 None（必要时尝试 gcc/clang 编译） |
| `NativeEcdhBackend` / `PurePythonEcdhBackend` | 统一接口 `generate_keys`、`shared_secret` |
| `select_backend(requested=None)` | `auto` / `native` / `python` 选择 |
| `backend_name() -> str` | 启动日志打印的 `native` 或 `python` |
| `generate_ecdh_keys() -> (private, public)` | 48/24 字节线格式 |
| `compute_shared_secret(private, peer_public) -> bytes` | 48 字节共享点 |

## 数据流

```
ble.auth_handshake
   │ generate_ecdh_keys() ──> 发送 48B 公钥
   │ 收到 band 公钥 + 16B 盐
   └ compute_shared_secret(priv, peer) ──> shared[8:24] XOR auth_key ──> AES session key
                                  │
                     crypto.facade 选择 native / python
```

## 依赖

- `miband_monitor.config`（`MIBAND_ECDH_BACKEND`、`MIBAND_ECDH_LIB`、路径）。
- 标准库 `ctypes`、`subprocess`、`pathlib`；native 后端依赖系统 C 编译器（可选）。
- 纯 Python 后端依赖第三方 [vendor/tiny_ecdh/](../../vendor/tiny_ecdh/)。

## 配置与常量

- `MIBAND_ECDH_BACKEND`：`auto`（默认）/ `native` / `python`；
- `MIBAND_ECDH_LIB`：显式指定库文件，便于 Windows 自编译 DLL；
- 库候选顺序：显式路径 → 仓库根平台库 → 现场编译 → 纯 Python 回退；
- 编译命令：Linux `gcc -O3 -shared -fPIC`；macOS `clang -O3 -dynamiclib`；Windows MinGW/MSVC 见
  [guides/cross-platform.md](../guides/cross-platform.md)。

## 失败模式

| 情况 | 行为 |
|---|---|
| 无 `libecdh.*` | 自动回退纯 Python，日志 `ECDH backend: python` |
| `MIBAND_ECDH_BACKEND=native` 但库不可用 | 启动报错，要求安装/编译或改回 auto/python |
| 库 ABI/函数名不匹配 | `load_native_library` 拒绝加载并回退（auto 模式） |
| 编译失败 | 记录 stderr 摘要，回退纯 Python |
| 公钥长度/点非法 | 抛 `ValueError`，认证失败并重连，不会静默产生错误密钥 |
| 库路径包含用户目录 | `config.privacy.SENSITIVE_ENV_KEYS` 将其视为敏感值，日志脱敏 |

## 测试

- `tests/test_ecdh_backends.py`：两种后端逐字节交叉校验、后端选择、各平台库文件名、
  强制 native 时的错误路径。Linux CI 同时覆盖 native 与 `MIBAND_ECDH_BACKEND=python`。
- `tests/test_miband.py`（重构后 `tests/test_crypto_parsers.py`）：AES 测试向量与密钥格式。

## 相关文档

- [modules/ble.md](ble.md)（认证状态机如何使用 ECDH）
- [guides/cross-platform.md](../guides/cross-platform.md)
- [architecture.md](../architecture.md#6-第三方与兼容豁免)
