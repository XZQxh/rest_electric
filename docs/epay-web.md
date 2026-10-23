# 网页版通道：不经过微信查电费

日期：2026-10-22　　状态：**已在真机浏览器验证通过**（能出余额，充值也正常）

---

## 1. 结论

办事大厅里那个「一卡通 · 电费」页面（`/epay/h5/payele`）看起来坏了——下拉框改不了、房间显示
`hydxyktcs`——但**真正的查询页是另一个 URL**：

```
https://shoucard.shou.edu.cn/epay/h5/eleresult?sysid=1&roomid=12345678&areaid=1&buildid=1
```

直接打开就能看到「房间 12345678 / 剩余电量 23.57度」。

- 表单页之所以"坏"，是因为它的房间下拉只返回一个占位值（`hydxyktcs`），点了查询自然查不到东西；
- 查询页本身是**服务端渲染 HTML**，一个 GET 就够，没有 2 小时 token、没有微信、没有 `code`。

## 2. 链路（抓包实测）

```
GET /epay/h5/eleresult?sysid=1&roomid=12345678&areaid=1&buildid=1
  └─ 未登录 → 302 https://uis.shou.edu.cn/cas/login?service=<epay 的 CAS 回调>
       ├─ GET  /cas/login           ← 登录页，含 hidden execution
       ├─ GET  /cas/jwt/publicKey   ← RSA 公钥（2048 位，PKCS#1 v1.5）
       └─ POST /cas/login           ← username + RSA(password) + execution，无验证码
            └─ 302 /epay/j_spring_cas_security_check?ticket=ST-...
                 └─ 302 回 /epay/h5/eleresult → 200
```

> **密码字段的前缀**：页面 JS 是 `"__RSA__" + encrypt.encrypt(password)`，
> 抓包里该字段 351 字符 = 7（`__RSA__`）+ 344（base64 的 256 字节密文）。
> 少了这 7 个字符，服务端会把密文当明文密码，然后**静默退回登录页**——
> 既不报"密码错误"，`hasErrors` 也是 false。这是最容易踩的坑。

页面里余额的三个位置（任取其一，客户端以第一个为主、第二个兜底）：

```html
<div class="weui-cell__bd"><label class="weui-label">剩余电量</label></div>
<div > 23.57度 </div>
...
<input id="roomdef" left-degree="23.57">
```

房间号也在页面里（`房间 / 12345678`），客户端会校验它和配置里的一致，不一致就拒绝入库。

## 3. 口径

学校 **1 度 = 1 元**，所以「剩余电量 23.57 度」直接按 23.57 元入库，
阈值（余额 < 5 元）、预测、报表全部沿用，不用改任何口径。

> 之前小程序返回的 `data.restElecDegree` 字段名里的 "Degree" 也印证了这一点：它一直就是度数。

## 4. 用法

`config/config.toml`：

```toml
[api]
mode = "epay"          # fixture / http / epay

[epay]
base_url = "https://shoucard.shou.edu.cn"
result_path = "/epay/h5/eleresult"
sysid = "1"
areaid = "1"
buildid = "1"
# 房间号用 [room].room
```

`config/secrets.toml`：

```toml
[epay]
username = "<学号>"
password = "<统一身份认证密码>"
```

然后：

```bash
python -m rest_electric once --dry-run     # 只查一次，不写库不推送
python -m rest_electric once               # 写库 + 按需告警
```

Pi 上由 `rest-electric.timer` 白天 08:07–22:07 每 2 小时跑一次（带 0–10 分钟抖动）。

## 5. 会话与登录

- 第一次查询会自动走 CAS 登录，成功后把 Cookie 存到 `data/epay-cookies.txt`（权限 600，已在 .gitignore 里）；
- 之后每次先带 Cookie 直连，**只有拿不到结果页时才重新登录**——尽量少触发登录，避免把浏览器那边的会话挤掉；
- `CAS 登录失败`（密码错、被锁定、学校新增验证码）会抛 `AuthExpiredError`，推送一条「电费网页端登录失败」并退出码 4。

## 6. 安全与合规

| 项 | 做法 |
|---|---|
| 密码存哪 | 只在本机 `config/secrets.toml`（600），不写日志、不进库、不进 git |
| 会话存哪 | `data/epay-cookies.txt`（600），可随时删掉，下次自动重登 |
| 只读 | **只发 GET 查询 + 一次登录 POST**；充值接口（页面里的 `doNext()` POST）永不调用 |
| 频率 | 白天每 2 小时一次（8 次/天），远低于人工使用强度 |
| 房间 | 只查 `[room].room` 这一个房间，且校验服务端返回的房间号 |

## 7. 失败模式

| 现象 | 原因 | 处理 |
|---|---|---|
| `CAS 登录失败` | 密码错 / 账号锁定 / 新增验证码 | 推送告警，人工处理后重跑 |
| `结果页里找不到「剩余电量」` | 页面结构变了 | 推送告警；改 `epay.py` 里的两个正则即可 |
| `结果页的房间是 X` | 参数或配置不对 | 拒绝入库，避免记错房间 |
| 网页端返回 HTTP 5xx | 学校系统维护 | 沿用 `http.py` 的重试语义（当前是直接报错 + 告警） |
| 登录后仍拿不到结果页 | 权限变化 | 抛 `AuthExpiredError` |

## 8. 验证记录

| 时间 | 结果 |
|---|---|
| 2026-10-22 16:53 | `once --dry-run` → `网页端读数：23.51 度（房间 12345678）` |
| 2026-10-22 16:55 | `once` → 入库 `balance_cents=2351, source=epay`，报表正常 |

踩过的坑（已修）：密码字段必须带 `__RSA__` 前缀，否则服务端**静默**退回登录页，
既不报错、`hasErrors` 也是 `false`——现象看起来像"密码明明是对的却登录失效"。

## 9. 与其它通道的关系

| 通道 | 配置 | 何时用 |
|---|---|---|
| **网页版（本文件）** | `mode = "epay"` | 默认，主力 |
| 小程序接口 | `mode = "http"` | 网页端挂了、但你手上有 2 小时内的 token 时临时用 |
| 手机读屏上报 | `serve-ingest` | 网页端和小程序都挂了的最后兜底（见 [scripts/phone/README.md](../scripts/phone/README.md)） |
