# 认证机制只读分析：token 为什么只能活 2 小时

日期：2026-10-22　　性质：**只读调查**（未改现有代码、未向学校服务器发任何请求）
范围：`POST /openservice/wechatauth/token`、`POST /openservice/miniprogram/queryroominfo`、CAS/SSO 链路

---

## 0. 先回答：GPT 那套说法对不对

**一句话：清单的方向是对的，但它把已经做完的事当成待办，而且有三处前提站不住；照它做不会推翻昨天的结论，只会多花半天。**

| # | GPT 的要求 | 判定 | 说明 |
|---|---|---|---|
| 1 | 查 token 的保存/读取/过期/错误处理 | ✅ 对，且已完成 | 已实现，本次补出 5 个缺口（§2） |
| 2 | 找 `wx.login` / `refreshToken` / `expiretime` | ⚠️ 部分对，位置找错了 | 代码里没有；前端包不在本机；`expiretime` 在**接口响应**里，不在前端（§3） |
| 3 | 判断是兑换接口还是刷新接口 | ✅ 对，能定论 | 兑换接口，5 条证据（§4） |
| 4 | 梳理三类凭证关系 | ✅ 对，能定论 | 但现有文档有一处推断要修正（§5） |
| 5 | 找其他续期接口 | ✅ 对，已完成 | 76 条流量已穷举，没有（§6） |
| 6 | 按可行性排序 | ⚠️ 排序需改 | 把"正常登录流程自动恢复"当成服务器侧独立选项是错的（§7） |
| 7 | 输出报告 | ✅ | 本文件 |

### 三处需要纠正的前提

**(1) 让你去小程序前端资源里找 `wx.login()` / `expiretime`——位置错了。**
`expiretime` 不在前端，在**换 token 的响应体**里（E7）：`"expiretime":"1792608837483"`，毫秒时间戳，约等于 JWT 的 `exp × 1000`。而"JS 里调用了 `wx.login()`"这件事即便看到，也不能推出"服务端能续期"——真正决定可行性的是服务端接口，接口行为已经从抓包看完了。另外，小程序包（wxapkg）不在本机，解包属于对客户端做逆向，既没必要也有合规风险，**建议不做**。

**(2) 它假设可能存在未被发现的"刷新接口"——从协议层面就能排除。**
换 token 的请求体里没有任何旧凭证（只有 `code` + `stuempno`），刷新协议在逻辑上不成立：没有"被刷新的东西"。76 条流量里也没有第二个会下发 token 的接口（§6）。

**(3) 排序里"正常登录流程自动恢复"不是一条独立的服务器侧路线。**
它必然需要一台跑着微信的终端，等价于"手机/PC 辅助"，不能单独成立（§7.2）。

### 它漏掉的两件更要紧的事

- token 响应的 `expiretime` 可以直接用来做本地过期判断，比解析 JWT 更稳；
- 现有调度 **09:07 / 21:07 与 2 小时 TTL 在算术上就不匹配**——这比认证机制更容易见效（§7.3）。

> 结论：GPT 这份清单可以当"验收表"，不适合当"接下来的工作计划"。真正该做的三件事在 §8。

---

## 1. 证据来源与方法

| 编号 | 来源 | 规模 | 用途 |
|---|---|---|---|
| E1 | `D:\EdgeDownload\flows (2)` | 59 条流量，2026-10-22 00:53:44–00:54:12（北京时间），冷启动 | 唯一一次完整的登录→换 token 过程 |
| E2 | `D:\EdgeDownload\flows (1)` | 3 条业务请求 | token 复用期的正常查询 |
| E3 | `D:\EdgeDownload\flows` | 14 条业务请求 | 同上（与 E2 同一个 token） |
| E4 | `data/token-watcher.log` | 3 次成功收 token（01:07:56、01:22:53、07:42:50） | 续期实际在什么时点发生 |
| E5 | `data/rest_electric.log` | 00:47、07:41 两次 403 | 过期后的真实表现 |
| E6 | E1 中 JWT 的声明 | `exp/iat/nbf/iss/aud/jti/sub` | 令牌归属与寿命 |
| E7 | E1 中 token 响应体 | `data` 的 9 个固定字段 | 有没有 refresh 凭证 |
| E8 | E1 中三次认证调用的 code 指纹 | 见 §4.1 | code 是否每次都是新的 |
| E9 | E1 中 token 请求的请求头 | UA / Content-Type / Referer | 有没有会话态 |

**方法**：用 `mitmdump -n -r <flow>` 加载只读插件读取历史抓包（不启动代理、不联网），
只输出**长度、SHA-256 前 10 位、字段名、枚举值**，不输出 token / code / cookie / ticket 原值。
现场证据已固化为 [`scripts/flow-auth-fingerprint.py`](../scripts/flow-auth-fingerprint.py)，输出在 `data/flow-auth-fingerprint.txt`。

> 本次分析**没有**向 `shoucard.shou.edu.cn` 或 `uis.shou.edu.cn` 发起任何请求，也没有执行任何可能改变账号状态的调用。

---

## 2. 任务 1：现有 token 逻辑（保存 / 读取 / 过期 / 错误）

| 环节 | 位置 | 实际行为 |
|---|---|---|
| 存储 | `config/secrets.toml` → `[api].token` | 裸 JWT（295 字符，**不含** `Bearer `）。写入前先 `tomllib.loads` 校验，并备份 `secrets.toml.bak` |
| 写入者 | [`token_watcher.py:128-151`](../scripts/token_watcher.py#L128-L151)、[`import-flow.py:227`](../scripts/import-flow.py#L227)、[`cli.py:355-363`](../src/rest_electric/cli.py#L355-L363) | 插件自动写 / 从 flow 提取 / 手工 `refresh-token --token`；前两者会剥掉 `Bearer ` 前缀 |
| 读取 | [`config.py:69-72`](../src/rest_electric/config.py#L69-L72) → [`client.py:132-150`](../src/rest_electric/client.py#L132-L150) | `Config.secret("api","token")`，拼 `token_prefix + token` 放进 `sw-Authorization` |
| 前缀 | `config/config.toml:54` `token_prefix = "Bearer "` | 末尾空格有语义，`_opt_raw_str` 专门保留空格（少空格会 403） |
| 过期判断 | [`cli.py:366-400`](../src/rest_electric/cli.py#L366-L400) | **只有 `token-status` 在本地解析 JWT**；真正查询前没有任何本地预检 |
| 运行时判定 | [`client.py:171-174`](../src/rest_electric/client.py#L171-L174)、[`client.py:189-205`](../src/rest_electric/client.py#L189-L205) | HTTP 401/403 → `AuthExpiredError`；或 200 响应里出现「未登录/登录失效/…」关键字 |
| 错误处理 | [`cli.py:184-199`](../src/rest_electric/cli.py#L184-L199) | 推送「需要续期」（1 天冷却，见 [`alerts.py:15-20`](../src/rest_electric/alerts.py#L15-L20)）→ **退出码 4** |

实测（E5）：`07:41:34` 403 → 日志记 `登录态失效` → 退出 4，行为与设计一致。

### 5 个缺口（都不是"认证机制"问题，属于工程细节）

| 编号 | 缺口 | 影响 |
|---|---|---|
| G1 | 不读 `expiretime` / `exp` 做预检 | 明知已过期仍发一次注定 403 的请求，白白增加风控暴露 |
| G2 | 业务码从未被识别：判定函数只看 `code`/`errcode`/`status`（[`client.py:195`](../src/rest_electric/client.py#L195)），而这个接口用的是 `retcode` | "此用户未注册"（`retcode=9`）会被当成 `ProtocolError`（"找不到字段 data.restElecDegree"），排查时被误导成协议变更 |
| G3 | `refresh-token --token` 不剥 `Bearer ` | 响应体里的 token **自带** `Bearer `（E7/E9：token 长度 302 = 请求头 302），直接复制粘贴会拼成 `Bearer Bearer eyJ…` → 403。插件和 import-flow 都会剥，只有这条手工命令不会 |
| G4 | 固定 09:07/21:07 与 2h TTL 不匹配 | 见 §7.3，平均一半调度注定失败 |
| G5 | 日志里 `08:23:23`、`08:56:51` 两次只有"开始查询"、没有结果行 | "绝不静默失败"依赖进程正常结束；被中断时不会留下失败记录 |

---

## 3. 任务 2：`wx.login` / `refreshToken` / `expiretime` 到底在哪

- **代码**：`wx.login`、`refreshToken`、`refresh_token`、`expiretime`、`openid` 在 `src/` 与 `scripts/` 里 **0 命中**。项目只做"读 token → 发请求 → 判 403"。
- **前端资源**：仓库内没有任何小程序包；抓包里只有 CAS 登录页 HTML（101 KB）和 `jweixin-1.3.2.js`（微信 JS-SDK，不含换 token 逻辑）。**未解包小程序**，理由见 §0(1)。
- **`expiretime` 确实存在，但在响应体里**（E7）：

  ```
  data = { retcode, retmsg, ssl, ssltype, pwdrule, token, expiretime, type, gzhRedirect }
  expiretime = "1792608837483"   # 毫秒时间戳
  exp        = 1792608837        # JWT，秒；两者相差 483 毫秒
  ```

  即 `expiretime = 签发时刻 + 7200 秒`。它说明客户端被设计成"自己知道何时过期"，**不是**"服务端支持续期"。

---

## 4. 任务 3：`wechatauth/token` 是登录码兑换，还是刷新接口

**判定：一次性登录码兑换接口（高置信）。**

| 方向 | 证据 |
|---|---|
| 支持"兑换" | ① 请求体只有 `{code, stuempno}`，**没有任何旧凭证**——刷新协议在逻辑上不成立 |
| | ② 响应体没有 `refresh_token` 一类字段（E7，`data` 只有 9 个固定键） |
| | ③ 请求头里**没有 Cookie、没有 Authorization**（E9）——接口是无状态的，身份只能来自 `code` |
| | ④ 复用 code → `请求微信平台失败`（[decisions-dianfei.md](decisions-dianfei.md) D12 实测），这只可能来自"拿 code 去微信核验" |
| | ⑤ `{code, stuempno:""}` → `retcode=9 此用户未注册`：先用 code 解析微信身份，再查本地绑定 |
| 存疑 | ⑥ `code` 这个参数名在两处复用：`sso/login?code=`（SSO 事务码）与 token 请求体。没有前端 JS，无法 100% 证实后者由 `wx.login()` 产生 |
| | ⑦ 无法排除"服务端同时接受学校自签 code"这一分支（U1，§9 V1） |

### 4.1 真实时序（E1，全程 13 秒，code 只显示指纹）

| 时刻（北京） | 调用 | code 指纹 | 结果 |
|---|---|---|---|
| 00:53:44 | `POST wechatauth/token {code, stuempno:""}` | `52799572e7` | `retcode=9 此用户未注册` |
| 00:53:46 | `GET sso/login?code=…` | `843e9c131a` | 302 → `uis.shou.edu.cn/cas/login` |
| 00:53:46–56 | CAS 登录（学号+密码+空验证码，RSA 加密） | — | 302 → `/openservice/sso/auth?ticket=…`（下发 TGC） |
| 00:53:56 | `GET sso/auth?ticket=…` | — | 302 → `sso/login?code=…`（**同一个** `843e9c131a`） |
| 00:53:56 | `GET sso/login?code=…` | `843e9c131a` | 200，返回 1.6 KB HTML |
| 00:53:57 | `POST wechatauth/token {code, stuempno:<学号>}` | `ad21fcf60e` | `retcode=0 成功`，下发 JWT |
| 00:53:57 起 | 9 次业务请求（wxlayout/accountinfo/queryroominfo/…） | — | 全部 200，同一 JWT |

两个值得记住的细节：

1. **换到 JWT 用的 code（`ad21…`）既不是第一次失败的 code（`5279…`），也不是 SSO 往返里的 code（`843e…`）。** 也就是说小程序**没有**把 SSO 回调里的 code 直接拿去换 JWT——这削弱了"CAS 会话能换 JWT"的猜想，但没有排除（U1）。
2. `843e…` 在 302 和 200 两次请求里**原样出现**，说明它是 SSO 事务码（相关 ID），不是被消费掉的一次性 code。

---

## 5. 任务 4：微信 code、CAS 会话、业务 JWT 的关系

```
微信客户端 wx.login()
  └─ code（32 位，一次性，分钟级）
       ├─ POST wechatauth/token {code, stuempno}   ← 唯一能造 JWT 的地方
       │      ├─ 未绑定 → retcode 9「此用户未注册」
       │      └─ 已绑定 → JWT（7200 秒）
       └─ GET sso/login?code=… → CAS（学号+密码）→ ticket → sso/auth
              └─ 只在「首次绑定」时走一次，之后可静默复用 CAS 会话
```

| 凭证 | 谁签发 | 寿命 | 能否续期 | 续期需要什么 | 树莓派能否独立完成 |
|---|---|---|---|---|---|
| 微信 `code` | 微信客户端 | 一次性，分钟级 | **不能**（只能重新调 `wx.login()`） | 一台跑着微信的终端 | ❌ |
| CAS 会话（TGC / JSESSIONID / ST） | 学校 CAS | 会话级（TGC 较长） | ✅ 可用学号密码重新登录，或用 TGC 静默 | 学号 + 密码 | ✅ 能拿到，**但换不到 JWT**（实测只带 Cookie 查 queryroominfo → 403） |
| 业务 JWT | 校园卡平台（`iss=com.supwisdom`，E6） | 7200 秒，**无滑动续期** | ❌ | 只能再走一次 code 兑换 | ❌ |
| openid | 微信 | 长期 | — | 只存在于服务端内部，抓包里从未出现 | — |

### 对现有文档的一处修正

[docs/结论与后续.md](结论与后续.md) 写「服务端的用户主键是微信 openid，学号只是绑定在 openid 上的附属信息」。
JWT 的实际声明是 `aud=<学号>`、`iss=com.supwisdom`（E6）。更准确的说法：

> **绑定关系是 openid ↔ 学号；但业务令牌是按学号签发的。**
> 这解释了为什么第二次换 token 必须显式带 `stuempno`——服务端要确认"这个 openid 绑定的就是你这个学号"。

这个修正不改变结论（服务器仍然造不出 code），但影响以后排查：查 403 时不要去找 openid，去看 JWT 的 `aud`/`exp`。

---

## 6. 任务 5：有没有别的续期入口

76 条流量里，`shoucard.shou.edu.cn` 只出现 **10 个端点**（另有 `uis.shou.edu.cn` 的 2 个 CAS 路径）：

| 类别 | 端点 |
|---|---|
| 业务（7） | `miniprogram/wxlayout`、`accountinfo`、`popUpMessage`、`queryroominfo`、`getbanks`、`userKeyConfig`、`offlineUserKeyMeta` |
| 认证（3） | `wechatauth/token`、`sso/login`、`sso/auth` |
| CAS（2，在 uis 域） | `cas/login`（GET 取页面、POST 提交）、`cas/jwt/publicKey` |

- 26 次业务请求**全部只带 `sw-Authorization`**，请求/响应里都没有新 token（E1/E2/E3）。
- 没有任何 `/refresh`、`/renew`、`/reissue`、`/token/refresh` 类路径。
- `token-watcher.log` 里出现过一次 `POST /sso/check`（无响应记录）——语义未知（U3），但从命名和上下文看是会话检查，不是发 token 的接口。

**结论：抓包范围内不存在续期接口。** 注意"不存在"只在这 76 条范围内成立；没有理由假设存在一个对小程序开放、却从未被小程序调用的刷新接口。

---

## 7. 任务 6：方案排序（修正版）

### 7.1 五条路径

| 路径 | 可行性 | 代价 | 能否 7×24 | 依据 |
|---|---|---|---|---|
| 1. 官方支持的续期 | **不存在** | — | — | §6 |
| 2. 服务器自助登录（CAS 自动登录换 JWT） | 仅在 U1 成立时可行；现有证据倾向不成立 | 本机存学号密码；自动提交登录表单 | 若成立则能 | §4.1 细节 1、§9 V1 |
| 3. 常驻微信终端 + 被动收 token | **可行**（PC 微信认系统代理已验证 T4） | 一台常开 PC/安卓；微信改版可能失效 | 接近（仍需"打开小程序"这一动作） | E4：3 次收 token 都紧跟在打开小程序之后 |
| 4. 半自动（现状） | 可行 | 每天手工 1–2 次 | 不能 | [D13](decisions-dianfei.md) |
| 5. 失败提醒 + 人工 | 已实现 | 只保底 | 不能 | [`alerts.py`](../src/rest_electric/alerts.py) |

### 7.2 为什么顺序要改

GPT 把 2 排在 3 前面，隐含"服务器可以自己恢复登录"。证据表明 2 只剩 U1 一条缝，且即便成立也要把密码放进定时任务；而 3 是**已确定可行**的（T4 + E4）。正确顺序：**3 > 2 > 4/5**。

### 7.3 现状里最该修的其实是调度，不是认证

算术：token 在 `07:42` 拿到，`09:42` 过期 → 09:07 的定时查询能成功，**21:07 的必然 403**（除非 19:07 之后又开过小程序）。
即"每天两次"在 2 小时 TTL 下，平均有一半的调度注定失败，还各烧掉一次告警冷却（1 天）。

> 产品定义要改：这不是"每天 9 点报余额"的系统，而是"**有人打开小程序时顺手记一笔**"的系统。
> 查询时机应该跟着 token 走，而不是跟着时钟走。

---

## 8. 推荐架构（按投入分三档）

### A. 零认证改动，今天就能做（建议先做）

1. **调度改为 token 驱动**：`token_watcher` 收到新 token → 立刻跑一次 `once` → 之后每 45–60 分钟跑一次，403 就停并告警。采点密度随"你打开小程序的次数"变化，而不是随时钟。
2. **代理改为"选择性常开"**：`mitmweb --ignore-hosts '^(?!(.*\.)?shou\.edu\.cn$).*$'`，系统代理常开，只解密校园域名，其余流量直通不记录（正则语义先用 V2 验一次）。这样你**正常打开小程序**就完成刷新，不必再跑 `refresh-token.ps1` 那套开/关代理的仪式。
3. **加预检**：用 `expiretime`（或 `exp`）判断，过期就不发请求，直接推"该开小程序了"。
4. **告警冷却 1 天 → 6 小时**：2 小时 TTL 下，1 天冷却等于"今天你已经没救了"。

失效点：微信哪天不认系统代理了（现在认，T4）；mitmweb 常驻的稳定性；证书过期。
风险：系统代理指向 mitmweb，它挂了整机没网 → 加看门狗，或改用 PAC 只代理 `*.shou.edu.cn`（影响面更小）。

### B. 一周内：把代理挪到树莓派

- Pi 上跑 mitmproxy，PC 的系统代理指向 Pi 的 IP；新 token 直接落进 Pi 的 `secrets.toml`，省掉"复制/同步"。
- 更稳的变体：Pi 提供 PAC，只代理 `*.shou.edu.cn`。
- 前提：PC 与 Pi 同网段；PC 关机时无法刷新（可接受）。

### C. 真正的 7×24：需要一台设备

- 闲置安卓手机/平板长期插电，用无障碍/ADB 定时打开小程序，**读屏取"余额"这个数字**，POST 给 Pi。
- 不碰 token、不碰证书、不逆向；缺点是设备成本 + 微信 UI 改版要修。
- 这才是"无人值守"，但它解决的问题是"读屏"，不是"续 token"。

### 明确不做

伪造/复用 code、自己签 JWT、把 CAS 密码塞进定时任务去刷（除非 U1 验证通过且你接受合规风险）。

---

## 9. 下一步验证（全部只读 / 不改变账号状态）

| 编号 | 验证什么 | 怎么做 | 通过/不通过的含义 |
|---|---|---|---|
| **V1** | 学校自签 code 能否换 JWT（U1） | 用一个**新的** CAS 会话拿到 `sso/login?code=…` 里的新 code，用它 POST 一次 `wechatauth/token`（带学号） | 返回 `请求微信平台失败` → 服务器侧彻底死路；返回 token → "密码换 JWT"成立，7×24 有解 |
| V2 | 微信认不认 PAC / `--ignore-hosts` | 设 PAC 只代理 `*.shou.edu.cn`，打开小程序看 mitmproxy 有没有收到请求 | 收到 → 方案 A2 可用 |
| V3 | `/sso/check` 的语义 | 只观察日志，不主动请求 | 若它下发 token 才需要重新评估（目前无迹象） |
| V4 | 学校有没有网页端电费页 | 人工浏览器登录办事大厅/后勤/能源平台，找"寝室余额" | 有 → 树莓派方案复活（CAS 可程序化）；没有 → 死心 |
| V5 | 把业务码整理进判定表 | 记录 `retcode` 全表（已知 0=成功、9=未注册） | 后续改 `client.py` 时用 |

> **V1 我本次没有执行**：它需要向学校服务器发起登录与换 token 请求，超出"只读调查"的边界，而且需要你决定"是否允许脚本代你登录"。
> 如果 V1 也失败（按现有证据，这是大概率），那么"服务器侧自助续期"这条路就可以正式关闭，不用再花时间。

---

## 10. 未知事项（明确标未知，不猜）

| 编号 | 未知 | 现有证据倾向 |
|---|---|---|
| U1 | `wechatauth/token` 是否接受学校自签 code | 倾向"只接受微信 code"：无会话态、复用即失败、成功那次用的是全新的第三个 code |
| U2 | `sso/login?code=` 的 code 由谁生成（小程序 wx.login 还是后端） | 未知。它在 CAS 往返中原样复用，更像事务码 |
| U3 | `POST /sso/check` 的语义 | 未知，仅出现 1 次 |
| U4 | 服务端 token TTL 是否可配置（2h 是否软限制） | 未知；已解出的 JWT 与 3 次落盘日志都显示 7200 秒 / 还剩 120 分钟 |
| U5 | 学校对脚本化访问的明文态度 | 未知；本项目按"只查本人、只读、低频"自我约束 |
| U6 | 是否存在网页端电费查询 | 未知（办事大厅没有，其它平台未查） |

---

## 11. 合规红线（本项目内继续有效）

1. 不伪造、不复用、不猜测微信 code；不自己签发 JWT。
2. 不复用他人凭证，不查询他人房间，不做房间枚举。
3. 只调只读接口，绝不触碰充值/支付。
4. 凭证只留在本机 `config/secrets.toml`，不进日志、不进库、不进 git。
5. 抓包只用于本人账号的协议分析；分析产物只保留长度/哈希/字段名。

---

## 附录 A：复现方式

```powershell
# 只读分析：不启动代理，不联网
& "D:\Program Files\mitmproxy\bin\mitmdump.exe" -n -r "D:\EdgeDownload\flows (2)" `
    -s scripts\flow-auth-fingerprint.py
Get-Content data\flow-auth-fingerprint.txt
```

输出里只会有 `sha256:xxxxxxxxxx`、长度、`retcode`/`retmsg`、字段名。

## 附录 B：本次改动

| 文件 | 类型 | 说明 |
|---|---|---|
| `docs/token-auth-analysis.md` | 新增 | 本报告 |
| `scripts/flow-auth-fingerprint.py` | 新增 | 只读分析插件，不参与运行 |
| `data/flow-auth-fingerprint.txt` | 新增（运行时产物，`data/` 已在 .gitignore） | 指纹输出 |

**没有修改任何现有代码或配置文件。**
