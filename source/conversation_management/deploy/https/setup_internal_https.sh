#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="${APP_DIR:-/home/chaos/program/conversation_management}"
HTTPS_HOST="${1:-${HTTPS_HOST:-}}"
HTTPS_PORT="${2:-${HTTPS_PORT:-8599}}"
HTTPS_EXTRA_SANS="${HTTPS_EXTRA_SANS:-}"
UPSTREAM="${CONVERSATION_UPSTREAM:-127.0.0.1:8032}"
CERT_DIR="${CERT_DIR:-/etc/nginx/ssl/conversation-management}"
CONF_NAME="conversation-management-https.conf"

log(){ printf '\n[%s] %s\n' "$(date '+%F %T')" "$*"; }
fail(){ echo "[ERROR] $*" >&2; exit 1; }
[[ "${EUID:-$(id -u)}" -eq 0 ]] || fail "请使用 sudo bash setup_https.sh [访问IP或域名] [HTTPS端口]"
command -v nginx >/dev/null 2>&1 || fail "未找到 nginx，请先安装并启动 Nginx"
command -v openssl >/dev/null 2>&1 || fail "未找到 openssl"

if [[ -z "$HTTPS_HOST" ]]; then
  HTTPS_HOST="$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' | head -1 || true)"
fi
[[ -n "$HTTPS_HOST" ]] || fail "无法自动识别访问地址，请执行: sudo bash setup_https.sh 10.x.x.x 8599"

if [[ -d /etc/nginx/conf.d ]]; then
  NGINX_CONF="/etc/nginx/conf.d/$CONF_NAME"
elif [[ -d /etc/nginx/sites-available ]]; then
  NGINX_CONF="/etc/nginx/sites-available/$CONF_NAME"
else
  fail "无法识别 Nginx 配置目录"
fi

mkdir -p "$CERT_DIR"
chmod 0700 "$CERT_DIR"
CA_KEY="$CERT_DIR/conversation-root-ca.key"
CA_CERT="$CERT_DIR/conversation-root-ca.crt"
SERVER_KEY="$CERT_DIR/conversation-server.key"
SERVER_CSR="$CERT_DIR/conversation-server.csr"
SERVER_CERT="$CERT_DIR/conversation-server.crt"
OPENSSL_CNF="$CERT_DIR/server-openssl.cnf"

if [[ ! -f "$CA_KEY" || ! -f "$CA_CERT" ]]; then
  log "生成内部根 CA（仅首次）"
  openssl genrsa -out "$CA_KEY" 4096 >/dev/null 2>&1
  openssl req -x509 -new -sha256 -days 3650 \
    -key "$CA_KEY" \
    -subj "/C=CN/O=PHM Internal/CN=PHM Conversation Internal Root CA" \
    -out "$CA_CERT"
  chmod 0600 "$CA_KEY"
  chmod 0644 "$CA_CERT"
else
  log "复用已有内部根 CA"
fi

is_ipv4(){ [[ "$1" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; }
{
  cat <<EOF
[req]
prompt = no
distinguished_name = dn
req_extensions = req_ext

[dn]
C = CN
O = PHM Internal
CN = $HTTPS_HOST

[req_ext]
subjectAltName = @alt_names

[v3_ext]
subjectAltName = @alt_names
basicConstraints = CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth

[alt_names]
EOF
  dns_i=1; ip_i=1
  add_san(){
    local v="$1"
    [[ -n "$v" ]] || return 0
    if is_ipv4 "$v"; then
      echo "IP.${ip_i} = $v"; ip_i=$((ip_i+1))
    else
      echo "DNS.${dns_i} = $v"; dns_i=$((dns_i+1))
    fi
  }
  add_san "$HTTPS_HOST"
  add_san "localhost"
  IFS=',' read -ra extras <<< "$HTTPS_EXTRA_SANS"
  for v in "${extras[@]}"; do
    v="$(echo "$v" | xargs)"
    add_san "$v"
  done
} > "$OPENSSL_CNF"

log "签发服务器证书，SAN=$HTTPS_HOST${HTTPS_EXTRA_SANS:+,$HTTPS_EXTRA_SANS}"
openssl genrsa -out "$SERVER_KEY" 2048 >/dev/null 2>&1
openssl req -new -key "$SERVER_KEY" -out "$SERVER_CSR" -config "$OPENSSL_CNF"
openssl x509 -req -sha256 -days 825 \
  -in "$SERVER_CSR" \
  -CA "$CA_CERT" -CAkey "$CA_KEY" -CAcreateserial \
  -out "$SERVER_CERT" \
  -extensions v3_ext -extfile "$OPENSSL_CNF" >/dev/null 2>&1
chmod 0600 "$SERVER_KEY"
chmod 0644 "$SERVER_CERT"

log "写入 Nginx HTTPS + WSS 反向代理"
cat > "$NGINX_CONF" <<EOF
server {
    listen $HTTPS_PORT ssl;
    server_name $HTTPS_HOST;

    ssl_certificate     $SERVER_CERT;
    ssl_certificate_key $SERVER_KEY;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_session_cache shared:ConversationTLS:10m;
    ssl_session_timeout 1d;

    client_max_body_size 30m;

    # Realtime ASR WebSocket. This location must preserve Upgrade/Connection.
    location = /chat/v1/asr/realtime {
        proxy_pass http://$UPSTREAM;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
    }

    # SSE task event stream.
    location ~ ^/chat/v1/tasks/[0-9a-fA-F-]+/events$ {
        proxy_pass http://$UPSTREAM;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_cache off;
        gzip off;
        proxy_read_timeout 900s;
        proxy_send_timeout 900s;
        add_header X-Accel-Buffering no always;
    }

    location / {
        proxy_pass http://$UPSTREAM;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_buffering off;
        proxy_read_timeout 900s;
        add_header Cache-Control "no-store, no-cache, must-revalidate, max-age=0" always;
    }
}
EOF

if [[ "$NGINX_CONF" == /etc/nginx/sites-available/* ]]; then
  mkdir -p /etc/nginx/sites-enabled
  ln -sfn "$NGINX_CONF" "/etc/nginx/sites-enabled/$CONF_NAME"
fi

nginx -t
if command -v systemctl >/dev/null 2>&1; then
  if systemctl is-active --quiet nginx; then
    systemctl reload nginx
  else
    systemctl start nginx
  fi
else
  nginx -s reload
fi

mkdir -p "$APP_DIR/deploy/https"
cp -f "$CA_CERT" "$APP_DIR/deploy/https/conversation-ca.crt"
chmod 0644 "$APP_DIR/deploy/https/conversation-ca.crt"

log "HTTPS 配置完成"
echo "访问地址: https://$HTTPS_HOST:$HTTPS_PORT/chat/"
echo "客户端需要信任的 CA 公钥证书: $APP_DIR/deploy/https/conversation-ca.crt"
echo "Windows 管理员终端可执行: certutil -addstore -f Root conversation-ca.crt"
echo "注意：只分发 conversation-ca.crt，绝不要分发 $CA_KEY"
echo "验证: curl -k https://$HTTPS_HOST:$HTTPS_PORT/ready"
