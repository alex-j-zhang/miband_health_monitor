# Linux 验证报告（Ubuntu 24.04 / BlueZ）

> 验证日期：2026-11-08（两轮：软件链路 + 真机） · 被测提交：`4f4d63b`（`main`）
> 环境：Ubuntu 24.04.5 LTS，内核 6.8.0-147，x86-64；Python 3.13.13（conda 与 `python3 -m venv`）；
> BlueZ 5.72，USB 适配器 `hci0`，systemd `bluetooth` 服务 active。
> 设备：Mi Smart Band 6（地址已脱敏）。
> **本次只做验证与记录，未修改任何代码。** 诊断用的 BlueZ agent 脚本放在 `/tmp`，不属于仓库。

## 1. 结论速览

| 层次 | 结果 |
|---|---|
| 单元/集成测试（无硬件） | ✅ 216 passed / 4 skipped（原生与纯 Python 两种 ECDH 后端各跑一遍） |
| demo 全链路（采集→存储→CSV→看板） | ✅ 通过，产物齐全 |
| 真实 BLE 扫描 | ✅ 可用（非 root、无需 `bluetooth` 组） |
| 真机连接 + Chunked ECDH 认证 | ✅ 成功（未配对/已配对均可完成认证） |
| 真机心率 / 步数 / IMU 流 | ⚠️ 步数与 IMU ✅；心率在已配对的一轮里 **0 条通知**（待复测） |
| 真机历史同步 | ✅ 1188 条活动样本 + 1 条 SpO2（`--sync-history 1`） |
| 真机长会话稳定性 | ⚠️ 未配对：约 36 s 断链；**配对后：300 s 完整跑满** |
| 实时看板接真机数据 | ✅ `/api/snapshot`、`/api/history`、`/events` 均有真实数据 |
| 记录的问题 | 10 条（L1–L10）+ 5 条备注（N1–N5）；其中 **L9/L10 已在 P0 中修复（§7）** |

**一句话**：软件层在 Linux 可用；真机可用，但前提是**先在 BlueZ 侧完成配对**，
否则连接会在约 36 秒后被手环断开（L9）。这正好对应原报告的 L7 风险。

## 2. 通过的验证项（软件链路，第一轮）

| # | 项目 | 命令 / 方法 | 结果 |
|---|---|---|---|
| 1 | 测试套件（原生 ECDH） | `python3 -m pytest -q` | 216 passed / 4 skipped |
| 2 | 测试套件（纯 Python ECDH） | `MIBAND_ECDH_BACKEND=python python3 -m pytest -q` | 216 passed |
| 3 | 按文档走 venv 安装 | `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt` | 安装成功，pytest/demo 均通过 |
| 4 | CI 全链路 | `miband_client.py --demo 2 --imu --sync-history 1 --sync-sleep-spo2 --storage both --export-csv` | 7 个必需产物全部非空 |
| 5 | 真实 BLE 扫描 | `scan_ble.py` / `scan_live.py` | 可用；手环广播时能发现 |
| 6 | 扫描超时 | `--device-id C0:FF:EE:C0:FF:EE --scan-timeout 3` | 报错清晰，exit 1 |
| 7 | Ctrl+C | demo 运行中 / 扫描中发送 SIGINT | 优雅退出，`Storage summary` 正常 |
| 8 | 存储后端 | `--storage jsonl / sqlite / both / none` | 四种均正确 |
| 9 | 非 ASCII + 空格路径 | `--output "/tmp/数据 目录"` | 正常写入 |
| 10 | 看板 HTTP | `/`、`/api/snapshot`、`/api/history`（含 `source=db`）、`/events`(SSE)、`/healthz` | 全部 200 |
| 11 | 端口占用 | 两次绑定 `18080` | 明确报错，不崩溃 |
| 12 | 离线看板/报告/导出 | `history_dashboard.py`、`history_report.py`、`export_csv.py`、`sleep_analysis.py` | 正常 |
| 13 | 0x26 载荷导入 | `tools/import_spo2_sleep.py`（合成 91B 载荷） | 3 条样本写入 JSONL+SQLite |
| 14 | ECDH 三级回退 | native → 现场 `gcc` 编译 → 纯 Python | 三级均可用，共享密钥一致 |
| 15 | 环境变量 | `SCAN_TIMEOUT`、`BLE_MTU`、`LOG_LEVEL`、`STORAGE_BACKEND`、`DEMO_MODE` | 均生效 |
| 16 | 参数校验 | `--sync-sleep-spo2` 缺 `--sync-history` | argparse 拒绝，exit 2 |
| 17 | 反向兼容检查 | 无 `fcntl/termios/resource` 等 POSIX-only 依赖；CSV 使用 `newline=""` | 不阻碍 Windows |

## 3. 真机验证（第二轮）

### 3.1 成功了什么

| 项目 | 观测 |
|---|---|
| 发现手环 | `Band detected! Establishing BLE link`；广播间断出现，95 s 扫描窗口内捕获 |
| 连接 | `BLE link established`（MTU 日志恒为 23，见 L10） |
| Chunked ECDH 认证 | `Chunked ECDH authentication SUCCEEDED. Session is ready.` |
| 心率流 | 未配对的一轮：70–72 BPM 正常上报（9–16 条） |
| 步数流 | 正常：`Steps: 8666 | Distance: 5877 m | Calories: 213 kcal` |
| IMU 流 | 正常：一轮采集到 689 条样本 |
| 历史同步 | `1188 activity sample(s), 1 SpO2 sample(s), 0 sleep SpO2` |
| 实时看板 | `/api/snapshot` 返回真实 spo2/imu；`/events` SSE 推送真实数据 |
| CSV 导出 | 6 张表全部生成（heart_rate 16、imu 689、activity_history 1188 等） |
| 长会话 | 配对后 **300 s 完整跑满**：`Collection window (300s) elapsed` |

### 3.2 断链的定位过程

未配对时的两次会话，都在**开始采集后约 36 秒**终止：

```
12:40:28 [ERROR] Heart-rate keep-alive failed; treating link as lost:
  [org.freedesktop.DBus.Error.UnknownObject] Method "WriteValue" ... doesn't exist
```

- BlueZ 日志给出根因：`src/device.c:new_auth() No agent available for request type 2`
  与 `device_confirm_passkey: Operation not permitted` —— 手环发起配对，系统没有 agent。
- 断开后 BlueZ 直接移除设备对象（`bluetoothctl info` → `not available`），
  于是后续写特征命中 `UnknownObject`；应用把它当作链路丢失，行为正确但报错文案有误导性。
- 对照实验：用 D-Bus 注册 `NoInputNoOutput` agent 后重连，
  agent 收到 `RequestAuthorization ... -> accept`，设备变为 `Paired: yes`，
  同一命令 **300 秒不再掉线**。

结论：**L9 成立**——Linux 上不先配对，连接会被手环在约 36 秒后断开。

### 3.3 待复测

已配对的那一轮（300 s）心率通知为 **0 条**，而步数每 60 s 正常上报；
此前未配对的两轮心率正常。差异可能来自手环佩戴状态（光学心率在离腕时停止），
也可能与配对后特征权限有关，**目前不能归因**，需要在佩戴状态下复测。

## 4. 问题清单（已记录，**未修复**）

### L1 · `.env` 实际权限 0644，与文档声称的 0600 不符
- **现象**：`ls -l .env` → `-rw-r--r--`；README 第 805 行承诺"忽略，权限 0600"。
- **原因**：代码只在**写入** `.env` 时 `chmod 0600`（`get_auth_key.py:67`、`fetch_by_token.py:49`），读取时不检查也不收紧。
- **影响**：Linux 多用户主机上 `AUTH_KEY` 可被同机其他用户读取。
- **平台相关性**：是（Windows ACL 下"0600"不成立，故在 Windows 上不可见）。

### L2 · 遥测产物权限过大（0664 / 0644）
- **现象**：`heart_rate.jsonl` 为 `-rw-rw-r--`，`mi_band.db` 为 `-rw-r--r--`。
- **影响**：个人健康数据在共享目录下可读，与仓库的隐私定位不一致。
- **平台相关性**：是（Windows 默认按用户 ACL）。

### L3 · 输出目录不可写/被同名文件占用 → 未捕获异常 + 原始 traceback
- **现象**：只读目录 → `PermissionError`；`--output` 指向文件 → `FileExistsError`；
  两者都直接打印 Python 堆栈，exit 1。
- **平台相关性**：跨平台，Linux 更易触发。
- **建议方向**：捕获 `OSError` 并给出可操作提示。

### L4 · `history_dashboard.py` 对不存在的 DB 静默成功
- **现象**：生成 22 KB 空看板、exit 0，并创建 0 字节 DB；与 `history_report.py`/`export_csv.py`（exit 1）不一致。

### L5 · 跨平台文档与事实不符
- `docs/cross_platform.md:3` 称"最初在 Linux（BlueZ）上开发"；`:21` 称 Linux"已长期使用"。
- `:106` 称 Windows 冒烟"预期：88 passed"，当前为 **216 passed**。
- **说明**：本轮验证确认 Linux 软件链路可用，但"长期使用"仍不成立；文档应改为"已验证软件链路 + 真机需先配对（见 L9）"。

### L6 · `tools/` 三个脚本的用法示例写死 Windows venv 路径
- `tools/probe_fetch_types.py:9`、`probe_sleep_session.py:29`、`calibrate_sleep.py:22` 均为 `./.venv/Scripts/python.exe`。

### L7 · Linux 配对（bonding）环节缺失 → **本轮已被真机证实会断链**（见 3.2）
- 代码与文档都没有 BlueZ agent / 配对引导；Windows 上系统"添加设备"可能已完成配对，从而掩盖差异。
- **影响**：不配对时连接约 36 s 后断开；配对后 300 s 稳定。
- **建议方向**：文档补 `bluetoothctl` 配对步骤（最低成本），或内置 agent（需权衡权限与安全）。

### L8 · 探针脚本超时参数不一致
- `probe_fetch_types.py` 无 `--scan-timeout`，无手环时按默认 90 s 扫描后还可能重连，实测 60 s 未退出。

### L9 · **【高】未配对时手环在约 36 秒后断链，并触发误导性 D-Bus 报错** —— ✅ 已修复（见 §7）
- **现象**：两次未配对会话均在 ~36 s 结束；日志为 `UnknownObject: Method "WriteValue" ... doesn't exist`。
- **根因**：BlueZ `No agent available for request type 2` → `device_confirm_passkey: Operation not permitted`；
  断链后 BlueZ 移除设备对象，后续特征写入命中已消失的对象。
- **对照**：注册 agent 完成配对后，同一命令 300 s 不掉线（见 3.2）。
- **影响**：Linux 新用户按文档直接运行会反复遇到"采集几十秒就停"，且错误信息指向不存在的方法，难以自查。
- **修复**：文档补齐配对/trust 步骤；D-Bus 错误翻译为可操作提示（§7）。

### L10 · **【中】ATT MTU 在 Linux 上从未真正协商** —— ✅ 已修复（见 §7）
- **现象**：每次连接都打印
  `UserWarning: Using default MTU value. Call _acquire_mtu() or set _mtu_size first`，
  且启动日志恒为 `MTU=23`。
- **原因**：`miband_client._resolve_mtu()` 只读取 `client.mtu_size`；bleak 3.0.2 的 BlueZ 后端
  必须显式调用 `_acquire_mtu()` 才会填充该值，否则返回 23。WinRT/macOS 后端会在连接时自动填充。
- **影响**：分块写按 20 字节载荷发送，认证与历史同步的分块数量成倍增加（本轮 1188 条历史仍能同步完成，但更慢，
  弱信号下更容易触发认证超时）。
- **平台相关性**：是，且属于"Windows 正常、Linux 悄悄降级"的典型差异。
- **修复**：连接后显式 `_acquire_mtu()`；实测 MTU 23 → **247**（§7）。

## 5. 备注（非缺陷，避免误判）

- **N1 · 预编译库仅覆盖 x86-64/glibc**：`libecdh.so` 为 ELF x86-64；ARM/musl 上会先现场 `gcc` 编译，失败再纯 Python。该回退链已验证。
- **N2 · 日志时间为 UTC**：本机 `TZ=Etc/UTC`。
- **N3 · `scan_live.py` 管道下无输出**：Python 缓冲，`-u` 或 TTY 下正常。
- **N4 · 失败后没有 "Reconnecting" 日志**：当 `duration` 已耗尽时，`run_monitor` 会静默 `return`（deadline 判断）。
  属预期行为，但缺少"采集窗口已结束"的收尾日志，容易误判为重连失效。
- **N5 · 手环广播是间断的**：息屏后基本不广播；`scan_ble.py` 默认 8 s 窗口经常扫不到，
  需要更长的 `--scan-timeout` 或 `scan_live.py` 连续扫描。

## 6. 复现命令摘要

```bash
# 软件链路
python3 -m pytest -q
python3 miband_client.py --demo 2 --imu --sync-history 1 --sync-sleep-spo2 \
  --output /tmp/ci-demo --storage both --export-csv

# 真机（先配对，见 L9）
bluetoothctl pairable on
python3 /tmp/bt_agent.py 900 &          # 诊断用 agent（不属仓库）
python3 miband_client.py 300 --scan-timeout 180 --storage both --imu

# 看板
python3 miband_client.py --demo 15 --web-port 0
curl -s http://127.0.0.1:<port>/healthz
```

> 报告中设备地址均为占位符；`data/`、`.venv/`、`.cache/` 均被 `.gitignore` 覆盖。
> 本轮为对照实验配对过一次，如需撤销：`bluetoothctl remove <MAC>`。

---

## 7. P0 修复与复验（2026-11-09）

针对 L9/L10 以及"Linux 连接难"的体感，做了四项改动，全部只在 Linux 路径生效，
Windows/macOS 行为不变。

| 改动 | 落点 | 复验结果 |
|---|---|---|
| 配对引导 + 环境自检 | `docs/cross_platform.md` §2.3/§2.5、`doctor.py`、`--doctor` | 自检能指出"未配对/不可配对/.env 权限"，并给出可复制的命令 |
| 直连快路径（跳过扫描） | `bluez_support.paired_device()`、`MiBand6Client._open_client()` | 日志 `Bonded band ... skipping discovery`，**未发生扫描** |
| ATT MTU 协商 | `bluez_support.try_acquire_mtu()`、`_apply_mtu()` | `MTU=23` → **`MTU=247`**，`UserWarning` 消失 |
| D-Bus 错误翻译 | `bluez_support.describe_ble_error()` | `UnknownObject` 被翻译为"未配对/已断开"+ 修复命令 |

真机复验日志（节选）：

```
22:31:17 [INFO] Bonded band C0:FF:EE:C0:FF:EE found in the BlueZ object tree; skipping discovery.
22:31:26 [INFO] BLE link established. Initializing chunked transfer listener (MTU=247)...
22:31:26 [INFO] Negotiated ATT MTU via BlueZ: 247 bytes.
22:31:28 [INFO] Chunked ECDH authentication SUCCEEDED. Session is ready.
```

补充说明：

- 直连失败（手环不在范围/休眠）时会在 12 秒内回退到扫描，不会占用完整连接超时；
- `--no-direct-connect` 可强制回到扫描路径，便于排查"缓存对象过期"；
- 新增测试 `tests/test_bluez_support.py`（26 项），全套 **242 passed / 4 skipped**；
- L9 的"配对后 300 秒稳定"与 L10 的"MTU 247"均已在本机真机复现验证。
