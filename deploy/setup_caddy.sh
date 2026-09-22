#!/usr/bin/env bash
# Идемпотентная настройка Caddy как HTTPS-прокси перед мини-аппом.
# Домен — бесплатный wildcard-сервис nip.io, резолвящий IP сервера без
# покупки настоящего домена: 104-171-131-24.nip.io -> 104.171.131.24.
set -euo pipefail

DOMAIN="104-171-131-24.nip.io"
PROJECT_DIR="/root/onlineambot"

"${PROJECT_DIR}/.venv/bin/pip" install -q -r "${PROJECT_DIR}/requirements.txt"

if ! command -v caddy >/dev/null 2>&1; then
    echo "Устанавливаю Caddy..."
    apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https curl gnupg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
        | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
        > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -qq
    apt-get install -y -qq caddy
else
    echo "Caddy уже установлен."
fi

cat > /etc/caddy/Caddyfile <<EOF
${DOMAIN} {
    reverse_proxy localhost:8080
}
EOF

systemctl enable --now caddy
systemctl reload caddy

WEBAPP_URL="https://${DOMAIN}"
if grep -q '^WEBAPP_URL=' "${PROJECT_DIR}/.env" 2>/dev/null; then
    sed -i "s#^WEBAPP_URL=.*#WEBAPP_URL=${WEBAPP_URL}#" "${PROJECT_DIR}/.env"
else
    echo "WEBAPP_URL=${WEBAPP_URL}" >> "${PROJECT_DIR}/.env"
fi

systemctl restart onlineambot

echo
echo "Готово. Мини-апп должен быть доступен на ${WEBAPP_URL}"
echo "--- caddy status ---"
systemctl status caddy --no-pager | head -6
echo "--- onlineambot log ---"
sleep 2
journalctl -u onlineambot --no-pager -n 10
