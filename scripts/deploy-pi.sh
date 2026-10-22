#!/usr/bin/env bash
# 把项目部署到树莓派并安装 systemd 定时器。
#
#   sudo bash scripts/deploy-pi.sh
#   sudo bash scripts/deploy-pi.sh --prefix /opt/rest_electric --user restelec
#
# 特性：不覆盖已有的 config/config.toml 与 config/secrets.toml。

set -euo pipefail

PREFIX="/opt/rest_electric"
RUN_USER="restelec"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --prefix) PREFIX="$2"; shift 2 ;;
        --user)   RUN_USER="$2"; shift 2 ;;
        -h|--help)
            sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
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
mkdir -p "${PREFIX}"/{src,config,data,scripts/systemd,logs}

rm -rf "${PREFIX:?}/src/rest_electric"
cp -a "${SRC}/src/." "${PREFIX}/src/"
cp -a "${SRC}/scripts/systemd/." "${PREFIX}/scripts/systemd/"
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
cp "${SRC}/scripts/systemd/rest-electric.timer" /etc/systemd/system/rest-electric.timer

systemctl daemon-reload
systemctl enable rest-electric.timer
systemctl restart rest-electric.timer

# ---- 收尾提示 -------------------------------------------------------------

cat <<EOF

部署完成。

下一步：
  1. 编辑配置：sudo -u ${RUN_USER} nano ${PREFIX}/config/config.toml
  2. 填入密钥：sudo nano ${PREFIX}/config/secrets.toml   # token / sendkey
  3. 自检：    cd ${PREFIX} && sudo -u ${RUN_USER} PYTHONPATH=${PREFIX}/src python3 -m rest_electric selftest
  4. 试跑：    sudo systemctl start rest-electric.service && journalctl -u rest-electric.service -n 50
  5. 看定时器：systemctl list-timers rest-electric.timer

注意：secrets.toml 属主应为 ${RUN_USER} 且权限 600，否则服务读不到 token。
EOF
