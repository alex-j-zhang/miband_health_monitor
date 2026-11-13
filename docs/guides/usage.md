# 使用

所有命令从仓库根目录运行；示例用 `.venv/bin/python`，Windows 写成
`.venv\Scripts\python.exe`。配置项含义见 [configuration.md](configuration.md)。

## 1. 先做无硬件全链路

```bash
.venv/bin/python miband_client.py --demo 2 --imu --sync-history 1 \
    --output data/demo --export-csv
```

这一条覆盖认证、三类实时流、历史同步、JSONL/SQLite 落盘、CSV 导出。
产物：`data/demo/heart_rate.jsonl` 等、`mi_band.db`、`data/demo/csv/`。
不需要蓝牙硬件，Windows/macOS 也可用来做冒烟测试。

## 2. 真机采集

```bash
# 环境自检（适配器、配对、ECDH、.env 权限；不连接手环）
.venv/bin/python miband_client.py --doctor

# 采集 60 秒心率 + 步数
.venv/bin/python miband_client.py 60

# 采集 60 秒并附加 25Hz IMU
.venv/bin/python miband_client.py 60 --imu

# 一直采集到 Ctrl+C，同时写 SQLite，结束后导出 CSV
.venv/bin/python miband_client.py --export-csv

# 只看日志不落盘
.venv/bin/python miband_client.py --storage none
```

真机要点：

- **关闭手机蓝牙 / 开飞行模式**，否则手环被手机占连，不广播；
- 手环每 **40–60 秒**才发一个孤立可连接广播脉冲，熄屏时完全不发；
  **点亮/滑动屏幕不会让它广播更快**（2026-11-12 实测），只影响建链后的连接间隔，
  因此 `--scan-timeout 120`；
- Linux 首次使用先 `bluetoothctl trust <MAC>`（见 [cross-platform.md](cross-platform.md)）；
- 心率传感器在自动重连后会自动重新开启；
- 启动日志前两行（`ECDH backend`、`Discovery target`）反馈问题时请一并附上。

## 3. 实时看板（HTTP API + SSE）

```bash
.venv/bin/python miband_client.py --web-port 8080             # 真机
.venv/bin/python miband_client.py --demo 30 --web-port 8080   # 无硬件演示
```

浏览器打开 `http://127.0.0.1:8080`（`--web-host 0.0.0.0` 可局域网访问，注意安全）。
页面用 Canvas 绘制心率/血氧/步数/IMU 合加速度趋势，SSE 实时推送，无需刷新。

| 端点 | 说明 |
|---|---|
| `GET /` | 单页看板 |
| `GET /api/snapshot` | 最新值 + 最近序列 + 计数（JSON） |
| `GET /api/history?kind=heart_rate&limit=600` | 单指标内存序列；加 `&source=db` 读 SQLite |
| `GET /events` | SSE 事件流（先发 snapshot，再持续推送 sample） |
| `GET /healthz` | 存活探针 |

历史回填：页面加载时并行请求四条 `?source=db` 序列，再打开 SSE；每个 snapshot
（含 SSE 首帧）都会把持久化点与实时点按时间戳合并去重。数据库不存在、缺表或
`--storage jsonl|none` 时静默降级为纯实时视图。DB 查询走线程池，不阻塞 BLE。

## 4. 历史同步（实验性）

小米手环 6 的血氧不是实时通知，而是历史记录；活动数据按分钟保存：

```bash
# 连接后先拉取最近 7 天历史，再进入实时采集
.venv/bin/python miband_client.py 60 --sync-history 7

# 只做历史同步（Ctrl+C 结束）
.venv/bin/python miband_client.py --sync-history 3

# 额外抓取 0x26 整夜血氧（写入独立 spo2_sleep 表 + JSONL）
.venv/bin/python miband_client.py 60 --sync-history 7 --sync-sleep-spo2
```

协议要点（依据 Gadgetbridge 公开逆向结果独立实现）：

- 控制/元数据特征 `0x0004`，数据分片 `0x0005`；
- 写 `[0x01, 数据类型] + 本地时间(6B) + [原因, 时区]` 启动；活动 `0x01`、血氧 `0x25`；
- 手环先回 `10 01 01 <计数 u32> <起始时间>`，再写 `[0x02]` 请求数据；
- 分片带递增计数（每次拉取**不重置**），完成后回 `10 02 01`，客户端回 `[0x03]` 确认；
- 血氧记录：版本 2 + 每条 65 字节（int32 LE 时间、1 字节 SpO₂、60 字节保留），
  负值表示自动测量（真实值 = 原始值 + 128）；
- 活动记录：每分钟 8 字节 `kind,intensity,steps,heart_rate,unknown1,sleep,deep_sleep,rem_sleep`。

### 真机暴露的两个协议陷阱

1. **`<计数 u32>` 对活动数据是"分钟数"，不是字节数**：请求 16 小时窗口报 960，
   实际传 7680 字节。客户端改为读到完成帧 `10 02 01` 为止，再按整记录裁剪。
2. **窗口内无新数据时手环回填 1970-01-01**：直接 `fromtimestamp()` 在 Windows 上会抛
   `OSError [Errno 22]`。客户端把 2000 年之前视为无效并回退到请求窗口。

> ✅ 真机验证（Mi Band 6 / Windows WinRT / MTU 247）：2 天窗口一次拿到 1920 条活动
> （零重复、时间单调、合计 10675 步），血氧 1 条；1 天窗口正确返回空并正常确认。
> 逐项清单见 [validation/history-hardware.md](../validation/history-hardware.md)。

### 手环只保留约 40 小时

手环分钟数据是一块 **2400 分钟（40 小时）**的滑动窗口，每分钟写入挤掉最老一行；
App 取走并回执的时段会留下**永久空洞**。因此 `--sync-history 7` 实际最多只能拿到
40 小时。想留住某一夜，必须在那夜结束后 40 小时内同步（稳妥做法：每天同步）。

只问不取、不扰动缓冲的探针：

```bash
.venv/bin/python tools/probe_declared.py 168 24
```

### 睡眠血氧 0x26（独立表）

```bash
# 取证：原始 payload 存成 .bin + .json（含 SHA-256）
.venv/bin/python tools/capture_history_payload.py --days 7 --spo2-sleep --output validation/payloads

# 把取证 .bin 导入正式数据链路（手环 ACK 后可能清数据，这常是唯一取回途径）
.venv/bin/python tools/import_spo2_sleep.py --output data validation/payloads/history_26_*.bin
```

布局：版本 2 + N×30B（`int32 LE ts / u8 spo2 / u8 duration / 6B high / 6B low /
8B signal / 4B spare`）。真机验证：12901 B = 1 + 430×30，SpO₂ 80–100、均值 95.5。
`duration`、`signal`、`extend` 语义未定，因此仍标 `[experimental]` 且默认不请求；
样本进入 `spo2_sleep` 独立表/JSONL/CSV，绝不混入点测 `spo2`。

## 5. 本地报告与睡眠推导

```bash
.venv/bin/python history_report.py --hours 24       # 最近 24 小时文本报告
.venv/bin/python history_report.py --hours 168 --json

.venv/bin/python sleep_analysis.py --db data/mi_band.db            # 睡眠文本报告
.venv/bin/python sleep_analysis.py --db data/mi_band.db --json     # 结构化输出
.venv/bin/python sleep_analysis.py --db data/mi_band.db --night 2020-01-07

# 对照工具：四种候选读法并排，可用夜 <2 时拒绝拟合
.venv/bin/python tools/calibrate_sleep.py --zepp data/zepp_nights.json
```

### 5.1 三个字节是评分，不是阶段时长

活动历史里的 `sleep` / `deep_sleep` / `rem_sleep` **不是各阶段分钟数**。三种候选读法
都拿真实的 Zepp 数字对过表（01-06 夜：深睡 **44 分钟 = 8%**）：

| 读法 | 深睡 | 结论 |
| --- | --- | --- |
| `分数 / 128` 积分 | 96 分钟（27%） | 差 52 分钟，且把 REM 算成 82 分钟（那一夜 App 报 0） |
| Gadgetbridge 阈值（REM>55 / 深睡>42） | 128 分钟（35%） | 差 84 分钟 |
| 心率排名启发式 | 50 分钟（11%） | 最接近，采用 |

证据是**倒挂**的：App 报 REM 0 分钟的 01-06 夜，`rem_sleep` 458/485 分钟非零；
App 报 REM 87 分钟的 01-07 夜，`rem_sleep` 396/396 分钟全零。不存在任何单调缩放
能让两列同时成立——它们的语义是"睡眠窗口内、有心率时算出的判别分数"。
全库统计也支持：`rem_sleep` 非零的 463 分钟全部有心率；有心率但字节为 0 的 755
分钟全部落在白天非睡眠段。因此本项目**不输出 REM**，三列原样留在库里作证据。

### 5.2 六夜 Zepp 对照（原始记录见 data/zepp_nights.json）

| 日期 | 得分 | 卧床 | 睡眠 | 深睡 | 浅睡 | REM | 清醒 | 手环有这一夜 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 01-01 | 75（胜 38%） | 23:06 → 07:36 | 7h57 | 1h02（13%） | 6h55（87%） | 无此栏 | 33 | 没有 |
| 01-02 | 86（胜 71%） | 22:07 → 06:35 | 8h27 | 58（11%） | 7h29（89%） | 无此栏 | 1 | 没有 |
| 01-03 | 88（胜 78%） | 21:45 → 06:34 | 8h40 | 1h08（13%） | 7h32（87%） | 无此栏 | 9 | 没有 |
| 01-05 | 75（胜 38%） | 21:16 → 05:42 | 8h05 | 44（9%） | 7h21（91%） | 无此栏 | 21 | 没有 |
| 01-06 | 73（胜 34%） | 21:06 → 06:06 | 8h40 | 44（8%） | 7h56（92%） | 无此栏 | 20 | **有**（缺一小时） |
| 01-07 | 92（胜 90%） | 22:44 → 05:11 | 6h27 | 1h14（19%） | 3h46（58%） | **1h27（23%）** | 0 | 没有（未佩戴） |

三个结论：

1. **REM 只出现在"不是手环"的那一夜**：手环佩戴的五夜全都没有 REM 栏；01-07 那页
   来自同账号下另一台设备（手环 679 分钟 `0xF3`、0 条心率）。
2. **"深睡固定比例"被排除**：六夜深睡 62/58/68/44/44/74 分钟，占睡眠 8%–19%，
   既不是常数也不是固定百分比。
3. **得分主要惩罚觉醒**：与清醒分钟 `r = -0.87`、与睡眠效率 `+0.86`、与睡眠时长
   `-0.47`。分数不能拿来当校验基准。

按重叠区间对表（01-06）：我们卧床 470 分钟/睡眠 453/清醒 17/深睡 50（11%）；
Zepp 按同区间折算约 480/460/20/44（8%）。反推出的三条规则就是实现：
**清醒按步数、窗口按锚点 + 静止延伸、不输出 REM**；深睡两个阈值只有一夜样本，
属于临时值，等更多 Zepp 夜再收敛。

### 5.3 与 Gadgetbridge 交叉核对

| 问题 | 结论 |
| --- | --- |
| 8 字节布局 | `FetchActivityOperation.createExtendedSample()` 逐字段同名同序，手环 6 的样本大小就是 8 |
| 三字节是不是时长 | Gadgetbridge 也**不**当时长：`& 127` 取分数后与阈值比较（REM > 55、深睡 > 42），作者自注 "thresholds are arbitrary … not accurate" |
| Zepp 的时长从哪来 | 另一条命令 `SLEEP_SESSION (0x48)`，594 字节/条，含分期段列表与睡眠得分 |
| 手环 6 能拿到吗 | **不能**。`supportsSleepScore()` 默认 false 且 `MiBand6Coordinator` 未覆盖；真机实测 `0x48` 状态成功但 0 条记录 |

完整取证、Zepp 逐页原文与结论修正过程见
[investigations/sleep-fields.md](../investigations/sleep-fields.md)。

### 5.4 未佩戴与样本补充

`kind` 的低四位才是活动码，`3` = **未佩戴**（Gadgetbridge `TYPE_NONWEAR`）。
真机上 `0xF3` 的 679 分钟**一条心率都没有**（其余 kind 约 100% 有心率），且全部零强度
零步数——所以 01-07 不是"卧床 11.3 小时"，而是手环没戴。未佩戴夜不生成睡眠段，
也不进看板"最近一晚"。

补样本只要三步：**戴着睡 → 当天同步 → 当天把 Zepp 数字记进 `data/zepp_nights.json`**。
第二步是硬前提：手环收到 `03` ack 后会清掉已上传的分钟。可用夜少于两夜时
`tools/calibrate_sleep.py` 会拒绝拟合，只列差值。

## 6. 离线历史看板（单文件 HTML）

```bash
.venv/bin/python history_dashboard.py --db data/mi_band.db \
    -o dashboard_history.html --days 7

# 只有 JSONL、还没导入数据库时，可显式指定整夜血氧来源
.venv/bin/python history_dashboard.py --db data/mi_band.db \
    -o dashboard_history.html --spo2-sleep-jsonl validation/payloads-26/spo2_sleep.jsonl
```

内容：KPI（最近一晚、深睡占比、睡眠效率、步数、心率区间、血氧最低）、睡眠结构 SVG、
心率曲线、按天活动节律、每日步数、整夜睡眠血氧曲线、点测血氧表。无 CDN/字体/图片，
可直接双击打开、发邮件或存档。

三个刻意设计：活动节律条按真实时间戳定位（手环历史不是连续网格）；心率缺失夜晚画
卧床灰带而不是空泳道；整夜血氧（0x26）与点测血氧（0x25）分成两节、超过 5 分钟空档断线。

## 7. CSV 导出

```bash
.venv/bin/python export_csv.py --db data/mi_band.db --output data/csv
```

`--export-csv` 会在会话结束时自动做同样的事；导出 `heart_rate`、`steps`、`imu`、
`spo2`、`activity_history`、`spo2_sleep` 六张表，UTF-8、表头为列名。
字段与目录说明见 [data-formats.md](data-formats.md)。

## 8. 排障脚本

```bash
.venv/bin/python scan_ble.py      # 8–90 秒扫描列表（macOS 打印 UUID）
.venv/bin/python scan_live.py     # 连续事件扫描（抓孤立广播脉冲）
```

更多诊断工具见 [../tools/README.md](../../tools/README.md)，常见报错见 [faq.md](faq.md)。

## 9. Windows 示例

```powershell
.venv\Scripts\python.exe miband_client.py --demo 2 --imu --output data\demo --export-csv
.venv\Scripts\python.exe miband_client.py 60
.venv\Scripts\python.exe miband_client.py --web-port 8080
.venv\Scripts\python.exe history_report.py --hours 24
.venv\Scripts\python.exe scan_ble.py
```
