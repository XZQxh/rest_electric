# 数据模型

数据库：`data/electric.db`（SQLite 3，WAL 模式）。所有金额字段均为**整数分**，展示层除以 100。

---

## 表结构

### `readings` — 每次查询的原始读数

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | 自增 |
| `room_key` | TEXT NOT NULL | 房间唯一标识，如 `A-305`；仅作分区字段 |
| `balance_cents` | INTEGER NOT NULL | 余额（分）。允许 0，允许负数（欠费） |
| `source` | TEXT NOT NULL | 数据来源标记，如 `api` / `fixture` / `manual` |
| `raw_json` | TEXT | 原始响应（已脱敏），用于协议变更时回溯 |
| `captured_at` | TEXT NOT NULL | ISO 8601 带时区，如 `2026-10-21T09:07:12+08:00` |

唯一约束：`UNIQUE(room_key, captured_at)` — 同一时间点重跑不会重复插入。

索引：`idx_readings_room_time(room_key, captured_at)`。

### `alerts_sent` — 告警去重

| 字段 | 类型 | 说明 |
|---|---|---|
| `room_key` | TEXT NOT NULL | |
| `level` | TEXT NOT NULL | `low_balance` / `predicted_low` / `auth_expired` / `fetch_failed` |
| `day` | TEXT NOT NULL | 本地日期 `YYYY-MM-DD` |
| `sent_at` | TEXT NOT NULL | 实际发送时间 |
| `detail` | TEXT | 当时的余额/剩余天数快照 |

唯一约束：`UNIQUE(room_key, level, day)` — 同一天同一级别只推一次。

### `meta` — 键值杂项

用于记录 `schema_version`、`last_success_at`、`last_error` 等，供 `report` 与 `selftest` 使用。

---

## 派生数据（不落库，运行时计算）

按天取**当天最后一条**读数得到序列 `[(day, balance_cents), ...]`，然后：

```
相邻两点 (d0, b0) → (d1, b1)
  gap   = (d1 - d0).days            # 必须 > 0
  delta = b0 - b1                   # 余额下降量
  若 delta > 0  → 用电，日均样本 = delta / gap
  若 delta === 0 → 用电 0，样本 = 0
  若 delta < 0  → 充值/退费，整段丢弃，不计入样本
```

`daily_cents = median(样本)`，`days_left = last_balance / daily_cents`。

**为什么用中位数**：单日（比如开了一天空调）的异常值会把均值拉偏，中位数不受影响。

**为什么丢弃充值段**：余额上升那天的「负数消耗」如果参与计算，会显著低估日均、把预测推后，导致漏报。

---

## 金额与精度

- 入口解析：接口若返回 `"12.34"`，用 `Decimal(str)` 转分：`int(Decimal("12.34") * 100)`，不要用 `float`。
- 出口展示：`f"{cents / 100:.2f}"`（仅展示，不参与比较）。
- 阈值比较一律用分：`balance_cents < config.balance_floor_cents`。

---

## 备份

```powershell
# 热备份（WAL 模式下安全）
cp data\electric.db data\electric-$(Get-Date -Format yyyyMMdd).db
```

树莓派上建议加一条每周的 cron：

```bash
sqlite3 /opt/rest_electric/data/electric.db ".backup '/opt/rest_electric/data/backup-$(date +%F).db'"
```

保留最近 8 份即可，数据量极小（每天 2 行）。

---

## 数据保留

- `readings`：永久保留，一年不到 1000 行。
- `raw_json`：单条约 1–2 KB，一年约 1 MB，可永久保留；若响应很大可在 `config.toml` 关掉 `storage.keep_raw`。
- `alerts_sent`：永久保留，用于去重与统计。

---

## 隐私

库里有房间号，没有姓名、学号、密码、token。token 只在 `config/secrets.toml`。备份文件同样不要外发。
