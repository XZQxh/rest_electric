# mitmweb 上手速查（给第一次用的人）

## 先搞清两个端口

mitmweb 同时开两样东西，新手 90% 的困惑来自把它们搞混：

| 端口 | 是什么 | 你要做什么 |
|---|---|---|
| `8081` | 浏览器里那个**控制台界面**，只是用来看结果 | 用浏览器打开 `http://127.0.0.1:8081` |
| `8080` | 真正的**代理管道** | 让浏览器/微信把流量送进来（开系统代理） |

**「界面打开了但什么都没有」= 流量没送到 8080**，不是界面坏了。

---

## 第 1 步：打开系统代理（必须用你自己的终端）

我这个会话被沙盒限制，改不了系统网络设置，所以要你自己开一个 PowerShell 窗口：

开始菜单搜 **PowerShell** → 打开 → 粘贴：

```powershell
cd D:\DSHProject\rest_electric
.\scripts\proxy-on.ps1
```

如果提示"禁止运行脚本"，用这条（只对本次生效）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\proxy-on.ps1
```

> 这几个 `.ps1` 都是 **UTF-8 带 BOM** 保存的。如果你以后用编辑器改过它们，
> 一定要仍然保存为「UTF-8 with BOM」，否则 Windows PowerShell 5.1 会按 GBK 读中文注释，
> 报出莫名其妙的「意外的标记 }」。

它会先备份你原来的代理设置到 `data\proxy-state.json`，再指向 `127.0.0.1:8080`。

> ⚠️ **重要**：从这一刻起，你的上网要经过 mitmproxy。
> **关掉 mitmweb 的窗口 = 代理断掉 = 上不了网。** 收工时执行 `.\scripts\proxy-off.ps1` 恢复。
> 如果忘了，也可以用 `设置 → 网络和 Internet → 代理 → 关闭`。

---

## 第 2 步：确认真的通了（不要用 example.com）

浏览器新开标签，访问：

```
http://mitm.it
```

- 看到 mitmproxy 的证书下载页面（有 Apple / Windows / Android 图标）→ **通了**
- 打不开、转圈 → 代理没生效，回到第 1 步

再去 mitmweb 界面（8081），左侧列表应该立刻出现一堆条目。

再访问一个 https 网站，比如 `https://www.baidu.com`，列表里应该出现 `www.baidu.com` 的条目。

---

## 第 3 步：看懂界面三块区域

```
┌───────────── 顶部：过滤器 ─────────────────────────┐
│  ~u shou        ← 输入关键字只显示匹配的请求         │
├───────────────┬───────────────────────────────────┤
│  左侧：Flow 列表 │  右侧：选中某条后的详情            │
│  每行 = 一次请求 │  ┌─────────────────────────────┐  │
│  GET  www...   │  │ Headers | Request | Response│  │
│  POST xxx      │  │ Message Body  ...           │  │
│                │  └─────────────────────────────┘  │
└───────────────┴───────────────────────────────────┘
```

- **左侧列表**：一次 HTTP 请求一行。没有行 = 没有流量进来。
- **点某一行** → 右侧显示详情。
- **Headers**：请求头/响应头，token 和 Cookie 就在这里。
- **Request**：请求带了什么参数（楼栋号、房间号）。
- **Response**：服务器返回什么（**余额就在这里**）。
- **过滤器**：输入 `~u shou` 只看 URL 含 shou 的；`~u qq` 看腾讯的。

---

## 第 4 步：抓微信小程序

顺序很重要，**先开代理、再重启微信**，否则微信还在用旧网络栈：

1. 确认 `proxy-on.ps1` 已经跑过、`mitm.it` 能打开。
2. 托盘右键微信 → **退出**（要完全退出，关窗口不算）。
3. 重新登录微信。
4. 打开「上海海洋大学一卡通」小程序。
5. 在 mitmweb 里看左侧有没有新流量（大概率能看到 `servicewechat.com`、`qq.com`）。
6. 进入电费查询 → 选楼栋 → 选房间 → 点查询。
7. 回到 mitmweb，按时间找最后几条请求，逐个点开看 **Response** 里有没有一个金额。

### 如果完全没有微信的流量

微信不认系统代理，这时需要 **Proxifier**（试用 31 天足够）：

1. 装好打开 → `Profile → Proxy Servers → Add`
   - Address: `127.0.0.1`，Port: `8080`，Protocol: `HTTP`，勾选 HTTPS
2. `Profile → Proxification Rules → Add`
   - Name: `wechat`
   - Applications: `WeChatAppEx.exe;WeChat.exe`
   - Action: 选刚才那个代理
   - 把这条规则拖到 `Default` 上面
3. 再退出微信、重开、重试。

还不行就换手机方案：手机 Wi-Fi 设代理指向电脑的 IP:8080，电脑上 `ipconfig` 查 IPv4 地址，手机装 mitmproxy 证书。

---

## 第 5 步：把结果留给我

**方式一（推荐，最完整）**：mitmweb 菜单 `File → Save → All flows`，存成：

```
D:\DSHProject\rest_electric\capture\raw.flow
```

这个文件里有明文 token，**只放本机，不要发给任何人**。我会用脚本在本地读取并脱敏后再分析。

**方式二（最省事）**：在目标请求上右键 → `Copy → Copy as cURL`，自己粘贴到记事本先存着。

---

## 出问题对照表

| 现象 | 原因 | 处理 |
|---|---|---|
| 左侧列表一直空 | 系统代理没开 / 代理端口不是 8080 | 跑 `proxy-on.ps1`，确认 `mitm.it` 能开 |
| 网页打不开，提示证书错误 | 根证书没被信任 | 访问 `http://mitm.it` 重新装证书；或确认装在**当前用户 → 受信任的根证书颁发机构** |
| 浏览器正常但微信没流量 | 微信绕过系统代理 | 上 Proxifier（第 4 步） |
| 某条请求显示红色/报错 | 证书固定或连接失败 | 点开看错误信息，把内容告诉我就行 |
| 突然全部网站打不开 | mitmweb 被关了 | 重开 mitmweb，或先 `proxy-off.ps1` 恢复上网 |
| 关代理后还是上不了网 | 设置没还原 | `.\scripts\proxy-off.ps1`；失效则手动关系统代理 |

---

## 收尾（一定要做）

```powershell
.\scripts\proxy-off.ps1
```

然后卸载证书（可选，不卸也不影响）：

```powershell
# 管理员 PowerShell
certutil -delstore Root mitmproxy
```
