# 手机采集端脚本（可选）

网页版通道（`api.mode = "epay"`）是默认方案，**不需要这个目录**。
只有当学校网页版也查不了、而你愿意用一台常开的安卓机读屏兜底时，才用这里的脚本。

两个引擎，二选一：

| 文件 | 跑在哪 | 说明 |
|---|---|---|
| `adb_dump.py` | 树莓派 | 用 `adb shell uiautomator dump` 读界面，纯标准库，手机上不用装东西 |
| `electric_auto.js` | 手机（AutoX.js） | 侧载 App，手机自己定时跑，不依赖树莓派在线 |

两个脚本都把读数 POST 给 `rest-electric serve-ingest`，落库、预测、告警与 `once` 完全一致。

快速校准（任何改动前先做这一步）：

```bash
python3 adb_dump.py --calibrate     # 打印当前屏幕上的所有文字与坐标
```

在 `config/config.toml` 里配好 `[ingest]`（监听地址、允许网段、`stale_hours`），
在 `config/secrets.toml` 里生成一个 16 位以上的 `[ingest].key`，手机脚本里填同一串。

> 脚本只读屏，不抓包、不碰 token、不需要 root。页面文字选择器要按你的手机实际界面校准。
> 自动操作第三方客户端可能违反其服务条款，请自行评估，仅用于你自己的账号与你自己的宿舍。
