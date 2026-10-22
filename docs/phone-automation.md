# 手机采集端：让闲置安卓手机读余额，树莓派记数据

> **2026-10-22 傍晚：本方案已降级为备用。**
> 网页版 `/epay/h5/eleresult` 可以直接查电费（CAS 登录，不需要微信），见 [epay-web.md](epay-web.md)。
> 只有在网页端也挂掉时，才需要动用这里的手机通道。

日期：2026-10-22　　状态：**代码已就绪，等待真机校准**（P0 的四道门需要你在手机上跑一遍）

---

## 1. 为什么是「读屏」而不是「抓 token」

业务 token 只有 2 小时，且换 token 必须由微信客户端现场生成一次性 `code`
（证据见 [token-auth-analysis.md](token-auth-analysis.md)）。所以「让服务器自己续期」这条路是死的。

换个角度：**系统真正要的是余额，不是 token。**
让手机每次重新打开小程序、把页面上那个数字读出来上报，token 的问题就不存在了——
小程序自己会 `wx.login()` 换新 token，我们完全不参与。

代价：手机必须常开、微信必须常驻、页面结构变了要重新校准。

## 2. 架构

```
┌──────────────┐   读屏    ┌────────────────┐   HTTP/JSON   ┌─────────────────────┐
│ 闲置安卓手机  │ ────────► │ 采集脚本        │ ────────────► │ 树莓派              │
│ 小号微信      │           │ A: adb (Pi 驱动)│  X-Ingest-Key │ rest-electric-ingest│
│ 小程序       │           │ B: AutoX.js     │               │  ├ SQLite readings  │
└──────────────┘           └────────────────┘               │  ├ 预测/告警        │
        ▲                          │ 失败                        │  └ Server酱 推送    │
        │                          ▼                            └─────────────────────┘
        └──────── 每 2 小时 ── 本地 queue.jsonl（断网补传）              ▲
                                                                        │
                                                    check-stale（每 6 小时）：30 小时
                                                    没有新读数 → 推送「采集端离线」
```

两种引擎，二选一：

| | A. 树莓派 + adb（`scripts/phone/adb_dump.py`） | B. AutoX.js（`scripts/phone/electric_auto.js`） |
|---|---|---|
| 手机上要装什么 | 什么都不用装，只需开 USB 调试 | AutoX.js（侧载 APK）+ 无障碍权限 |
| 逻辑在哪 | 树莓派上，Python，可测试、可日志 | 手机上，JS，手机自己跑 |
| 调度 | systemd timer | 脚本内 `sleep` 循环 |
| 重启后 | 无线 adb 失效，需要插一次 USB 重新 `adb tcpip 5555` | 需要给 AutoX「自启动/后台运行」权限 |
| 适合 | 愿意插一次 USB、想要集中日志 | 手机放得远、不想连电脑 |

先按 §3 的 G2 门两种都试一下：**谁能稳定读到余额，就用谁**。

## 3. 四道门（必须先过，再谈部署）

### G1 账号门：小号绑定（有风险，自己拍板）

小号第一次打开电费页会走：`token → retcode 9 此用户未注册 → CAS → 绑定`。

未知：学校是「一个学号可绑多个微信」还是「后绑覆盖前绑」。

1. 小号打开小程序 → 电费页，记录是否出现「未注册」与 CAS 页；
2. 完成绑定；
3. **立刻**用主号打开小程序，确认是否仍正常；
4. 若主号被顶掉：用同一 CAS 流程把主号绑回来，确认可恢复。

- 两号都能用 → 最理想；
- 只有小号能用、主号可重绑 → 可以接受，但要记住这个代价；
- 绑定被拒 / 主号救不回来 → **停**，看 §8 回退。

> 这一步会改变账号状态，属于你自己决定的事。

### G2 可读性门：无障碍能不能读到那个数字

**引擎 A：**
```bash
adb connect <手机IP>:5555
python3 scripts/phone/adb_dump.py --calibrate
```
**引擎 B：** AutoX 里跑一段：
```javascript
toast(textMatches(/\d+\.\d{2}/).find().length)
```
或在 AutoX 控制台执行 `textMatches(/\d+\.\d{2}/).find().forEach(n => log(n.text() + " " + n.bounds()))`。

要看到类似 `24.03` 的节点，并且能用「电费/余额」标签把它和卡号、学号区分开。

### G3 保活门

- 息屏后能否唤醒；有锁屏密码的机器**无法**自动解锁 → 备用机建议不设密码；
- AutoX 是否被系统杀（电池优化白名单、自启动、前台服务）；
- 手机重启后脚本/无线 adb 是否恢复。

### G4 网络门

手机和树莓派同一 Wi-Fi；给树莓派做 DHCP 保留（固定 IP）。
手机不在家时上报会失败，脚本会进队列，回家后自动补传——**不要做内网穿透**。

## 4. 树莓派侧部署

```bash
sudo bash scripts/deploy-pi.sh          # 安装并启动接收器 + 离线检查
```

它会：
- 装 `rest-electric-ingest.service`（常驻，`Restart=always`，只读系统 + 只允许写 `data/`）；
- 装 `rest-electric-stale.timer`（每 6 小时检查有没有新读数）；
- **默认停用**原来的「每天 2 次直连查询」定时器（手机通道上线后它只会因 2 小时 token 过期误报）；
  想保留就加 `--with-once-timer`。

配置两处：

```toml
# config/config.toml
[ingest]
bind = "0.0.0.0"
port = 8787
allow_cidr = ["127.0.0.1/32", "::1/128", "192.168.0.0/16"]
```

```toml
# config/secrets.toml
[ingest]
key = "<32 位随机 hex>"
```

生成密钥：`python3 -c "import secrets;print(secrets.token_hex(16))"`

自测（不经过手机）：

```bash
curl -s -X POST http://127.0.0.1:8787/ingest \
  -H 'X-Ingest-Key: <key>' -H 'Content-Type: application/json' \
  -d '{"balance":"24.03","source":"phone"}'
python3 -m rest_electric report --days 7
```

接收器的几条硬规则：

- 只收 `room_key` 等于配置里那一个房间的读数；
- 同一房间、同一金额、10 分钟内重复上报 → 判定为重复，返回 `inserted: false`（重试安全）；
- 与上一条读数相差超过 `max_delta_cents`（默认 20 元）→ `422`，并推送「读数异常」，需人工确认后
  `python3 -m rest_electric ingest --balance <值> --force`；
- 手机时钟偏差超过 `max_skew_seconds` 时以服务器时间为准，原时间留在 `raw_json`；
- 同一来源 5 秒内重复请求 → `429`。

## 5. 手机侧：引擎 A（adb）

一次性：

```bash
# 手机：设置 → 开发者选项 → USB 调试
# 树莓派上，手机用 USB 连着时：
adb devices
adb tcpip 5555
# 拔掉 USB，确认无线可用：
adb connect <手机IP>:5555
```

校准（G2）：

```bash
python3 scripts/phone/adb_dump.py --calibrate      # 打印当前屏幕所有文字与坐标
```

试跑：

```bash
python3 scripts/phone/adb_dump.py --dry-run        # 只读，不上报
REST_ELECTRIC_INGEST_KEY=<key> python3 scripts/phone/adb_dump.py --url http://127.0.0.1:8787/ingest
```

定时（每 12 小时，早晚各一次）：

```ini
# /etc/systemd/system/rest-electric-phone.service
[Unit]
Description=用 adb 读电费余额并上报
After=network-online.target

[Service]
Type=oneshot
User=restelec
Environment=PYTHONPATH=/opt/rest_electric/src
Environment=REST_ELECTRIC_INGEST_KEY=<key>
ExecStart=/usr/bin/python3 /opt/rest_electric/scripts/phone/adb_dump.py --serial <手机IP>:5555 --url http://127.0.0.1:8787/ingest
SuccessExitStatus=2

[Install]
WantedBy=multi-user.target
```

配一个 `OnCalendar=*-*-* 09:07:00` / `21:07:00` 的 timer 即可。

> 注意：手机重启后 `adb tcpip` 失效。要么别重启，要么重启后插一次 USB。
> 所以更稳的做法是把引擎 B 作为备用：两者都部署时，谁成功都行。

## 6. 手机侧：引擎 B（AutoX.js）

1. 侧载 AutoX.js，授予无障碍、后台运行、自启动；
2. 把 `scripts/phone/electric_auto.js` 放到 `/sdcard/electric/`；
3. 改文件顶部的 `CFG.url`、`CFG.key`；
4. 先手动跑一次看控制台；
5. 稳定后开「定时执行」或依赖脚本里的 `sleep` 循环（AutoX 需要前台服务保活）。

脚本行为：
- 唤醒 → 回桌面 → 点「上海海洋大学一卡通」快捷方式（比在微信里层层点稳）；
- 等页面 → 找「电费/余额」标签最近的金额节点 → 范围校验 → 上报；
- 失败进 `/sdcard/electric/queue.jsonl`，下次先补传；
- 4xx 视为永久失败，记到 `rejected.jsonl`（不会无限重试）。

## 7. 故障排查

| 现象 | 检查 |
|---|---|
| `401` | 手机脚本里的 key 与 `secrets.toml` 不一致 |
| `403` | 手机不在 `allow_cidr` 网段 |
| `422` 相差过大 | 读到了别的数字，或真的充值了；看 `data/rest_electric.log`，确认后 `ingest --force` |
| `429` | 上报太频繁（默认 5 秒 1 次） |
| 树莓派日志 | `journalctl -u rest-electric-ingest -n 50` |
| 手机日志 | AutoX 控制台 / `/sdcard/electric/fail-*.png` |
| 30 小时没数据 | 收到「采集端离线」推送 → 看手机是不是被锁、微信是不是掉了 |

## 8. 回退

1. G1 失败 → 回到半自动：PC 开机时抓一次 token，`once` 手动跑；
2. G2 失败（两种引擎都读不到）→ 截图 + OCR（要引入第三方依赖，另评估）；
3. G3 失败 → 用 Tasker/MacroDroid 定时拉起 AutoX；
4. 全失败 → 回到 [token-auth-analysis.md](token-auth-analysis.md) §8 的方案 A（PC 常开 + 只代理校园域名）。

## 9. 安全与合规

- 只读自己的房间，不枚举、不批量；
- 手机脚本里的 key 只用于局域网上报，泄漏了也只是能写一条余额；
- `ingest` 只监听局域网，有 IP 白名单 + 共享密钥 + 限流，**不要**映射到公网；
- 小号只用于这台手机，不做多开/分身，频率 2–4 次/天；
- 微信 UI 改版、学校接口变更都会让脚本失效——失效时**告警**，不静默。
