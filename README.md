# rest_electric — 宿舍电费自动查询与告警

上海海洋大学「一卡通」微信小程序里能查到宿舍电费余额。本项目把它变成**每天自动记录的本地数据库 + 余额过低时的微信推送**，先在 Windows 上跑通，再原样搬到树莓派 7×24 运行。

> **当前状态：真实接口已接通，但"7×24 全自动"被微信认证卡住。**
> `python -m rest_electric once` 能查到真实余额（实测 24.10 → 24.03 元）。
> 阻塞点：token 是 2 小时 JWT，且换取 token 必须提供微信客户端实时生成的一次性 `code`；
> 实测 CAS 会话、空 code、复用旧 code 全部失败（详见 [docs/decisions-dianfei.md](docs/decisions-dianfei.md) 的 D12）。
> **树莓派单独无法无人值守**，已选定**半自动**方案（D13）：每天打开一次小程序，token 自动落盘，其余全自动。
> 另外该服务只在校园网可达（`shoucard.shou.edu.cn` → `172.18.3.170`）。

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
| `config.py` | 读取并校验 TOML 配置，给人话报错 | 否 |
| `http.py` | urllib 封装：超时、指数退避重试、5xx/429 判定 | 否 |
| `store.py` | SQLite：建表、幂等写入、按天取数、告警去重 | 否 |
| `forecast.py` | 日均消耗（中位数）、剩余天数、剔除充值跳变 | 否 |
| `alerts.py` | 告警判定：固定阈值 + 预测天数 | 否 |
| `notify.py` | Server酱 / PushPlus / SMTP 三通道 | 否 |
| `report.py` | 文本日报、CSV 导出 | 否 |
| `cli.py` | `once` / `probe` / `report` / `refresh-token` / `init-db` / `selftest` | 否 |

---

## 命令一览

| 命令 | 说明 |
|---|---|
| `python -m rest_electric selftest` | 检查版本、配置、数据库、时区、推送通道连通性 |
| `python -m rest_electric once` | 查一次、落库、判断并推送告警；`--dry-run` 不写库不推送，`--no-notify` 只写库 |
| `python -m rest_electric report --days 14` | 打印最近 N 天余额、日均、剩余天数；`--csv out.csv` 导出；`--push` 推到微信 |
| `python -m rest_electric probe --file capture/sample.json` | 用抓到的响应样本验证解析逻辑，不联网、不写库 |
| `python -m rest_electric refresh-token --token xxx` | 更新 `secrets.toml` 里的 token（人工续期用） |
| `python -m rest_electric init-db` | 初始化数据库 |
| `python -m rest_electric token-status` | 查看当前 token 过期时间（本地解析，不联网） |
| `python scripts/import-flow.py --flow <file> --write-capture` | 从 mitmproxy flow 提取 token 写入 secrets.toml，并生成脱敏样本 |
| `python scripts/import-flow.py --flow <file> --list` | 列出该 flow 里的全部请求，用于找登录接口 |

退出码：`0` 成功；`2` 查询失败（已推送失败通知）；`3` 配置错误；`4` 登录态失效。

---

## 半自动日常流程（当前采用的方案）

原因见 [docs/decisions-dianfei.md](docs/decisions-dianfei.md) D12：查询必须用 2 小时有效的 JWT，
而换 token 必须提供微信客户端实时生成的一次性 `code`，服务器无法自己完成。

所以流程设计成「你打开一次小程序，其它全自动」。

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

54 个用例，纯标准库、无网络访问（推送测试用本地假服务端）。测试临时目录建在 `.test-tmp/`，跑完自动清理。

---

## 定时运行

**Windows（现在）**

```powershell
# 注册每天 09:07 / 21:07 两个计划任务（含 ±N 分钟抖动由程序内部处理）
.\scripts\install-task.ps1
# 卸载
.\scripts\install-task.ps1 -Uninstall
```

**树莓派（以后）**

```bash
sudo bash scripts/deploy-pi.sh          # 复制到 /opt/rest_electric 并安装 systemd 单元
sudo systemctl enable --now rest-electric.timer
systemctl list-timers rest-electric.timer
journalctl -u rest-electric.service -n 50
```

`rest-electric.timer` 带 `Persistent=true`，断网/关机期间错过的执行会在恢复后补跑一次。

---

## 设计要点

- **纯标准库**：不装任何第三方包。树莓派 Zero 2W 也能跑，不会因为 pip 或版本漂移半夜挂掉。
- **金额一律整数分**：数据库里没有浮点，避免 `0.1+0.2` 那类误差。
- **充值不算用电**：余额上升的区间直接丢弃，不会把充值误算成负消耗。
- **宁可多报一次，不可静默失败**：查询失败、token 过期都会推送，且有冷却期避免刷屏。
- **只查自己一个房间**：不做房间枚举，`room_key` 只是数据分区字段。

---

## 安全与合规

- `config/secrets.toml`、`data/`、`capture/` 的原始抓包都在 `.gitignore` 里，**不要提交、不要外发**。
- 抓包样本入库前请脱敏（token、openid、学号、姓名替换为占位符）。
- 只查询本人宿舍，频率压到每天 2 次，不提供任何他人房间查询能力。
- 该接口非公开，学校随时可能变更或加固；届时按 [docs/recon-sop.md](docs/recon-sop.md) 重新抓包，通常只需更新 `client.py`。

---

## 文档

- [docs/结论与后续.md](docs/结论与后续.md) — **项目为什么停下、什么条件下值得重启**
- [docs/mitmweb-quickstart.md](docs/mitmweb-quickstart.md) — **第一次用 mitmweb 先看这个**：界面怎么读、代理怎么开、问题对照表
- [docs/decisions-dianfei.md](docs/decisions-dianfei.md) — 决策记录（结论 + 理由 + 待定项）
- [docs/feasibility.md](docs/feasibility.md) — 三条自动化路线对比、决策门 G1–G4、合规边界
- [docs/recon-sop.md](docs/recon-sop.md) — 抓包 SOP，含证书固定/加密响应/代理失效的预案
- [docs/data-model.md](docs/data-model.md) — 表结构、字段语义、备份
