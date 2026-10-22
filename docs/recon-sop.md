# 抓包 SOP：定位「上海海洋大学一卡通」小程序的电费查询接口

目标：拿到**一次完整查询**的请求（URL、方法、请求头、请求体）和响应原文，判断走 [feasibility.md](feasibility.md) 的哪个决策门（G1–G4）。

> 第一次用 mitmproxy/mitmweb，先读 [mitmweb-quickstart.md](mitmweb-quickstart.md)：界面怎么读、代理开关怎么用、常见故障对照表。本文是完整流程，那份是上手速查。

预计耗时 20–40 分钟。只做一次，产物长期复用（直到 token 过期）。**全程只操作本人的账号与本人宿舍。**

---

## 0. 准备

- 一台 Windows 电脑，已登录 PC 微信，能正常打开小程序查到余额。
- 管理员权限（装根证书需要）。
- 记录工具：记事本即可。

工具选型：

| 工具 | 适用 | 说明 |
|---|---|---|
| **mitmproxy**（推荐） | 命令行、可脚本化、能导出 flow | `winget install mitmproxy` 或 `pipx install mitmproxy` |
| Fiddler Classic | 不想碰命令行 | 图形界面，导出 JSON 方便；注意默认只解 HTTPS 需开 `Tools → Options → HTTPS → Decrypt` |
| Proxifier | **微信不认系统代理时必备** | 收费软件，可试用；强制指定进程走代理 |

> 注意：mitmproxy / Fiddler 只用于本次侦察，**不是**运行时依赖。运行时仍然只用 Python 标准库。

---

## 1. 启动代理

```powershell
# mitmproxy 三件套里选一个：
mitmweb                      # 带 Web 界面的抓包，浏览器自动打开 127.0.0.1:8081
# 或
mitmdump -w capture\raw.flow # 无界面，直接录到文件
```

默认监听 `127.0.0.1:8080`。首次运行会在 `%USERPROFILE%\.mitmproxy\` 生成一套证书。

---

## 2. 安装根证书（关键步骤）

```powershell
# 管理员 PowerShell
certutil -addstore -f Root "$env:USERPROFILE\.mitmproxy\mitmproxy-ca-cert.cer"
```

或双击 `mitmproxy-ca-cert.cer` → 安装证书 → 当前用户 → **受信任的根证书颁发机构**。

> 只想给当前用户装而不动全机信任库时，选「当前用户」即可。事后卸载见第 8 节。

验证：浏览器访问 `https://example.com`，页面正常且 mitmproxy 里有记录，说明根证书生效。

---

## 3. 让微信走代理

**3.1 先试系统代理**（多数情况够用）

设置 → 网络和 Internet → 代理 → 手动设置代理 → `127.0.0.1` : `8080`，保存。

或命令行：

```powershell
$k='HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings'
Set-ItemProperty $k ProxyEnable 1
Set-ItemProperty $k ProxyServer '127.0.0.1:8080'
```

**3.2 如果小程序流量没出现在 mitmproxy 里**（很常见）

PC 微信的小程序跑在独立进程 `WeChatAppEx.exe`（旧版是 `WeChatApp.exe`）里，**经常不读系统代理**。此时用 Proxifier：

1. `Profile → Proxy Servers → Add`：`127.0.0.1:8080`，Protocol 选 `HTTP`，勾选 HTTPS。
2. `Profile → Proxification Rules`：新增规则，Applications 填 `WeChatAppEx.exe;WeChat.exe`，Action 选上面的代理。
3. 规则优先级放在 Default 之上。

**3.3 重启微信并抓包**

1. 托盘右键微信 → **退出**（必须完全退出，右上角关窗口不算）。
2. 重新登录微信。
3. 打开「上海海洋大学一卡通」小程序。
4. 在 mitmweb 里观察流量；正常应立刻看到 `*.qq.com`、`servicewechat.com` 等域名。
5. 进入电费查询，**选楼栋 → 选房间 → 查询**。
6. 在 mitmweb 的过滤栏输入 `~u shou` 或 `~u ecard` 缩小范围；也可以点一次查询后按时间排序找最后几条。

---

## 4. 记录什么

对那条**返回余额**的请求，逐项抄下来：

- [ ] 请求方法（GET / POST）
- [ ] 完整 URL（含 query string）
- [ ] 请求头：`Authorization`、`token`、`Cookie`、`openid`、`Referer`、`User-Agent`、`Content-Type`、自定义签名头
- [ ] 请求体（POST 时）：楼栋号、房间号、`sign`、`timestamp`、`nonce`
- [ ] 响应头（尤其 `Set-Cookie`）
- [ ] 响应体原文
- [ ] 时间戳（用来推断 token 有效期起点）

把请求与响应保存为两个文件：

```
capture/req-balance.http      # 方法 + URL + 请求头 + body
capture/resp-balance.json     # 响应体原文（脱敏后）
```

> `.gitignore` 已排除 `capture/` 下的原始文件，避免误提交。

脱敏清单：`token`、`Cookie`、`openid`、学号、姓名、手机号替换为 `REDACTED`。**脱敏前先备份一份原始文件到本机其它目录**，因为后面复现请求还要用真值。

---

## 5. 判断是否加密 / 有签名

**5.1 看响应**

- 明文 JSON，字段里有金额 → 好，走 G1/G2。
- 形如 `{"encryptData":"Base64..."}` 或整体乱码 → 需要解包 JS 找密钥。

**5.2 看请求参数**

- 只有楼栋/房间号 + token → G1。
- 有 `sign` / `signature` / `signData` / `timestamp` + `nonce` → 大概率 G3，需要确认签名算法。

**5.3 解包小程序找逻辑**（需要时）

PC 微信会把小程序包缓存到本地：

```
%USERPROFILE%\Documents\WeChat Files\Applet\<appid>\
# 新版微信可能在：
%APPDATA%\Tencent\xwechat_files\<wxid>\...\Applet\<appid>\
```

找到 `__APP__.wxapkg`（或 `__APP__.wxpkg`），用开源解包器（`wxappUnpacker`、`unveilr`、`wxapkg` 等）还原出 JS，然后在源码里搜：

```
encryptData   AES   CryptoJS   sign   md5   sha256
/login        token         /balance  /electric  /recharge
```

找到接口路径与签名函数后，把算法和参数抄进 `docs/decisions-dianfei.md` 的 T1/T2。**只分析，不改动小程序文件。**

---

## 6. 验证 token 有效期（决定 G1 还是 G2）

拿到 token 后，隔一段时间用同样的请求重放一次（用真值，别用脱敏版）：

```powershell
curl.exe -i "https://<接口地址>?<参数>" -H "Authorization: <token>" -H "User-Agent: <原样UA>"
```

分别在 **1 小时后、6 小时后、24 小时后**各试一次，记录返回：

| 时间点 | HTTP 状态 | 是否返回余额 | 结论 |
|---|---|---|---|
| 立刻 | 200 | 是 | 参数正确 |
| 1h | | | |
| 6h | | | |
| 24h | | | |

- 24h 仍有效 → G1，人工介入可达每周一次。
- 6h–24h → G2，每天跑一次问题不大，但要处理过期。
- < 6h → 转 G3 / 放弃全自动。

---

## 7. 遇到阻塞怎么办

### 7.1 小程序不产生任何流量

- 确认 `WeChatAppEx.exe` 在运行（任务管理器）。
- 用 Proxifier 强制代理（第 3.2 节）。
- 换 Fiddler，它的系统代理接管更彻底。
- 检查是否被杀软/加速器接管了网络。

### 7.2 出现证书错误 / 连接被重置（证书固定）

微信部分版本对小程序域名做 TLS 证书校验。按顺序尝试：

1. 换 **安卓真机**：手机 Wi-Fi 设代理指向电脑 IP:8080，安装 mitmproxy 证书到「用户证书」，Android 7+ 对用户证书不信任的 app 需要额外处理。
2. **Android 7 以下**的旧手机/模拟器，或已 root 的手机装证书到系统分区。
3. root 设备 + `justtrustme` / `frida` 脚本绕过固定（仅限本人设备）。
4. **iOS + Charles**：iOS 对用户安装的根证书默认信任，成功率较高。

### 7.3 抓到的是加密字段

转到 5.3 解包 JS。若密钥来自微信运行时（如 `wx.getStorageSync('key')` 且值由服务端动态下发），基本判定为 G3。

### 7.4 UA 或 Referer 校验导致 403

把小程序请求的 `User-Agent`、`Referer` 原样复制进配置。注意 PC 微信小程序的 UA 里通常含 `MicroMessenger/8.x` 与 `miniProgram` 字样，缺了会被拒。

---

## 8. 收尾（务必执行）

```powershell
# 关闭系统代理
$k='HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings'
Set-ItemProperty $k ProxyEnable 0
# 或设置 → 网络和 Internet → 代理 → 关闭

# 移除根证书（管理员）
certutil -delstore Root mitmproxy

# 退出代理程序，删除 .mitmproxy 目录（如需）
```

Proxifier 里停用规则或退出程序。原始（未脱敏）抓包文件确认已从项目目录移走。

---

## 9. 完成后要交回的东西

1. `capture/req-balance.http`（脱敏）
2. `capture/resp-balance.json`（脱敏）
3. T1–T6 的结论填入 [decisions-dianfei.md](decisions-dianfei.md)
4. 一句话判定：走 **G1 / G2 / G3 / G4**

之后把样本复制成 `capture/sample_response.json`，执行：

```powershell
python -m rest_electric probe --file capture/sample_response.json
```

解析通过即可开始改 `client.py`。
