# 交接文档：宿舍电费监控（2026-10-23）

> 给"新开一个窗口接着干"的 agent 看。读完这份 + `README.md` + `docs/epay-web.md`，
> 就能接手，不需要再问用户背景。

---

## 0. 一句话

Windows 上做开发，树莓派上跑生产。采集链路已经完全跑通（走学校网页版，不依赖微信），
**每天 23:00 发一封带 14 天图表的电费邮件日报**已经在代码里完成（`mail.py` + `report --email`
+ systemd timer + 全套测试），剩下的是树莓派上验证与提交。

---

## 1. 谁在跑、数据在哪

| | 位置 | 状态 |
|---|---|---|
| **生产** | 树莓派 `/opt/rest_electric` | `rest-electric.timer` 白天 08:07–22:07 每 2 小时 + 0–10 分钟抖动；`rest-electric-daily.timer` 每天 23:00 发邮件日报；systemd 常驻 |
| 生产数据库 | 树莓派 `/opt/rest_electric/data/electric.db` | 由 `rest-electric.timer` 持续写入（交接时 9 条） |
| **开发** | 这台 Windows `D:\DSHProject\rest_electric` | 代码与文档在这里改 |
| 开发数据库 | `data/electric.db` | 8 条，最后一条 2026-10-22T20:13:49 23.00 元；**只用于本地测试** |

**重要：Windows 上那 8 个计划任务已经 `Disabled`**（交接时停的）。
不要重新启用，也不要在 Windows 上手动跑 `once`——两边同时跑会重复写库、并发 CAS 登录可能触发学校的失败次数限制。

---

## 2. 数据怎么来的（认证链路）

`config.toml` 里 `[api].mode = "epay"`，走 `epay.EpayClient`：

```
GET https://shoucard.shou.edu.cn/epay/h5/eleresult?sysid=1&roomid=<房间>&areaid=1&buildid=1
  ├─ 已登录：200，HTML 里有「剩余电量 23.51度」
  └─ 未登录：302 → uis.shou.edu.cn/cas/login?service=<epay 回调>
       ├─ 取登录页里的 hidden execution
       ├─ GET /cas/jwt/publicKey（RSA-2048）
       ├─ POST /cas/login：username + "__RSA__" + base64(RSA(pwd)) + execution，无验证码
       └─ 302 → /epay/j_spring_cas_security_check?ticket=… → 302 回结果页
```

- Cookie 缓存在 `data/epay-cookies.txt`，只有失效才重新登录；
- 学校 **1 度 = 1 元**，按"元"入库，阈值/预测口径不变；
- 备用通道：`mode="http"`（小程序，2 小时 token，实际已弃用）、`serve-ingest`（手机读屏上报，备用）。

---

## 3. 每日邮件日报（代码已完成，待 Pi 验证）

**用户已拍板**：每天 **23:00** 发；图 **14 天**；**不带 CSV**；**保留 Server酱**（日报走邮件，突发告警继续走微信）；
主题用 `电费日报 日期 · 余额 X 元 · 约 N 天`；纯文本版与 `report.render()` 完全一致；
失败在 23:20 / 23:40 重试；文字报表**不加**「充值」列。

### 已完成

| 文件 | 内容 | 状态 |
|---|---|---|
| `src/rest_electric/chart.py` | 手写 PNG：折线图、柱状图、5×7 点阵数字、2 倍超采样、`demo_readings()` 造数 | ✅ 定稿 |
| `src/rest_electric/usage.py` | `daily_usage()`：逐区间识别充值，算出每天"真实消耗 + 充值额" | ✅ 定稿 |
| `src/rest_electric/mail.py` | 主题/HTML/明细表 + MIME 组装（CID 内嵌两张 PNG）+ SMTP 发送 | ✅ |
| `src/rest_electric/store.py` | 新增 `readings_since()`（充值识别要区间读数） | ✅ |
| `src/rest_electric/notify.py` | `SmtpNotifier.send_message()`、`smtp_notifier()` | ✅ |
| `src/rest_electric/config.py` | `[report] enabled / chart_days / to`；`report_recipient()` 回退 `smtp_to` | ✅ |
| `src/rest_electric/cli.py` | `report --email` / `--force`；3 小时去重（`meta.daily_report_sent_at`） | ✅ |
| `scripts/systemd/rest-electric-daily.{service,timer}` | 23:00 + 23:20 + 23:40 | ✅ |
| `scripts/deploy-pi.sh` | 安装新单元；SMTP 配齐才启用 timer | ✅ |
| `tests/test_mail.py`、`test_report_email.py`、`test_config.py`、`test_store.py` | MIME、假 SMTP、空数据降级、CLI 去重/退出码、`readings_since` | ✅ |

### 待做（只剩验证与提交）

1. 树莓派上 `sudo bash scripts/deploy-pi.sh`，确认 daily timer 被启用；
2. 填 `config/secrets.toml` 的 `[notify].smtp_*`（QQ 邮箱授权码）与 `config.toml` 的 `[report].to`；
3. 手动发一次：`sudo -u restelec PYTHONPATH=src python3 -m rest_electric report --email --force`；
4. 看邮箱和 `journalctl -u rest-electric-daily.service -n 50`；
5. 把新文件提交（见 §8）。

> 设计稿 `docs/email-report-design.md` 已经按定稿更新（23:00、充值识别、图表定稿、无 CSV）。

---

## 4. 已定稿的设计细节（别再改）

**图**

- 纯标准库手写 PNG（`zlib` + `struct`），**不引入 matplotlib/Pillow**；
- 1000×360，先 2 倍渲染再 2×2 降采样；
- 图里只有数字刻度（5×7 点阵），**不能出现中文**（没有字库），中文放邮件正文；
- 两张图：余额折线（充值日绿点）、每日消耗柱状（绿点 + 金额，如 `+10`）。

**充值识别**（`usage.daily_usage`）

- 逐区间比较：余额上升 = 有充值；
- 充值额 ≈ 上升幅度 + 该区间消耗（按整体中位速率估算），再按整数元四舍五入；
- 该区间的消耗仍计入当天用电（否则会凭空少一截）；
- 跨午夜区间按小时摊到两天，充值归到后一条读数那天；
- 已知边界：小于区间消耗的充值检测不到（2 小时消耗约 0.1–0.2 元，可忽略）；一天多次充值合并。

**邮件**

- `multipart/related` → `multipart/alternative`（text + html）+ 两张 `image/png`（Content-ID）；
- QQ 邮箱：`smtp.qq.com:465` SSL，密码是**授权码**不是登录密码；
- 明细表列：日期 / 余额 / 用电 / 充值；
- 无数据也发，标题加 `⚠️`；图生成失败降级纯文本；
- 去重：`meta.daily_report_sent`（日期）+ `daily_report_sent_at`（时间），同一天 3 小时内不重发，
  覆盖 23:20 / 23:40 的失败重试；`--force` 可强制重发；
- 收件人优先级 `[report].to` > `[notify].smtp_to`；SMTP 配置在 `secrets.toml`，与 `channel` 无关。

---

## 5. 必须知道的坑（都踩过）

1. **`__RSA__` 前缀**：密码字段少了它，服务端把密文当明文，**静默退回登录页**，不报错；
2. **`.ps1` 必须 UTF-8 with BOM**：否则 Windows PowerShell 5.1 按 GBK 解析中文，报"字符串缺少终止符"；
3. **SQLite 是 WAL 模式**：复制数据库前先 `PRAGMA wal_checkpoint(TRUNCATE)`，或连 `-wal`/`-shm` 一起拷；
4. **不要两边同时跑**：双库 + 并发 CAS 登录；
5. **不要打印 secrets**：`config/secrets.toml` 里有学号、密码、Server酱 key、ingest key；
6. **图里不能有中文**；
7. **`config/config.toml` 与 `secrets.toml` 不在 git 里**，部署到树莓派要单独拷。

---

## 6. 怎么验证

```powershell
# 全套测试（当前 118 个，应全绿）
$env:PYTHONPATH = "src"
python -m unittest discover -s tests

# 只跑图表/算法/邮件的测试
python -m unittest tests.test_usage -v
python -m unittest tests.test_mail tests.test_report_email -v

# 生成两张预览图（假数据：第 5 天充 1 元、第 9 天充 10 元）
python -c "from rest_electric import chart, usage; r=chart.demo_readings(); d=usage.daily_usage(r); import pathlib; p=pathlib.Path('data/preview'); p.mkdir(parents=True, exist_ok=True); p.joinpath('balance.png').write_bytes(chart.line_chart([(x.day,x.balance_cents) for x in d if x.balance_cents is not None], recharge_days={x.day for x in d if x.recharged_cents})); p.joinpath('usage.png').write_bytes(chart.bar_chart(d))"

# 只组装日报、存成 .eml（不联网、不发送）
python -c "from pathlib import Path; from rest_electric import chart, usage, mail; from rest_electric.config import load_config; from rest_electric.forecast import estimate; r=chart.demo_readings(); d=usage.daily_usage(r); p=[(x.day,x.balance_cents) for x in d if x.balance_cents is not None]; c=load_config(); m=mail.build_daily_email(c,p,estimate(p),[],day_usages=d,last_reading=r[-1]); Path('data/preview/daily.eml').write_bytes(m.as_bytes())"
```

预览文件在 `data/preview/`（`data/` 已在 .gitignore）；`.eml` 可直接用邮件客户端打开看效果。

---

## 7. 用户偏好与约束

- 全中文交流；回答简洁，别刷屏；
- **零第三方依赖**是硬约束（树莓派要能长期跑）；
- 新增功能优先新增命令，不改现有 `once` / `report` 的行为；
- 不碰 `config/secrets.toml`，需要用户自己填的东西就让他填；
- 改动要有测试；文档同步更新；
- 用户会自己在树莓派上跑部署，那边也装了 Claude Code。

---

## 8. 当前未提交的改动（新窗口接手时先看一眼）

```
 M README.md
 M config/config.example.toml
 M docs/data-model.md
 M scripts/deploy-pi.sh
 M scripts/install-task.ps1          # Windows 任务加了"错过补跑"，但任务已全部停用
 M src/rest_electric/cli.py
 M src/rest_electric/config.py
 M src/rest_electric/notify.py
 M src/rest_electric/store.py
 M tests/test_config.py
?? docs/email-report-design.md       # 设计稿，已按定稿更新
?? docs/handover.md
?? scripts/systemd/rest-electric-daily.service
?? scripts/systemd/rest-electric-daily.timer
?? src/rest_electric/chart.py        # 图表，已定稿
?? src/rest_electric/mail.py         # 日报组装 + 发送
?? src/rest_electric/usage.py        # 充值识别，已定稿
?? tests/_fake_smtp.py
?? tests/test_mail.py
?? tests/test_report_email.py
?? tests/test_usage.py
```

`config/config.toml` 与 `config/secrets.toml` 不在列表里，正常（.gitignore）。
最近两个提交：`01f26f0 chore: 采集频率改为白天每 2 小时`、`16d2493 docs: 网页版链路定稿`。

---

## 9. 建议的下一步顺序

1. 树莓派上 `sudo bash scripts/deploy-pi.sh`，确认 `rest-electric-daily.timer` 被启用；
2. 填 `config/secrets.toml` 的 `[notify].smtp_*`（QQ 授权码）和 `config.toml` 的 `[report].to`；
3. 手动发一次：`sudo -u restelec PYTHONPATH=src python3 -m rest_electric report --email --force`；
4. 看邮箱 + `journalctl -u rest-electric-daily.service -n 50`，再等一次 23:00 的自动触发；
5. 收尾：`git diff` 过一遍，把新文件提交。

---

## 10. 已拍板的小事（不要再改）

- 发送时间 **23:00**，失败 **23:20 / 23:40** 各重试一次；
- 主题 `电费日报 2026-10-23 · 余额 22.87 元 · 约 12 天`（无今日读数加 `⚠️`）；
- 纯文本版与 `report.render()` **完全一致**；
- 文字报表**不加**「充值」列，充值只在邮件明细里；
- 不带 CSV，保留 Server酱。
