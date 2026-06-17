#!/bin/sh
set -eu

SECRETS_FILE="/app/.streamlit/secrets.toml"

if [ ! -s "$SECRETS_FILE" ]; then
  cat > "$SECRETS_FILE" <<EOF
# 由 docker-entrypoint 根据环境变量自动生成
[apify]
token = "${APIFY_TOKEN:-}"

[gemini]
api_key = "${GEMINI_API_KEY:-}"
model = "${GEMINI_MODEL:-gemini-3.5-flash}"
proxy_url = "${GEMINI_PROXY_URL:-}"

[brand]
website = "${BRAND_WEBSITE:-https://www.nuagewears.com}"
context = "${BRAND_CONTEXT:-}"

[email]
smtp_host = "${SMTP_HOST:-smtp.gmail.com}"
smtp_port = "${SMTP_PORT:-587}"
smtp_user = "${SMTP_USER:-}"
smtp_password = "${SMTP_PASSWORD:-}"
from_addr = "${SMTP_FROM_ADDR:-}"
from_name = "${SMTP_FROM_NAME:-FB广告库浅捞工具}"
default_recipients = "${EMAIL_DEFAULT_RECIPIENTS:-}"
EOF
fi

exec "$@"
