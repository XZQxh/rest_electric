# capture/

协议样本目录。只放**脱敏后**的响应样本；原始抓包（`*.flow`、`*.har`、`*.txt`、`*.json`）
被 `.gitignore` 排除，避免把 token 与个人信息带进仓库。

| 文件 | 用途 |
|---|---|
| `sample_response.json` | `probe` 命令和 `api.mode = "fixture"` 用的解析样本。字段结构与真实响应一致，值已替换为占位值 |

```powershell
# 用样本验证解析逻辑（不联网、不写库）
rest-electric probe --file capture/sample_response.json

# 完全离线跑一次：把 config.toml 里 api.mode 改成 "fixture"
rest-electric once --dry-run
```

提交任何样本前，请把 `token` / `Authorization` / `Cookie` / `openid` / 学号 / 姓名 / 手机号
替换为占位符。
