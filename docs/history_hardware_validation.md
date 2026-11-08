# 真机验证清单：⑦ 实验性血氧（SpO₂）与手环历史数据读取

> 适用版本：包含 `band_history.py` 且 `--sync-history` 可用的版本（git ≥ `28080a9`）。
> 涉及代码：[band_history.py](../band_history.py)、[miband_client.py](../miband_client.py)、
> [docs/../tools/capture_history_payload.py](../tools/capture_history_payload.py)。
> 模拟器侧已全部通过（`tests/test_history.py`）；**本清单的目的就是把“仅模拟器验证”变成“真机验证”**。

---

## 0. 这次验证要回答什么

| # | 问题 | 对应未验证假设 |
|---|---|---|
| Q1 | 手环 6 是否真的存在 `0x0004`/`0x0005` fetch 特征，且具备 notify/write 能力？ | A1 |
| Q2 | “启动 fetch”命令的时间编码（本地时间 6B + 时区 1B）是否正确？ | A2/A3 |
| Q3 | start-date 响应里 `expected_len` 的位置、单位、起始时间解析是否正确？ | A4/A5 |
| Q4 | 数据分片计数器是否从 0 开始、连续递增？ | A6 |
| Q5 | 完成帧是 `10 02 01` 还是带 CRC32 的 7 字节版本？CRC 算法/字节序是否正确？ | A7/A8 |
| Q6 | ACK 是否只需 `[0x03]`，回复是否 `10 03`？ACK 后手环是否删除已传数据？ | A9/A10 |
| Q7 | 数据类型码 `0x01`（活动）/`0x25`（血氧）是否正确？ | A11 |
| Q8 | 活动 8 字节/分钟、血氧 65 字节/条 的字段偏移是否正确？ | A12/A13 |
| Q9 | 解析出的时间是否与 Zepp App 中记录一致（时区/截断误差）？ | A2/A3/A14 |

---

## 0.1 验证结果（2026-11-08，Mi Band 6 / Windows WinRT / MTU 247）

**结论：活动数据与血氧都能真机取到，Q1–Q7、Q9 全部通过；Q8 部分通过。**
两处与「仅模拟器验证」不同的行为已被修正进代码，并补了真机字节级回归用例。

| 现象 | 真机实际行为 | 处理 |
|---|---|---|
| 活动数据长度 | `10 01 01` 里的 u32 对 `ACTIVITY` 是**分钟数**（16 小时窗口报 960），实际收 7680 字节 | 不再按该值截断，改为读到完成帧 `10 02 01` 为止，再按整记录裁剪 |
| 空窗口响应 | `10 01 01 00 00 00 00 b2 07 01 01 00 00 00 00`，时间是 **1970-01-01**；直接 `fromtimestamp()` 在 Windows 抛 `OSError [Errno 22]` | 2000 年之前视为无效，回退到请求窗口；日志改为「没有新数据」而非报错 |
| 分片计数器 | 同一会话内**不重置**，首片可能从 0 以外开始（曾见 4、11） | 首片接受任意起点，后续仍做连续性校验 |
| 完成帧 | 本固件为 3 字节 `10 02 01`，**不带 CRC32** | 保留可选 CRC 校验分支，不带 CRC 时跳过 |
| ACK 后数据 | 仍可按更早窗口再次取到同一批数据（窗口语义，非「ACK 即删除」） | 同步循环按分钟推进窗口，避免同一条记录重复写入 |

实测数据（`--sync-history 2`）：1920 条活动记录，2026-11-06 22:03 → 11-08 14:02
（本地时间），零重复、时间单调，合计 10675 步；血氧 1 条（100%，非自动测量）。
`--sync-history 1` 正确返回空并确认（`10 03 01`），不再异常。

> 2026-11-08 补充：睡眠血氧 `0x26` 也已单独取证通过——12901 B / 430 条，
> 30B 布局与 SpO₂ 字段偏移确认，详见附录 A.1。

---

## 1. 判定标准（先看这个）

### 必须通过（Must）

- [ ] `10 01 01` 开头的 start-date 响应出现，且 `expected_len > 0`（手环确有历史数据时）。
- [ ] 数据分片计数器从 **0** 开始、连续无缺口（见 `History chunk seq=…` 日志）。
- [ ] 完成帧出现，且为 `10 02 01`（或带 4 字节 CRC32 的 7 字节变体）。
- [ ] ACK 发出后有 `10 03` 回复（或仅出现 “No ack response received … (continuing)” 警告）。
- [ ] 血氧解析值在 **50–100** 之间（正常测量通常 90–100），`auto_measure` 标志合理。
- [ ] 活动数据时间戳严格按 **60 秒** 递增，步数 0–255、心率 0 或 30–220。
- [ ] 落盘行数 == 解析样本数：JSONL、SQLite、CSV 三者一致（见 §3）。
- [ ] 历史同步结束后，实时心率/步数仍正常（历史同步不破坏后续会话）。
- [ ] 重复同步（同窗口）要么返回 0 条新数据，要么行为可解释（见 §4）。

### 建议验证（Should）

- [ ] 换 `--days 1` / `--days 7` / `--days 30` 各跑一次，行为随窗口合理变化。
- [ ] 多轮拉取（`rounds=activity:2` 等）能正常工作。
- [ ] 解析出的时间与 Zepp App 中对应记录相差 ≤ 2 分钟（受分钟截断影响）。
- [ ] 抓一次 `btmon` 或 nRF Connect GATT 表，确认特征属性与帧序列。

> 任一项 Must 失败即视为“⑦ 未通过”，请按 §6 收集材料后反馈。

---

## 2. 前置条件（缺一不可）

### 环境

- Linux + BlueZ（WSL2 需 USB 蓝牙直通；`bluetoothctl` 可用）；Windows 用原生系统（WinRT），不要用 WSL2 的蓝牙。
- 项目依赖已安装（`.venv` 或系统环境），`python -m pytest -q` 在无硬件条件下应为 **200 passed**。
- 编译器非必需：有 `gcc`/`clang` 时用原生 ECDH，没有则自动回退纯 Python（日志会打印 `ECDH backend`）。
- 电脑时区与手环时区一致（协议按下述“本地墙上时间”编码）：
  ```bash
  timedatectl    # 确认 Time zone；必要时 sudo timedatectl set-timezone Asia/Shanghai
  ```

### 手环与账号

- 手环电量 ≥ 30%，佩戴中，屏幕可点亮；**手机蓝牙关闭 / 飞行模式**。
- 手环里确实有历史数据：Zepp Life / Zepp App 中能看到最近的活动记录/血氧记录。
- `.env` 中 `DEVICE_ID`（或旧版 `DEVICE_MAC`）、`AUTH_KEY` 已配置且能完成实时连接。
- 先跑一次基线（不涉及 ⑦）：
  ```bash
  .venv/bin/python miband_client.py 20 --storage none
  ```
  必须看到 `Chunked ECDH authentication SUCCEEDED. Session is ready.` 与至少一条心率日志；
  **基线不通过时不要继续 ⑦**，先按 README FAQ 解决连接/认证问题。

---

## 3. 采集流程

### 3.1 标准采集（推荐一次跑全）

```bash
RUN="validation/history-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$RUN"

# 环境快照（出问题时非常有用）
{
  uname -a
  .venv/bin/python -V
  .venv/bin/python -m pip freeze | grep -iE "bleak|cryptography|pytest|dotenv"
  bluetoothctl --version
} > "$RUN/env.txt" 2>&1
git rev-parse HEAD > "$RUN/commit.txt"
.venv/bin/python -m pytest -q > "$RUN/pytest.txt" 2>&1

# 采集：60 秒实时流 + 连接后先同步最近 7 天历史；DEBUG 日志
.venv/bin/python miband_client.py 60 \
    --sync-history 7 \
    --output "$RUN/data" \
    --export-csv \
    --log-level DEBUG 2>&1 | tee "$RUN/run.log"

# 本地历史报告
.venv/bin/python history_report.py --db "$RUN/data/mi_band.db" --hours 168 \
    > "$RUN/report.txt" 2>&1
.venv/bin/python history_report.py --db "$RUN/data/mi_band.db" --hours 168 --json \
    > "$RUN/report.json" 2>&1
```

> `--sync-history` 在**实时流启动之前**执行；日志顺序应为：
> 认证成功 → `Fetching up to 7 day(s) …` → 历史同步 → `Streaming session 1 started`。

### 3.2 原始 payload 采集（强烈建议，失败时几乎必需）

```bash
.venv/bin/python tools/capture_history_payload.py \
    --days 7 --output "$RUN/payloads" 2>&1 | tee "$RUN/payload_capture.log"
```

每个 payload 会保存为 `history_<类型>_<时间>.bin` + 同名 `.json`（类型、since、起始时间、
长度、SHA-256）。**这些 bin 是判断“字段偏移/版本号/CRC”问题的唯一硬证据。**

### 3.3 可选：HCI / GATT 抓包

```bash
# 终端 A（需要 root；在运行 Python 前启动，跑完 Ctrl+C）
sudo btmon -w "$RUN/btmon.btsnoop"
```

并准备一张 nRF Connect（手机）或 `bluetoothctl` 的 GATT 表截图，重点确认
service `0000fee0-0000-1000-8000-00805f9b34fb` 下：

- `00000004-0000-3512-2118-0009af100700`：**notify + write**；
- `00000005-0000-3512-2118-0009af100700`：**notify**。

---

## 4. 分阶段核对（照着日志打勾）

### 阶段 0：连接与认证（基线）

| 期望日志（DEBUG/INFO） | 记录 |
|---|---|
| `Waiting for Mi Band broadcast at …` | 扫描耗时 |
| `Band detected! Establishing BLE link …` | RSSI |
| `BLE link established. Initializing chunked transfer listener...` | — |
| `Chunked ECDH authentication SUCCEEDED. Session is ready.` | 认证耗时 |

### 阶段 1：历史同步启动

| 期望日志 | 记录 |
|---|---|
| `Fetching up to 7 day(s) of band-side history (experimental)...` | 是否出现 |

### 阶段 2：活动数据（type `0x01`，先于血氧）

| # | 期望日志 | 记录/判定 |
|---|---|---|
| 2.1 | `Fetching history type 0x01 since <iso>` | `since` 是否为 7 天前、时区是否本地 |
| 2.2 | `Start-date response: <hex>` | 抄下 hex；应以 `10 01 01` 开头 |
| 2.3 | `Band reports <N> byte(s) of history starting at <iso>` | N 是否为 8 的倍数；起始时间是否≤现在 |
| 2.4 | `History chunk seq=0 (…)`、`seq=1` … | 序号是否从 0 连续；最大 seq 与 N/19 大致匹配 |
| 2.5 | `Fetch completion response: <hex>` | 抄下 hex；是否 `10 02 01`（3B）或 7B（带 CRC） |
| 2.6 | `Ack response: <hex>` | 是否 `10 03`；缺失则记录警告 |
| 2.7 | `Fetched payload OK: N bytes, crc=verified|absent` | CRC 是否 verified |
| 2.8 | `Fetched <K> activity sample(s) up to <iso>` | K 是否 = N/8 |

### 阶段 3：血氧数据（type `0x25`）

| # | 期望日志 | 记录/判定 |
|---|---|---|
| 3.1 | `Fetching history type 0x25 since <iso>` | — |
| 3.2 | `Start-date response: <hex>` | `10 01 01` 开头 |
| 3.3 | `Band reports <N> byte(s) …` | N 是否为 `1 + 65×K` |
| 3.4 | `History chunk seq=…` | 连续性 |
| 3.5 | `Fetch completion response: <hex>` | 抄 hex |
| 3.6 | `Ack response: <hex>` | 抄 hex |
| 3.7 | `Fetched payload OK: N bytes, crc=…` | — |
| 3.8 | `Fetched <M> spo2 sample(s) up to <iso>` | M 是否 = (N−1)/65 |

### 阶段 4：汇总与后续实时流

| 期望日志 | 判定 |
|---|---|
| `Band history sync finished: <K> activity sample(s), <M> SpO2 sample(s), rounds=activity:x,spo2:y` | K/M 与阶段 2/3 一致 |
| 若无新数据：`Band history sync finished: no new history data` | 属正常（刚同步过/无数据） |
| `Streaming session 1 started (duration=60s, imu=False).` | 历史同步未阻塞实时流 |
| `Current Heart Rate: … BPM`、`Steps: …` | 实时流仍正常 |

### 阶段 5：落盘与展示

```bash
ls "$RUN/data"                         # 应有 heart_rate/steps/imu/spo2/activity_history .jsonl + mi_band.db
ls "$RUN/data/csv"                     # 应有 5 个 csv
cat "$RUN/report.txt"
```

| 检查 | 命令 | 期望 |
|---|---|---|
| 血氧表 | `sqlite3 "$RUN/data/mi_band.db" "select count(*),min(spo2),max(spo2),sum(auto_measure) from spo2;"` | 行数 = M；值 50–100 |
| 活动表 | `sqlite3 "$RUN/data/mi_band.db" "select count(*),sum(steps),avg(heart_rate) from activity_history;"` | 行数 = K；步数合理 |
| JSONL | `wc -l "$RUN/data/spo2.jsonl" "$RUN/data/activity_history.jsonl"` | 与表行数一致 |
| 时间间隔 | 见下方脚本 | 活动相邻样本严格 +60s |
| 看板 | 加 `--web-port 8080` 时打开 `http://127.0.0.1:8080` | 血氧卡片/曲线有数据 |

时间间隔自查：

```bash
.venv/bin/python - "$RUN/data/activity_history.jsonl" <<'EOF'
import json, sys
rows = [json.loads(l) for l in open(sys.argv[1], encoding="utf-8") if l.strip()]
rows.sort(key=lambda r: r["ts"])
gaps = [round(b["ts"] - a["ts"], 3) for a, b in zip(rows, rows[1:])]
print("samples:", len(rows), "gaps:", gaps[:10], "unique gaps:", sorted(set(gaps))[:5])
print("first:", rows[0]["iso_time"], "last:", rows[-1]["iso_time"])
EOF
```

期望：`unique gaps` 基本只有 `[60.0]`；最后一条时间 ≤ 当前时间且不早于窗口起点。

---

## 5. 与 Zepp App 对照（时区/截断验证）

1. 在 Zepp Life / Zepp App 的“血氧”“活动/睡眠”记录中找到同一天的数据。
2. 对比本机 `spo2.jsonl` / `activity_history.jsonl` 的时间：
   - 允许偏差 ≤ 2 分钟（协议本身分钟精度）；
   - 若整体相差整小时 → 时区编码问题（重点记录 A2/A3）；
   - 若日期/时刻完全错乱 → 时间字段偏移问题（记录 payload hex）。

---

## 6. 重复同步 / 幂等性验证

```bash
RUN2="$RUN/repeat"; mkdir -p "$RUN2"
# 紧接着再跑一次同样窗口
.venv/bin/python miband_client.py 20 --sync-history 7 \
    --output "$RUN2/data" --log-level DEBUG 2>&1 | tee "$RUN2/run.log"
grep -E "Band history sync finished|no new history data" "$RUN2/run.log"
```

判定：

- 若第二次 `no new history data` / 0 条 → 说明 **ACK 后手环删除已传数据**，行为符合预期；
- 若第二次仍返回相同样本 → 记录“重复返回”现象（可能固件保留数据，或 ACK 未真正生效），
  这不算崩溃，但需在报告中注明；
- 若第二次报错/超时 → 记录错误日志（可能 fetch 状态未复位）。

---

## 7. 已知未验证假设（逐项打勾，最重要）

> 下表就是本清单要证伪/确认的东西。每条通过就打勾，失败就贴对应日志。

- [x] **A1** `0x0004`/`0x0005` 在 Mi Band 6 固件存在，属性分别为 notify+write / notify。
- [x] **A2** 启动命令时间编码 = `年(LE16),月,日,时,分` + `reason(0x00)` + `时区(15min 单位)`。
- [x] **A3** 手环按**分钟**截断 `since`（若固件用秒精度，会出现重复/漏数）。
- [x] **A4** start-date 响应结构 = `10 01 01 | expected_len(u32 LE) | 起始时间(≥6B)`。
- [ ] **A5** ~~`expected_len` 为 payload 字节数~~ —— **仅对血氧成立**（66 = 版本字节 + 65）；
      对活动数据是**分钟数**（16 小时窗口报 960，实收 7680 字节）。已改为以完成帧为准。
- [ ] **A6** ~~计数器从 0 开始~~ —— **同会话内不重置**，首片可能从任意值开始（见 4、11）。
      首片放行，后续仍校验 `+1` 连续性。
- [x] **A7** 完成帧为 `10 02 01`（本固件 3 字节，无 CRC 段）。
- [ ] **A8** 本固件未返回 CRC32，无法验证算法；代码保留可选校验分支。
- [x] **A9** ACK 命令仅 `[0x03]`，回复 `10 03 01`。
- [ ] **A10** ~~ACK 后手环删除已传历史~~ —— **不成立**：按窗口重复拉取仍会返回同一批数据，
      幂等性由客户端按分钟推进窗口保证。
- [x] **A11** 类型码 `0x01`=活动、`0x25`=普通血氧。
- [x] **A12** 活动样本 8B：`kind,intensity,steps,heart_rate,unknown1,sleep,deep_sleep,rem_sleep`
      （字段可解，语义仅 `steps`/`heart_rate` 已确认）。
- [x] **A13** 血氧记录 65B：`int32 LE unix_ts, int8 spo2(负=自动, 值+128), 60B 保留`；版本字节=2。
      真机记录解出 100%（2026-11-07 07:42:07 本地）。
- [x] **A14** 历史同步放在实时流之前不产生副作用（先同步再开流，心率/步数正常）。

---

## 8. 失败速查表

| 现象 / 日志关键字 | 可能原因 | 下一步 | 需要附上的材料 |
|---|---|---|---|
| `timed out waiting for start-date response` | `0x0004` 无 notify 或写被拒；固件用别的特征 | 用 nRF Connect 导出 GATT 表；试 `--days 1`；重连重试 | GATT 截图、`run.log`、btmon |
| `band rejected fetch type 0x25 (status 0x02)` | 固件不支持该类型 / 类型码不同 / 无数据 | 在 App 确认有血氧记录；先只验证活动（可暂用 `--days` 小窗口） | status 值、类型码、App 截图 |
| `Start-date response: …` 前缀不是 `10 01` | 响应格式/特征不同 | 保存完整 hex；对照 `payload_capture` | 完整响应 hex、响应长度 |
| 连续 `Band reports 0 byte(s)` | 无新数据；或 `since` 编码成未来时间 | 检查电脑时区；试 `--days 30`；看 `since` 日志 | `since` 日志、`timedatectl` |
| `packet counter mismatch: got X, expected Y` | BLE 丢包 / 计数器起始不是 0 / 分片长度假设错 | 靠近手环重试；抓 btmon；记录首个 seq | 计数日志前后 10 行、btmon |
| `timed out waiting for activity data chunk` | 分片未开始（时序/需先开某个 notify） | 重试；确认 `10 02 01` 发送后手环状态 | 到超时为止的完整日志 |
| `unexpected completion response` | 完成帧格式不同 | 保存 hex；用 payload 工具确认数据本身是否完整 | 完成帧 hex |
| `payload CRC32 mismatch (expected …, got …)` | CRC 算法/字节序不同，或数据损坏 | 保存 payload bin；分别试大端/小端与 zlib/crc32c 计算 | payload bin + JSON、完成帧 hex |
| `No ack response received from band (continuing)` | 固件不回复 ACK 或需变体 ACK | 记录；观察是否有重复数据 | 后续同步结果 |
| `unsupported SpO2 payload version X` | 版本字节不同（非 2） | 保存 payload，记录 version 与前 32 字节 | payload bin、前 32B hex |
| `SpO2 payload length … not a multiple of 65` | 记录长度不同（字段更多/更少） | 保存 payload，记录长度与 `(len-1) % 65` | payload bin/JSON |
| 数值荒谬（SpO₂ < 50 或 > 100；steps > 255） | 字段偏移/加密/版本不同 | 保存 payload，暂停解析 | payload bin、解析结果 |
| 历史同步后实时心率不来 | 手环 busy / notify 冲突 | 去掉 `--sync-history` 复测；记录会话日志 | 两次 run.log |
| `--web-port` 端口占用 | 端口已被占用 | 用 `--web-port 0` 或换端口 | 错误日志 |

---

## 9. 打包与脱敏（提交前必做）

```bash
cd validation
tar -czf "history-validate-$(date +%Y%m%d-%H%M%S).tar.gz" history-*/
```

分享前：

```bash
# 1) 日志中的 MAC 脱敏
sed -E 's/([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}/XX:XX:XX:XX:XX:XX/g' run.log > run.redacted.log

# 2) 确认没有 auth_key 泄漏（正常代码不会打印它；仍应检查）
grep -inE "auth_key|AUTH_KEY" run.log || echo "no auth key in log"

# 3) .env 永远不要打包/截图
```

- DEBUG 日志包含 ECDH 盐值与公钥（非私密凭据，但可按需删除
  `Received random challenge salt` / `Received band ECDH public key` 两行）。
- `btmon.btsnoop` 含 BLE 地址与空口数据；只在必要时提供。
- 业务数据（心率/血氧/活动）本身也属于个人健康数据，对外反馈时**只贴必要片段**。

---

## 10. 反馈模板（复制填写）

```markdown
## ⑦ 真机验证报告

### 环境
- 手环型号 / 固件版本：
- 主机 / 发行版 / BlueZ / bleak 版本：
- 代码 commit（git rev-parse HEAD）：
- 电脑时区（timedatectl）：
- 手环时区是否一致：是 / 否

### 结论
- [ ] 活动数据（0x01）：PASS / FAIL
- [ ] 血氧数据（0x25）：PASS / FAIL
- [ ] 重复同步：PASS / FAIL / 现象：
- [ ] 实时流未受影响：PASS / FAIL

### 关键日志（DEBUG）
Start-date response (activity)：
Start-date response (spo2)：
Fetch completion response：
Ack response：
Fetched summary 行：

### 数据统计
activity 样本数：   spo2 样本数：   rounds：
sqlite 行数：spo2=   activity_history=
与 Zepp App 对照偏差：

### 附件
- [ ] run.log（已脱敏）
- [ ] payloads/*.bin + *.json
- [ ] report.json
- [ ] GATT 表截图
- [ ] btmon.btsnoop（可选）

### 异常与猜测
```

---

## 11. 验收签署

| 日期 | 手环型号/固件 | 活动 | 血氧 | 重复同步 | 备注 |
|---|---|---|---|---|---|
|  |  |  |  |  |  |

---

## 附录 A：协议常量速查

| 项 | 值 |
|---|---|
| fetch 控制/元数据特征 | `00000004-0000-3512-2118-0009af100700`（notify+write） |
| 活动数据特征 | `00000005-0000-3512-2118-0009af100700`（notify） |
| 命令 | 启动 `0x01`、取数 `0x02`、ACK `0x03` |
| 响应/成功 | `0x10` / `0x01` |
| 类型码 | 活动 `0x01`、普通血氧 `0x25`、睡眠血氧 `0x26` |
| 启动时间编码 | 6B 短日历（年 LE16/月/日/时/分）+ `0x00` + 时区（15min） |
| 血氧 payload | 版本 `2` + N×65B |
| 睡眠血氧 payload | 版本 `2` + N×30B（**2026-11-08 真机验证通过，见附录 A.1**）<br>`int32 LE ts` + `u8 spo2` + `u8 duration` + 6B high + 6B low + 8B signal + 4B spare |
| 活动 payload | N×8B（Mi Band 6） |
| 默认超时/轮数 | 30s / 5 轮 |

### 附录 A.1：睡眠血氧 `0x26` 真机验证结果（2026-11-08，PASS）

```bash
python tools/capture_history_payload.py --days 7 --spo2-sleep \
    --scan-timeout 90 --output validation/payloads-26
```

环境：Mi Band 6 / Windows 11 / WinRT。一次连接同时取回 3 段 ACTIVITY（8640 条）、
1 条 SPO2_NORMAL（66 B）、1 段 SPO2_SLEEP（12901 B），`errors: []`，退出码 0。

| 检查项 | 结果 |
|---|---|
| 固件接受 `0x26` | 是，返回成功状态而非 ignored / rejected |
| payload 长度 | **12901 B = 1 + 430×30**，`(len - 1) % 30 == 0` |
| 版本字节 | `2` |
| 时间戳 | 严格递增，21:39 → 次日 05:39（本地）；主要间隔 60 s（371 次），另有 59/61/120/180/360 s 缺口 |
| SpO₂ 值 | 80–100，均值 95.48，**0 条**越出 50–100 → 偏移 4 与「无符号」读法成立 |
| `high` / `low` | 两组均为 99–100 的采样值，与 Gadgetbridge 命名吻合 |
| `duration` | 集中在 16/32/48/64，**语义未定** |
| `signal` / `extend` | 非零但无固定解释，`extend` 仅 137/430 行非零 |
| 对照 `0x25` | 66 B = 1 + 65，1 条点测记录（100%，07:42） |

结论：**30B 布局与字段偏移通过真机验证**；前两条真机记录已固化为
`tests/test_history.py::test_parse_spo2_sleep_real_hardware_prefix` 字节级回归。
但 `duration` 及三个尾部字节组语义未定，上游 Gadgetbridge 仍标 `TODO save`，
因此保留 `[experimental]` 标记与默认关闭。

**同日转正（先可视化、再独立表）**：`history_dashboard.py` 新增整夜血氧曲线
（优先读表、表空回退 `spo2_sleep.jsonl`），`storage.py` 新增**独立** `spo2_sleep`
SQLite 表、`export_csv.py` 增加同名导出；`tools/import_spo2_sleep.py` 把本附录的
`.bin` 证据按时间戳去重导入两条链路（手环 ACK 后可能清数据，这一步常是唯一取回途径）。
实测导入 430 条后看板显示 min 80 / max 100 / avg 95.5、来源 `sqlite`，
KPI「血氧最低」正确取点测与整夜两种来源的合并最小值。

原始证据 `validation/payloads-26/history_26_*.bin`（SHA-256 `9cd65d3759948167…`）
含个人健康数据，目录已被 `.gitignore` 忽略，对外反馈前按 §9 脱敏，不要打包 `.env`。

## 附录 B：模拟器基线（对照用）

```bash
.venv/bin/python miband_client.py --demo 3 --sync-history 1 --output data/demo --export-csv
```

期望（模拟器固定数据）：

- `Fetched 4 activity sample(s)`、`Fetched 3 spo2 sample(s)`
- `rounds=activity:1,spo2:1`
- DB：`spo2=3`、`activity_history=4`；CSV 5 个文件
- `history_report.py`：`SpO2 min 96 / max 98 / avg 97.0 / auto 1`，`activity 4 minutes, 71 steps`

真机结果与模拟器**结构**应一致；数值和时间不同是正常的。

## 附录 C：日志文案索引（grep 用）

| 关键字 | 含义 |
|---|---|
| `Fetching up to .* day\(s\)` | 历史同步入口 |
| `Fetching history type` | 每种类型开始（含 since） |
| `Start-date response` | 启动响应 hex（关键） |
| `Band reports .* byte\(s\)` | expected_len 与起始时间 |
| `History chunk seq=` | 分片计数器连续性 |
| `Fetch completion response` | 完成帧 hex（关键） |
| `Ack response` | ACK 回复 hex |
| `Fetched payload OK` | 校验结果 |
| `Fetched .* sample\(s\)` | 解析出的样本数 |
| `Band history sync finished` | 汇总 |
| `no new history data` | 无新数据（正常） |
