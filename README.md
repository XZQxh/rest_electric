# rest_electric — 宿舍电费自动查询与告警

上海海洋大学「一卡通」微信小程序里能查到宿舍电费余额。本项目把它变成**每天自动记录的本地数据库 + 余额过低时的微信推送**，先在 Windows 上跑通，再原样搬到树莓派 7×24 运行。

> **当前状态：✅ 已跑通——一条 GET 请求就能拿到宿舍电费余额，不需要微信、不需要 token。**
> `once` 默认 `api.mode = "epay"`：GET `/epay/h5/eleresult?sysid=..&roomid=..&areaid=..&buildid=..`，
> 返回 HTML（未登录时自动走一遍 CAS：学号+密码），从页面里读「剩余电量 23.57度」入库。
> 学校 1 度 = 1 元，所以口径与之前完全一致，报表和阈值不用改。
> 原小程序接口（2 小时 token）保留为 `mode = "http"`；手机读屏上报（`serve-ingest`）保留为备用。
> 网页端链路的分析见 [docs/epay-web.md](docs/epay-web.md)。
>
> **每日 23:00 还会发一封带 14 天图表的邮件日报**：`python -m rest_electric report --email`，
> 纯标准库手写 PNG、CID 内嵌，不引入 matplotlib/Pillow。设计见
> [docs/email-report-design.md](docs/email-report-design.md)。

---

## 快速开始

```powershell
# 0. 本会话需要 PYTHONPATH 指向 src
$env:PYTHONPATH = "src"

# 1. 检查环境（Python >= 3.11，零第三方依赖）
python -m rest_electric selftest

# 2. 配置：config/config.toml 已按真实接口填好，只需 token
#    抓包后用一条命令把 token 写进去（不会打印 token 本身）
python scripts/import-flow.py --flow "D:\EdgeDownload\flows (1)" --write-capture

# 3. 真实查询一次，不写库不推送
python -m rest_electric once --dry-run

# 4. 建库并写入
python -m rest_electric init-db
python -m rest_electric once --no-notify

# 5. 看报表
python -m rest_electric report --days 14

# 6. 发一封日报到邮箱（先在 secrets.toml 填好 [notify].smtp_*）
python -m rest_electric report --email --force
```

Windows 下如果不想每次设 `PYTHONPATH`，直接用 `scripts\run-once.ps1`（脚本会自动设置）。

```powershell
.\scripts\run-once.ps1 -DryRun
```

---

## 项目结构

```
rest_electric/
├─ docs/            决策记录、可行性、抓包 SOP、数据模型
├─ config/          配置模板（真实配置与密钥不入库）
├─ capture/         抓包样本存放处（原始文件不入库）
├─ src/rest_electric 实现（纯标准库）
├─ scripts/         Windows 计划任务 / 树莓派部署 / systemd 单元
├─ tests/           单元测试 + 夹具
└─ data/            运行时生成：electric.db
```

模块职责：

| 模块 | 作用 | 协议变更时需要改吗 |
|---|---|---|
| `client.py` | 唯一与学校接口打交道的文件：请求、解析、登录态判断 | **是** |
| `ingest.py` | 手机上报接收器：白名单、密钥、载荷校验、限流 | 否 |
| `config.py` | 读取并校验 TOML 配置，给人话报错 | 否 |
| `http.py` | urllib 封装：超时、指数退避重试、5xx/429 判定 | 否 |
| `store.py` | SQLite：建表、幂等写入、按天取数、区间读数、告警去重 | 否 |
| `forecast.py` | 日均消耗（中位数）、剩余天数、剔除充值跳变 | 否 |
| `alerts.py` | 告警判定：固定阈值 + 预测天数 | 否 |
| `chart.py` | 手写 PNG：折线图、柱状图、5×7 点阵数字、2 倍超采样 | 否 |
| `usage.py` | 逐区间识别充值，算出每天「真实消耗 + 充值额」 | 否 |
| `mail.py` | 日报 HTML 正文 + MIME 组装（CID 内嵌两张 PNG） | 否 |
| `notify.py` | Server酱 / PushPlus / SMTP 三通道；SMTP 支持发送已组装邮件 | 否 |
| `report.py` | 文本日报、CSV 导出 | 否 |
| `cli.py` | `once` / `probe` / `report` / `report --email` / `refresh-token` / `init-db` / `selftest` | 否 |

---

## 命令一览

| 命令 | 说明 |
|---|---|
| `python -m rest_electric selftest` | 检查版本、配置、数据库、时区、推送通道连通性 |
| `python -m rest_electric once` | 查一次、落库、判断并推送告警；`--dry-run` 不写库不推送，`--no-notify` 只写库 |
| `python -m rest_electric serve-ingest` | 常驻接收手机上报的余额（HTTP，需 `[ingest].key`） |
| `python -m rest_electric ingest --balance 24.03` | 手工补录一条读数；`--force` 跳过与上一条的差值校验 |
| `python -m rest_electric check-stale` | 采集端多久没上报了；超过 `stale_hours` 推送告警（退出码 2） |
| `python -m rest_electric report --days 14` | 打印最近 N 天余额、日均、剩余天数；`--csv out.csv` 导出；`--push` 推到微信 |
| `python -m rest_electric report --email` | 发每日邮件日报（HTML + 14 天图表，同一天 3 小时内不重复）；`--force` 强制重发 |
| `python -m rest_electric probe --file capture/sample.json` | 用抓到的响应样本验证解析逻辑，不联网、不写库 |
| `python -m rest_electric refresh-token --token xxx` | 更新 `secrets.toml` 里的 token（人工续期用） |
| `python -m rest_electric init-db` | 初始化数据库 |
| `python -m rest_electric token-status` | 查看当前 token 过期时间（本地解析，不联网） |
| `python scripts/import-flow.py --flow <file> --write-capture` | 从 mitmproxy flow 提取 token 写入 secrets.toml，并生成脱敏样本 |
| `python scripts/import-flow.py --flow <file> --list` | 列出该 flow 里的全部请求，用于找登录接口 |

退出码：`0` 成功；`2` 查询失败 / 采集端离线 / 日报发送失败（已推送通知）；`3` 配置错误；`4` 登录态失效。

---

## 半自动日常流程（token 兜底通道，默认不启用）

手机上报是主通道。下面这套只在「手机坏了 / 小程序改版读不到」时用来临时补数据。
原因见 [docs/token-auth-analysis.md](docs/token-auth-analysis.md)：查询必须用 2 小时有效的 JWT，
而换 token 必须提供微信客户端实时生成的一次性 `code`，服务器无法自己完成。

### 一键刷新 token（推荐）

token 过期后，在项目目录跑：

```powershell
.\scripts\refresh-token.ps1
```

脚本会依次：开系统代理 → 启动带插件的 mitmweb → 提示你打开小程序 → 检测到新 token →
**自动恢复系统代理并关闭 mitmweb**。你只需要在弹出的提示后，用**电脑微信**打开
「上海海洋大学一卡通」小程序，进入电费查询。

默认最多等 5 分钟；超时会自动恢复网络。完成后验证：

```powershell
.\scripts\run-once.ps1 -DryRun
```

### 手动方式（脚本不可用时）

```powershell
.\scripts\proxy-on.ps1        # 1. 开代理
.\scripts\start-mitmweb.ps1   # 2. 启动带插件的 mitmweb（保持窗口开着）
# 3. 打开电脑微信 → 小程序 → 电费查询，控制台出现 "token 已更新"
.\scripts\proxy-off.ps1       # 4. 恢复上网（务必执行）
```

> 顺序很重要：**先 proxy-off 再关 mitmweb**，否则网页会打不开。
>
> 小程序只在 token 过期时才会重新登录。token 还有效时打开小程序不会触发刷新，
> 脚本会一直等到超时——这是正常的。

`start-mitmweb.ps1` 加载的 `scripts/token_watcher.py` 会监听
`/openservice/wechatauth/token` 的响应，把新 token 直接写进 `config/secrets.toml`，
**不打印 token，也不需要复制粘贴**。

查看当前 token 还有多久过期（不联网）：

```powershell
python -m rest_electric token-status
```

> token 只有 2 小时，所以**每次刷新后 2 小时内**的定时查询才能成功。
> 过期后 `once` 会返回退出码 4 并推送「需要续期」提醒（每天最多一条）。
>
> 备用刷新方式（不启动插件时）：抓包后跑
> `python scripts/import-flow.py --flow <flow文件>`，
> 或手工 `python -m rest_electric refresh-token --token "<新 token>"`。
>
> **`config.toml` 里 `token_prefix = "Bearer "` 的末尾空格不能删**，否则服务端返回 403。

---

## 运行测试

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
```

**118 个用例**，纯标准库、无网络访问（推送测试用本地假服务端；邮件用假 SMTP）。
测试临时目录建在 `.test-tmp/`，跑完自动清理。


---

## 定时运行

**Windows（开发机；计划任务已停用）**

```powershell
# 只保留脚本，生产已迁到树莓派——不要重新启用，否则会双写库/并发 CAS 登录
.\scripts\install-task.ps1
# 卸载
.\scripts\install-task.ps1 -Uninstall
```

**树莓派（现行：网页版 + 定时器 + 邮件日报）**

```bash
sudo bash scripts/deploy-pi.sh          # 装 systemd 单元
systemctl list-timers 'rest-electric*'
journalctl -u rest-electric.service -n 50
```

- `rest-electric.timer` → `once`（`mode = "epay"`），白天 **08:07–22:07 每 2 小时**一次（共 8 次），带 0–10 分钟抖动；
- `rest-electric-daily.timer` → `report --email`，每天 **23:00** 发带 14 天图表的日报，失败在 23:20 / 23:40 重试；
  只有 `[notify].smtp_*` 与收件人配置齐全时 deploy 脚本才会启用它；
- `rest-electric-ingest.service` + `rest-electric-stale.timer`：**备用**的手机上报通道，不用可以不管；
- 网页端没有 2 小时 token 限制，所以不再需要「抓 token / 打开小程序」这套流程。

手动验证日报（树莓派上）：

```bash
cd /opt/rest_electric
sudo -u restelec PYTHONPATH=src python3 -m rest_electric report --email --force
journalctl -u rest-electric-daily.service -n 50
```

---

## 设计要点

- **纯标准库**：不装任何第三方包。树莓派 Zero 2W 也能跑，不会因为 pip 或版本漂移半夜挂掉。
- **金额一律整数分**：数据库里没有浮点，避免 `0.1+0.2` 那类误差。
- **充值单独识别**：逐区间判断余额上升，把充值从用电里剥出去，同时把该区间的消耗加回来；
  小额充值（小于区间消耗）也不会被日终对比吃掉。
- **日报图也是零依赖**：`zlib + struct` 手写 PNG，邮件用 CID 内嵌，不依赖第三方图床。
- **宁可多报一次，不可静默失败**：查询失败、token 过期都会推送，且有冷却期避免刷屏。
- **只查自己一个房间**：不做房间枚举，`room_key` 只是数据分区字段。

---

## 安全与合规

- `config/secrets.toml`、`data/`、`capture/` 的原始抓包都在 `.gitignore` 里，**不要提交、不要外发**。
- 抓包样本入库前请脱敏（token、openid、学号、姓名替换为占位符）。
- 只查询本人宿舍，白天每 2 小时一次（8 次/天），不提供任何他人房间查询能力。
- 该接口非公开，学校随时可能变更或加固；届时按 [docs/recon-sop.md](docs/recon-sop.md) 重新抓包，通常只需更新 `client.py`。

---

## 文档

- [docs/epay-web.md](docs/epay-web.md) — **主通道**：网页版查询 + CAS 自动登录
- [docs/email-report-design.md](docs/email-report-design.md) — **每日 23:00 邮件日报**：图表、充值识别、MIME 结构、调度
- [docs/handover.md](docs/handover.md) — 交接说明：谁在跑、数据在哪、踩过的坑
- [docs/phone-automation.md](docs/phone-automation.md) — 备用方案：手机读屏 + 树莓派接收，含四道验证门与排障
- [docs/token-auth-analysis.md](docs/token-auth-analysis.md) — **为什么不再折腾 token**：认证机制的只读分析与证据
- [docs/结论与后续.md](docs/结论与后续.md) — 项目为什么停下、什么条件下值得重启
- [docs/mitmweb-quickstart.md](docs/mitmweb-quickstart.md) — **第一次用 mitmweb 先看这个**：界面怎么读、代理怎么开、问题对照表
- [docs/decisions-dianfei.md](docs/decisions-dianfei.md) — 决策记录（结论 + 理由 + 待定项）
- [docs/feasibility.md](docs/feasibility.md) — 三条自动化路线对比、决策门 G1–G4、合规边界
- [docs/recon-sop.md](docs/recon-sop.md) — 抓包 SOP，含证书固定/加密响应/代理失效的预案
- [docs/data-model.md](docs/data-model.md) — 表结构、字段语义、备份
