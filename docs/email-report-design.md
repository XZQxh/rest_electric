# 设计稿：每日电费邮件日报（含 14 天图表）

状态：**已实现**（2026-10-23 定稿）　　初稿：2026-10-23

> 实现：`src/rest_electric/mail.py`、`chart.py`、`usage.py`；
> 入口 `python -m rest_electric report --email`；
> 定时器 `scripts/systemd/rest-electric-daily.{service,timer}`。

---

## 1. 目标与非目标

**目标**

- 每天 **23:00** 把「最近 14 天」的电费情况发到邮箱，正文内嵌两张图；
- 余额趋势 + 每日消耗，**充值日一眼可见**；
- 全部用标准库实现，不引入 matplotlib / Pillow；
- 发送失败可重试、同一天不重复发。

**非目标**

- 不做交互（回复邮件查询之类）；
- 不做 QQ 机器人（另一份设计）；
- **不附带 CSV / JSON 附件**（用户已定：不带）；
- 不改现有 `once` / `report` 的行为，只给 `report` 增加 `--email` / `--force` 开关；
- 不在 Windows 上跑邮件定时任务——生产在树莓派，Windows 只做开发。

---

## 2. 复用的现有能力

| 能力 | 位置 | 复用方式 |
|---|---|---|
| 每天最后一条 | `store.daily_points()` | 余额折线、明细表 |
| 区间读数 | `store.readings_since()` | **新增**，充值识别需要 2 小时粒度的读数 |
| 纯文本报表 | `report.render()` | `text/plain` 原样复用，老客户端兜底 |
| 每日真实消耗 | `usage.daily_usage()` | 逐区间识别充值，算出「消耗 + 充值」 |
| 预测 | `forecast.estimate()` | 日均、剩余天数 |
| SMTP | `notify.SmtpNotifier.send_message()` | **新增方法**，发送已组装好的 HTML 邮件 |
| 告警记录 | `store.recent_alerts()` | 邮件末尾最近 3 条 |

---

## 3. 图表方案：为什么手写 PNG

邮件客户端对图形的支持很挑：

| 方案 | 结果 |
|---|---|
| SVG 内嵌 | Gmail / QQ 邮箱 / Outlook **都不渲染** → 否决 |
| `<img src="data:...">` | Gmail 会拦截 data URI；QQ 邮箱行为不稳定 → 否决 |
| 外部图表服务（quickchart 之类） | 把用电数据发给第三方，且依赖外网 → 否决 |
| HTML/CSS 柱状图 | 柱子勉强能做，**折线做不了** → 只适合做备选 |
| matplotlib / Pillow | 破坏「零第三方依赖」，树莓派要装科学计算栈 → 否决 |
| **手写 PNG（zlib + struct）+ CID 内嵌** | 所有客户端都认，零依赖 → **采用** |

代价是要自己画：像素缓冲、Bresenham 画线、CRC32、一块 5×7 点阵字体。这恰好是标准库能覆盖的范围。

### 3.1 PNG 编码

- 8 位真彩（color type 2）+ 无交错，扫描线前缀 filter=0；
- `zlib.compress()` 压 IDAT，`zlib.crc32()` 算块校验；
- 只写 IHDR / IDAT / IEND 三个块，足够所有客户端识别。

### 3.2 画布与抗锯齿

- 逻辑尺寸 **1000×360**，先按 2 倍渲染（2000×720）再 2×2 平均降采样；
- 配色：背景 `#ffffff`、网格 `#e5e7eb`、坐标轴 `#9ca3af`、主线 `#2563eb`、柱子 `#60a5fa`、充值绿 `#10b981`。

### 3.3 文字：只用 5×7 点阵数字

- 图里**不写中文**（没有字库），中文全部放在邮件 HTML 正文里；
- 图内只画数字刻度，用 5×7 点阵字（`0-9 . - : /`），放大 2 倍；
- 这样避免几百 KB 的字体文件，也避免中文乱码。

### 3.4 两张图

1. **余额趋势**：`chart.line_chart(points, recharge_days=...)`，折线 + 淡蓝面积填充，
   **充值日的点标绿**；
2. **每日消耗**：`chart.bar_chart(day_usages)`，柱子是 `usage.daily_usage()` 算出的
   **真实消耗**（已把当天充值加回去），充值日在柱顶画绿点并标金额，如 `+10`。

Y 轴 5 条刻度，X 轴按天取 5–7 个刻度（`10-17` 这种）。

### 3.5 数据不足时

- 少于 2 个数据点：不画图，正文写「数据不足（至少需要两天的读数），明天起才会有趋势图」；
- 某天缺数据：柱状图保留这一天（消耗按小时摊分），折线只连有读数的天；
- 图表生成抛异常：捕获后**整封降级为纯文本**，traceback 写日志（宁可少两张图，不能发不出去）。

---

## 4. 充值识别（`usage.daily_usage`）

为什么不能直接用「今天最后一条 − 昨天最后一条」：

> 充值会让余额上升。如果某天充值金额小于当天消耗，日终余额仍然是下降的，
> 这个差值就变成「消耗 − 充值」，把用电量算少了，而且看不出来。

有 2 小时粒度的读数时，逐区间判断：

1. **余额上升 = 那个区间里发生了充值**；
2. 充值金额 ≈ 上升幅度 + 该区间内本来会消耗掉的部分（按整体中位速率估算），
   再按「充值一般是整数元」四舍五入；
3. 这段区间的消耗**仍然计入当天用电**（否则会凭空少一截）；
4. 跨午夜区间按小时数把消耗摊到两天；充值归到**后一条读数**所在的日期；
5. 同一天多次充值合并成一个金额。

已知边界：

- 充值额小于该区间的消耗时，余额整体仍在下降，检测不到——但 2 小时消耗通常只有
  0.1–0.2 元，现实中不会有人充这么小的金额；
- 一天多次充值会被合并；
- 速率用中位数，抗单次异常读数。

---

## 5. 邮件结构

```
multipart/related
├─ multipart/alternative
│  ├─ text/plain        ← report.render() 的原样输出，老客户端兜底
│  └─ text/html         ← 正文 + <img src="cid:chart-balance"> + <img src="cid:chart-usage">
├─ image/png            ← Content-ID: <chart-balance>
└─ image/png            ← Content-ID: <chart-usage>
```

用 CID 而不是 data URI，QQ 邮箱、Gmail、Outlook 都能内嵌显示。
（少数客户端会退化成「两个图片附件」，正文仍然是完整的，可以接受。）

无图时（数据不足）根节点就是 `multipart/alternative`；图表异常时是纯 `text/plain`。

---

## 6. 正文内容

**标题**（已定稿）：

```
电费日报 2026-10-23 · 余额 22.87 元 · 约 12 天
```

- 今天没有读数：前缀 `⚠️`，如 `⚠️ 电费日报 2026-10-23 · 余额 22.87 元 · 约 12 天`；
- 完全没有读数：`⚠️ 电费日报 2026-10-23 · 今日无数据`；
- 没有预测（样本不足）时省略「约 N 天」；不足 1 天写「不足 1 天」。

**正文分区**

| 区块 | 内容 |
|---|---|
| 概览 | 当前余额、今日用电（截至最后一次采集）、较昨日 ±、预计可用天数 / 日均 |
| 图 1 | 余额趋势（默认 14 天，充值日绿点） |
| 图 2 | 每日用电（默认 14 天，充值日绿点 + 金额） |
| 明细表 | 最近 7 天：日期 / 余额 / 用电 / **充值**（充值日绿色 `+10.00`） |
| 告警 | 最近 3 条（余额低、登录失败、采集端离线等） |
| 页脚 | 房间、数据来源（epay）、最后读数时间、生成时间 |

样式全部用**内联 style**：QQ 邮箱会清洗 `<style>` 块，类选择器不可靠。

---

## 7. 代码接口与命令行为

```python
# mail.py
def subject_of(today, points, estimate) -> str
def build_daily_email(config, points, estimate, alerts=None, *,
                      day_usages=None, days=14, last_reading=None) -> EmailMessage
def send_daily_email(config, message) -> None

# store.py（新增）
def readings_since(conn, room_key, since: date) -> list[Reading]

# notify.py（新增）
def smtp_notifier(config, *, to="") -> SmtpNotifier
SmtpNotifier.send_message(message)     # send() 改成组装纯文本后调用它
```

`report --email` 的行为：

1. 读库：`daily_points`（余额/明细）+ `readings_since`（充值识别）；
2. `usage.daily_usage()` → 每日真实消耗与充值；
3. 生成两张 PNG（内存里，不落盘）→ 组装 MIME；
4. 用 `[notify].smtp_*` 发送（**与 `[notify].channel` 无关**，Server酱 继续负责突发告警）；
5. 成功后写 `meta.daily_report_sent`（日期）与 `meta.daily_report_sent_at`（时间）。

退出码：`0` 成功或跳过；`2` SMTP 发送失败；`3` 配置错误（缺 SMTP / 收件人）。

`--force`：忽略 `[report].enabled=false` 与去重，强制发送（人工重发用）。

---

## 8. 配置

```toml
[report]
enabled = true
chart_days = 14          # 图与明细默认看多少天
to = ""                  # 收件人；留空则复用 [notify].smtp_to
```

> `[report]` 整段可以省略：默认 `enabled = true`、`chart_days = 14`、`to` 回退到 `smtp_to`。

SMTP 复用现有 `[notify]` 配置（在 `secrets.toml`）：

```toml
[notify]
channel = "serverchan"   # 突发告警仍走 Server酱，和日报互不影响
smtp_host = "smtp.qq.com"
smtp_port = 465
smtp_user = "你的QQ号@qq.com"
smtp_password = "授权码（不是登录密码）"
smtp_to = "收件邮箱"
```

> QQ 邮箱要在「设置 → 账户」里开 SMTP 并生成**授权码**；465 端口是 SSL。
> 收件人优先级：`[report].to` > `[notify].smtp_to`。

---

## 9. 调度

树莓派新增两个单元：

- `rest-electric-daily.service`：`Type=oneshot`，执行 `python3 -m rest_electric report --email`；
- `rest-electric-daily.timer`：每天 **23:00 / 23:20 / 23:40** 三个 `OnCalendar`。

设计取舍：

- **失败重试**：23:00 失败后 23:20、23:40 各重试一次；成功后写去重标记，
  后两次触发会直接跳过。三次都失败则不再重试，等第二天；
- **去重窗口**：`meta.daily_report_sent` 记录日期、`daily_report_sent_at` 记录时间；
  同一天且距上次成功 **3 小时内**跳过。这样既能覆盖 20/40 分钟后的重试，
  又不会让白天手动发过之后晚上的定时任务被误跳过；
- **不做开机补发**（`Persistent=false`）：避免第二天早上补发一封、晚上又发一封；
- `scripts/deploy-pi.sh` 只在 SMTP / 收件人配置齐全时启用该 timer，否则给出警告
  并保持停用，避免每晚失败刷 journal。

---

## 10. 失败模式

| 现象 | 处理 |
|---|---|
| SMTP 认证失败 / 网络错误 | 日志 + 退出码 2；若 `[notify].channel` 是 Server酱/PushPlus，补推一条「日报发送失败」 |
| 缺 SMTP 配置或收件人 | 启动即报人话，退出码 3；deploy 脚本也不会启用 timer |
| 当天没有读数 | 照发，标题加 `⚠️`，正文提示采集端可能离线 |
| 数据点 < 2 | 不画图，正文提示明天才有趋势 |
| 图表生成异常 | 捕获后发纯文本邮件（不含图），日志记 traceback |
| 重复发送 | 3 小时去重窗口；`--force` 可强制重发 |
| 邮件太大 | 两张 PNG 合计约 20–40 KB，整封 < 150 KB，远低于常见限制 |

---

## 11. 测试

- `tests/test_mail.py`（11 个）：`email.parser` 反解 MIME 结构、两个 CID、PNG 签名、
  HTML 里两张图、空数据 ⚠️、单点不画图、图表异常降级纯文本、假 SMTP 发送、
  收件人回退、缺配置报 `ConfigError`；
- `tests/test_report_email.py`（6 个）：CLI 首次发送、去重、3 小时窗口过期、`--force`、
  `[report].chart_days` 生效、`enabled=false`、SMTP 失败退出码 2、缺 SMTP 返回退出码 3；
- `tests/test_config.py`（新增 4 个）：`[report]` 缺省值、显式值、收件人回退、`chart_days` 下限；
- `tests/test_store.py`（新增 1 个）：`readings_since()` 按本地日期过滤；
- 假 SMTP 用 `tests/_fake_smtp.py`，不联网；全套 **118 个用例**全绿。

---

## 12. 已拍板的事（原「需要你拍板的四件事」）

1. **每天几点发？** → **23:00**（失败 23:20 / 23:40 重试）。
2. **图看多少天？** → **14 天**，可用 `[report].chart_days` 改（最小 2）。
3. **要不要附 CSV？** → **不带**。
4. **Server酱 还留着吗？** → **留**：日报走邮件，突发告警继续走微信。
5. **主题格式？** → `电费日报 日期 · 余额 X 元 · 约 N 天`。
6. **纯文本版？** → 与 `report.render()` **完全一致**。
7. **文字报表加「充值」列？** → **不加**，避免改变现有 `report` 输出；充值只在邮件明细里。

---

## 13. 风险与后续

1. 点阵字体只覆盖数字，图里出现中文就会乱——设计上已规避；
2. 个别邮件客户端把 CID 图当附件（正文仍可读）；
3. QQ 邮箱对 HTML 有清洗，样式必须内联；
4. 树莓派上第一次启用前，务必手动跑一次
   `python3 -m rest_electric report --email --force`，确认能收到邮件；
5. 如果以后想改发送时间，只需改 timer 的 `OnCalendar`；去重逻辑不依赖具体钟点。
