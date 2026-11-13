# 隐私与数据安全

本项目会产生并落盘大量个人数据。仓库只应保存代码与脱敏文档；真实设备标识、
凭据与健康数据一律留在本机并被 git 忽略。

## 1. 数据分级

| 内容 | 位置 | 敏感级别 | git 状态 |
|---|---|---|---|
| 设备 MAC / auth_key | `.env` | **高**（配对凭据） | 已忽略，权限 0600 |
| 实时/历史健康数据 | `data/*.jsonl`、`data/mi_band.db`、`data/csv/` | **高**（个人健康数据） | 已忽略 |
| 真机验证材料 | `validation/`（run.log、payloads、btmon） | **高**（MAC / 数据 / 空口包） | 已忽略 |
| 调试日志与抓包 | `*.log`、`*.btsnoop`、`*.pcap` | **高** | 已忽略 |
| 导出的 HTML 看板 | `dashboard_history.html` 等 | **高**（内嵌健康数据） | 需自行管理 |
| 代码与文档 | 仓库根、`docs/` | 否（示例为占位符） | 跟踪 |

## 2. 凭据与 .env

- `.env` 被 [.gitignore](../../.gitignore) 忽略；程序启动时 `secure_env_file()`
  会尝试收紧到 **0600**；
- `AUTH_KEY` 等价于手环蓝牙配对凭据，视同密码：不要粘贴到 issue、聊天或截图；
- `get_auth_key.py` / `fetch_by_token.py` 只把结果写入本地 `.env`，终端仅显示掩码状态；
- 多用户 Linux 主机上确认 `ls -l .env` 是 `-rw-------`；`--doctor` 会检查权限；
- 不要把 `.env` 放进 Docker 镜像、备份脚本的公开目录或云盘共享目录。

## 3. 权限规则

| 路径 | POSIX 权限 | 说明 |
|---|---|---|
| 输出目录 `data/` | 0700 | `secure_directory` 设置 |
| JSONL / SQLite / CSV / HTML | 0600 | `secure_file` / `secure_path` 设置 |
| `.env` | 0600 | 每次启动尝试收紧 |
| `validation/`（取证） | 自行保证 | 已被忽略，仍建议 0700 |

Windows 使用用户 ACL；`0600` 语义不适用，但不要把数据目录放在共享位置。

## 4. 永远不会提交的内容

[.gitignore](../../.gitignore) 覆盖但不限于：

- `.env`、`.env.*`（保留 `.env.example`）、`secrets/`、`*.pem/*.key/*.pfx`；
- `data/`、`exports/`、`*.jsonl`、`*.csv`、`*.db*`、`*.sqlite*`；
- `validation/`、`payloads/`、`captures/`、`*.btsnoop`、`*.pcap`、`*.log`；
- 本地构建的原生库（`libecdh.dll/dylib`；Linux `libecdh.so` 有意跟踪）。

提交前自检：

```bash
.venv/bin/python tools/check_privacy.py     # CI 同款：真实 MAC/密钥/路径
git status --short                          # 确认没有 data/、.env、validation/
```

## 5. 对外反馈与分享

分享日志、payload、看板截图前：

1. 日志中的 MAC 脱敏：
   ```bash
   sed -E 's/([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}/XX:XX:XX:XX:XX:XX/g' run.log > run.redacted.log
   ```
2. 确认没有 auth_key / token：
   ```bash
   grep -inE "auth_key|AUTH_KEY|app_token|password" run.log || echo "clean"
   ```
3. DEBUG 日志含 ECDH 盐值与公钥（非私密凭据，但可按需删除
   `Received random challenge salt` / `Received band ECDH public key` 两行）；
4. 健康数据只贴必要片段；`.env`、`data/`、抓包文件永远不要打包或截图；
5. 文档中的设备地址统一使用占位符（如 `C0:FF:EE:C0:FF:EE`），日期使用占位日期。

详细验证报告模板见 [validation/history-hardware.md](../validation/history-hardware.md) §9–§10。

## 6. 清理本地数据

```bash
# 谨慎：以下命令会永久删除本地健康数据，请先备份需要的文件
rm -f data/*.jsonl data/mi_band.db data/mi_band.db-wal data/mi_band.db-shm
rm -rf data/csv data/demo
rm -rf validation payloads captures          # 取证材料
rm -f *.log *.btsnoop *.pcap
```

注意：

- 删除数据库后不可恢复（手环只剩约 40 小时历史）；
- 离线看板 HTML、CSV 导出可能被复制到别处，需单独清理；
- 撤销 Linux 配对：`bluetoothctl remove <MAC>`（需要时）。

## 7. 第三方与许可证

- `huami_token/`（MIT）、`vendor/tiny_ecdh/`（MIT）、`ecdh.c/ecdh.h` 与
  `libecdh.so` 为第三方/上游代码，保持原样；
- 第三方代码清单与豁免原因见 [architecture.md](../architecture.md#6-第三方与兼容豁免)；
- 本项目整体以 MIT License 发布，见 [LICENSE](../../LICENSE)。

## 8. 安全边界

- 实时看板/HTTP API **无鉴权**，默认只绑定 `127.0.0.1`；不要直接暴露公网；
- BLE 认证密钥只用于与手环的本地链路；云调用仅发生在主动运行
  `get_auth_key.py` / `fetch_by_token.py` 时；
- 本项目不做数据上传；所有分析（睡眠、报告、看板）都在本机完成。
