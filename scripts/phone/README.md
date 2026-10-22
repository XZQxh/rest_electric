# 手机采集端脚本

两个引擎，二选一（详见 [../../docs/phone-automation.md](../../docs/phone-automation.md)）：

| 文件 | 跑在哪 | 说明 |
|---|---|---|
| `adb_dump.py` | 树莓派 | 用 `adb shell uiautomator dump` 读界面，纯标准库，手机上不用装东西 |
| `electric_auto.js` | 手机（AutoX.js） | 侧载 App，手机自己定时跑，不依赖树莓派在线 |

快速校准（任何改动前先做这一步）：

```bash
python3 adb_dump.py --calibrate     # 打印当前屏幕上的所有文字与坐标
```

脚本只读屏，不抓包、不碰 token、不需要 root。
页面文字选择器按你的手机实际界面校准后再开定时。
