#!/usr/bin/env bash
# 把项目部署到树莓派：网页版查询定时器 +（可选的）手机上报接收器。
#
#   sudo bash scripts/deploy-pi.sh
#   sudo bash scripts/deploy-pi.sh --prefix /opt/rest_electric --user restelec
#
# 特性：不覆盖已有的 config/config.toml 与 config/secrets.toml。
# 主通道是 `once`（api.mode = "epay"，走网页版，不需要微信与 token），
# 由 rest-electric.timer 每天跑 3 次；手机上报接收器作为备用通道常驻。

set -euo pipefail

PREFIX="/opt/rest_electric"
RUN_USER="restelec"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --prefix) PREFIX="$2"; shift 2 ;;
        --user)   RUN_USER="$2"; shift 2 ;;
        -h|--help)
            sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) echo "未知参数：$1" >&2; exit 3 ;;
    esac
done

if [[ "${EUID}" -ne 0 ]]; then
    echo "请使用 sudo 运行：sudo bash scripts/deploy-pi.sh" >&2
    exit 3
fi

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ---- 前置检查 -------------------------------------------------------------

if ! command -v python3 >/dev/null 2>&1; then
    echo "未找到 python3，请先安装：sudo apt install -y python3" >&2
    exit 3
fi

if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    echo "需要 Python 3.11+（tomllib 在标准库中），当前：$(python3 -V)"
    echo "Raspberry Pi OS Bookworm 自带 3.11；Bullseye 是 3.9，请升级系统。"
    exit 3
fi

# ---- 用户与目录 -----------------------------------------------------------

if ! id -u "${RUN_USER}" >/dev/null 2>&1; then
    echo "创建系统用户 ${RUN_USER}"
    useradd --system --home-dir "${PREFIX}" --shell /usr/sbin/nologin "${RUN_USER}"
fi

echo "安装到 ${PREFIX}"
mkdir -p "${PREFIX}"/{src,config,data,scripts/systemd,scripts/phone,logs}

rm -rf "${PREFIX:?}/src/rest_electric"
cp -a "${SRC}/src/." "${PREFIX}/src/"
cp -a "${SRC}/scripts/systemd/." "${PREFIX}/scripts/systemd/"
cp -a "${SRC}/scripts/phone/." "${PREFIX}/scripts/phone/"
cp -a "${SRC}/docs" "${PREFIX}/"
cp -a "${SRC}/README.md" "${PREFIX}/"
cp -a "${SRC}/config/config.example.toml" "${PREFIX}/config/"
cp -a "${SRC}/config/secrets.example.toml" "${PREFIX}/config/"

if [[ ! -f "${PREFIX}/config/config.toml" ]]; then
    cp "${PREFIX}/config/config.example.toml" "${PREFIX}/config/config.toml"
    echo "已生成 config/config.toml（首次部署）"
fi
if [[ ! -f "${PREFIX}/config/secrets.toml" ]]; then
    cp "${PREFIX}/config/secrets.example.toml" "${PREFIX}/config/secrets.toml"
    chmod 600 "${PREFIX}/config/secrets.toml"
    echo "已生成 config/secrets.toml，请填入 token 与推送密钥"
fi

chown -R "${RUN_USER}:${RUN_USER}" "${PREFIX}"
chmod 700 "${PREFIX}/config"
chmod 600 "${PREFIX}/config/secrets.toml" || true

# ---- systemd --------------------------------------------------------------

echo "安装 systemd 单元"
sed "s#/opt/rest_electric#${PREFIX}#g; s#User=restelec#User=${RUN_USER}#; s#Group=restelec#Group=${RUN_USER}#" \
    "${SRC}/scripts/systemd/rest-electric.service" > /etc/systemd/system/rest-electric.service
sed "s#/opt/rest_electric#${PREFIX}#g; s#User=restelec#User=${RUN_USER}#; s#Group=restelec#Group=${RUN_USER}#" \
    "${SRC}/scripts/systemd/rest-electric-ingest.service" > /etc/systemd/system/rest-electric-ingest.service
sed "s#/opt/rest_electric#${PREFIX}#g; s#User=restelec#User=${RUN_USER}#; s#Group=restelec#Group=${RUN_USER}#" \
    "${SRC}/scripts/systemd/rest-electric-stale.service" > /etc/systemd/system/rest-electric-stale.service
cp "${SRC}/scripts/systemd/rest-electric.timer" /etc/systemd/system/rest-electric.timer
cp "${SRC}/scripts/systemd/rest-electric-stale.timer" /etc/systemd/system/rest-electric-stale.timer

systemctl daemon-reload

# 主通道：网页版查询（api.mode = "epay"），每天 3 次
systemctl enable rest-electric.timer
systemctl restart rest-electric.timer

# 备用通道：手机上报接收器（常驻；不用手机时可以不管它）
systemctl enable rest-electric-ingest.service
systemctl restart rest-electric-ingest.service

# 采集端离线检查（每 6 小时）
systemctl enable rest-electric-stale.timer
systemctl restart rest-electric-stale.timer

# 接收器没有密钥会启动失败，提前提醒
if ! PYTHONPATH="${PREFIX}/src" python3 -B -c "
import sys
from rest_electric.config import load_config
cfg = load_config('${PREFIX}/config/config.toml', '${PREFIX}/config/secrets.toml')
sys.exit(0 if cfg.secret('ingest', 'key') else 1)
"; then
    echo "警告：config/secrets.toml 里还没有 [ingest].key，接收器会启动失败。" >&2
    echo "      生成：python3 -c \"import secrets;print(secrets.token_hex(16))\"" >&2
fi

# ---- 收尾提示 -------------------------------------------------------------

cat <<EOF

部署完成。

下一步：
  1. 编辑配置：sudo -u ${RUN_USER} nano ${PREFIX}/config/config.toml
  2. 填入密钥：sudo nano ${PREFIX}/config/secrets.toml   # [ingest].key / [notify].sendkey
  3. 自检：    cd ${PREFIX} && sudo -u ${RUN_USER} PYTHONPATH=${PREFIX}/src python3 -m rest_electric selftest
  4. 看接收器：systemctl status rest-electric-ingest.service
               journalctl -u rest-electric-ingest.service -n 50
  5. 本机验证上报（KEY 取自 config/secrets.toml 的 [ingest].key）：
       curl -s -X POST http://127.0.0.1:8787/ingest \
         -H 'X-Ingest-Key: <KEY>' -H 'Content-Type: application/json' \
         -d '{"balance":"24.03","source":"phone"}'
  6. 看定时器：systemctl list-timers 'rest-electric*'

注意：secrets.toml 属主应为 ${RUN_USER} 且权限 600，否则服务读不到密钥。
      手机脚本里的地址填这台机器的局域网 IP（见 docs/phone-automation.md）。
EOF
