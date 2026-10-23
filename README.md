# rest_electric

把宿舍电费变成一条可以长期观察的曲线：**一条 GET 请求读出剩余电量 → 存进本地 SQLite → 每天算用电与花费 → 余额见底前推送 → 夜里再发一封带趋势图的邮件日报**。

纯标准库实现，零第三方依赖；Windows 上开发，树莓派上 7×24 常驻。

> **使用须知（请先读）**
>
> - 本项目针对**特定高校**的「一卡通 · 电费」网页版接口，只支持查询**你本人账号下的本人宿舍**。
> - 该接口不是公开 API。用脚本自动登录、定时访问是否被允许，取决于学校相关服务条款；
>   由此产生的后果（账号被限制、被要求停止等）由使用者自行承担。
> - 程序只做只读操作：**一次 GET 查询结果页 + 一次 CAS 登录 POST**，不调用充值接口，
>   不枚举房间，不向任何第三方提供查询服务。
> - 仓库里不含任何真实凭据、学号、姓名或房间号；`config/*.toml` 与 `data/` 都在 `.gitignore` 里。
>   贡献代码时请确认同样没有带入这些信息。
> - 如果你不是这所学校的学生，或者学校明确禁止此类访问，请不要使用。

---

## 它能做什么

| | |
|---|---|
| 采集 | 白天每 2 小时查一次余额，写入 SQLite；带随机抖动，避免固定节律 |
| 记录 | 金额一律整数「分」，不做浮点运算；保留原始响应便于协议变更后回溯 |
| 分析 | 中位数算法估算日均用量与剩余天数；逐区间识别充值，把充值从用电里剥出去 |
| 告警 | 余额低于阈值、预计天数不足、登录失败、采集端离线，走 Server酱 / PushPlus / SMTP |
| 日报 | 每天 23:00 一封 HTML 邮件：14 天余额折线 + 每日用电柱状图（手写 PNG，CID 内嵌） |
| 导出 | `report --csv` 导出 CSV，`report --days N` 看任意区间 |

余额口径按「1 度 = 1 元」处理，与页面上的「剩余电量 23.57 度」一致。

---

## 环境要求

- Python **3.11+**
- 不需要任何第三方包（连图像库都没有，PNG 是 `zlib + struct` 手写的）

## 安装

```bash
git clone https://github.com/XZQxh/rest_electric.git
cd rest_electric

python -m venv .venv
# Windows: .venv\Scripts\activate     Linux/macOS: source .venv/bin/activate
pip install -e .
```

装完就能用 `rest-electric` 命令。不想装也行，直接 `PYTHONPATH=src python -m rest_electric ...`。

> 配置与数据路径固定在**仓库根目录**下（`config/`、`data/`），所以命令在仓库根目录执行。

## 配置

```bash
cp config/config.example.toml config/config.toml     # 模板，不含密钥
cp config/secrets.example.toml config/secrets.toml   # 含密钥，已在 .gitignore 里
chmod 600 config/secrets.toml                        # Linux/macOS
```

**1. 填房间号**（`config/config.toml`）

```toml
[room]
room_key = "你的房间号"   # 本地数据分区键，只用于数据库与报表显示
building = "1"           # 楼栋号（接口里的 buildid）
room = "你的房间号"       # 真正参与查询的房间号（roomid）
```

> 模板里是 `12345678` 这样的**占位值**，必须换成你自己的。程序会校验服务端返回的房间号，
> 对不上就拒绝入库，所以填错只会查不到，不会记错房间。

**2. 填登录凭据**（`config/secrets.toml`）

```toml
[epay]
username = "你的学号"
password = "统一身份认证密码"
```

首次查询会自动走一遍 CAS 登录，之后把 Cookie 缓存在 `data/epay-cookies.txt`（600），过期才重新登录。

**3. 配通知**（可选，`config.toml` 的 `[notify]` + `secrets.toml`）

- `channel = "serverchan"` → 填 `[notify].sendkey`（Server酱）
- `channel = "pushplus"` → 填 `[notify].pushplus_token`
- `channel = "smtp"` → 填 `smtp_*`（QQ 邮箱用**授权码**，不是登录密码）
- 不想收推送就设 `channel = "none"`

邮件日报走独立的 `[notify].smtp_*`，与上面 `channel` 互不影响。

## 验证链路

```bash
rest-electric selftest            # 检查版本、配置、数据库、时区、推送通道
rest-electric init-db             # 建库
rest-electric once --dry-run      # 真查一次，不写库不推送
```

`--dry-run` 能打印出「网页端读数：23.57 度」就说明链路通了。然后：

```bash
rest-electric once                # 写库 + 按需推送
rest-electric report --days 14    # 看报表
rest-electric report --email      # 立刻发一封日报（--force 忽略冷却）
```

### 完全离线试跑

没有账号也想看看效果：把 `config.toml` 里 `api.mode = "fixture"`，
程序会读 `capture/sample_response.json`，不联网、不写库。

## 常驻运行

**树莓派 / Linux**

```bash
sudo bash scripts/deploy-pi.sh
systemctl list-timers 'rest-electric*'
journalctl -u rest-electric.service -n 50
```

- `rest-electric.timer` → `once`，白天 08:07–22:07 每 2 小时一次（8 次），带 0–10 分钟抖动
- `rest-electric-daily.timer` → `report --email`，每天 23:00，失败在 23:20 / 23:40 重试
- `rest-electric-ingest.service` + `rest-electric-stale.timer`：可选的手机上报通道，不用可以停掉

**Windows**

```powershell
.\scripts\install-task.ps1              # 注册计划任务
.\scripts\install-task.ps1 -Uninstall   # 卸载
.\scripts\run-once.ps1 -DryRun          # 手动跑一次（自动设置 PYTHONPATH）
```

> 同一份数据只应有一个采集端在跑。Windows 与树莓派同时定时会重复写库、并发登录。

## 命令一览

| 命令 | 说明 |
|---|---|
| `rest-electric selftest` | 环境与配置自检，`--send-test` 实际发一条推送 |
| `rest-electric once` | 查一次、落库、按需告警；`--dry-run` 只验证，`--no-notify` 只写库 |
| `rest-electric report` | 文本报表；`--days N`、`--csv out.csv`、`--push`、`--email`、`--force` |
| `rest-electric probe --file <样本>` | 用响应样本验证解析逻辑，不联网 |
| `rest-electric init-db` | 初始化数据库 |
| `rest-electric epay-cookie` | 手工写入网页端 Cookie（跳过自动登录，调试用） |
| `rest-electric ingest --balance 24.03` | 手工补录一条读数，`--force` 跳过差值校验 |
| `rest-electric serve-ingest` | 常驻接收手机上报（需要 `[ingest].key`） |
| `rest-electric check-stale` | 检查采集端多久没上报，超时推送告警 |
| `rest-electric refresh-token` / `token-status` | 仅 HTTP 模式：更新 / 查看小程序接口 token |

退出码：`0` 成功；`2` 查询失败、采集端离线或日报发送失败（已推送通知）；`3` 配置错误；`4` 登录态失效。

## 数据与隐私

- 数据库只有四类信息：房间号、余额（分）、采集时间、来源。**不存学号、姓名、密码。**
- `config/secrets.toml`、`data/`、`data/epay-cookies.txt` 都在 `.gitignore` 里，权限 600。
- 日志会脱敏：学号只打印前 3 位加 `***`；token 从不打印。
- 想清空历史：删掉 `data/electric.db` 重新 `init-db` 即可。

## 适配其它学校

当前实现绑定在这所学校的三个约定上：接口域名、`/epay/h5/eleresult` 的查询参数、结果页里
「剩余电量」的 HTML 结构。换学校时一般只需要改：

1. `config.toml` 的 `[epay].base_url` / `result_path` / `sysid` / `areaid` / `buildid`
2. `src/rest_electric/epay.py` 里的 CAS 地址与结果页正则

协议细节见 [docs/epay-web.md](docs/epay-web.md)。

## 项目结构

```
rest_electric/
├─ src/rest_electric/   实现（client/epay/ingest/store/forecast/chart/mail/...）
├─ tests/               单元测试与夹具（118 个用例，无网络访问）
├─ scripts/             systemd 单元、部署脚本、Windows 计划任务
├─ config/              配置模板（真实配置与密钥不入库）
├─ capture/             脱敏后的响应样本
├─ docs/                协议、数据模型、邮件日报设计
└─ data/                运行时生成：electric.db、会话 Cookie
```

## 开发与测试

```bash
python -m unittest discover -s tests -v
```

测试不联网：推送用本地假服务端，邮件用假 SMTP，临时目录建在 `.test-tmp/` 并自动清理。

提交前顺手确认没有把凭据带进去：

```bash
git status --short
git grep -nE "SCT[0-9a-f]{10}|password\s*=\s*\"[^\"]+\""
```

## 文档

- [docs/epay-web.md](docs/epay-web.md) — 网页版查询链路：CAS 登录、结果页解析、失败模式
- [docs/email-report-design.md](docs/email-report-design.md) — 邮件日报：图表、充值识别、MIME 结构
- [docs/data-model.md](docs/data-model.md) — 表结构与字段语义
- [scripts/phone/README.md](scripts/phone/README.md) — 可选的手机读屏采集通道

## 许可

[MIT](LICENSE)
