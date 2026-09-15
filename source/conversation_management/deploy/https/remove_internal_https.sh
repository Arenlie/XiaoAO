#!/usr/bin/env bash
set -Eeuo pipefail
CONF_NAME="conversation-management-https.conf"
[[ "${EUID:-$(id -u)}" -eq 0 ]] || { echo "请使用 sudo" >&2; exit 1; }
removed=0
for f in "/etc/nginx/conf.d/$CONF_NAME" "/etc/nginx/sites-enabled/$CONF_NAME" "/etc/nginx/sites-available/$CONF_NAME"; do
  if [[ -e "$f" || -L "$f" ]]; then rm -f "$f"; echo "removed $f"; removed=1; fi
done
nginx -t
systemctl reload nginx 2>/dev/null || nginx -s reload
[[ "$removed" -eq 1 ]] || echo "未发现 R20 HTTPS Nginx 配置"
echo "证书和内部 CA 未删除，仍保留在 /etc/nginx/ssl/conversation-management，避免误删根 CA 私钥。"
