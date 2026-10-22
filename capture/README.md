# capture/

抓包样本存放目录。**根目录下除本文件外的内容都被 `.gitignore` 排除**，防止 token 与个人信息误提交。

## 命名约定

| 文件 | 内容 |
|---|---|
| `req-balance.http` | 一次电费查询的完整请求（方法、URL、请求头、body），已脱敏 |
| `resp-balance.json` | 对应响应体原文，已脱敏 |
| `sample_response.json` | 给 `probe` 和测试用的响应样本，字段结构真实、值可伪造 |
| `raw.flow` | mitmproxy 原始流文件，**只存本机，切勿外发** |

## 脱敏清单

提交任何样本前，把下列值替换为占位符：

- `token` / `Authorization` / `Cookie` → `REDACTED`
- `openid` / `unionid` / `session_key` → `REDACTED`
- 学号、姓名、手机号 → `REDACTED`
- 房间号可保留（不影响协议分析），若在意可替换为 `A-305`

## 用法

```powershell
# 用响应样本验证解析逻辑（不联网、不写库）
python -m rest_electric probe --file capture/sample_response.json --field data.balance
```

`--field` 指定金额字段在响应里的路径（点号分隔，支持列表下标如 `data.records.0.balance`）。确定后写进 `config.toml` 的 `api.balance_field`；`--unit` 可临时覆盖元/分。
