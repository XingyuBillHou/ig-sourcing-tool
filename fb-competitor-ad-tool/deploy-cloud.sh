#!/bin/bash
# 云端 VPS 一键部署（Docker + Caddy HTTPS）
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"

compose_cmd() {
  if docker compose version >/dev/null 2>&1; then
    docker compose -f docker-compose.cloud.yml "$@"
  else
    docker-compose -f docker-compose.cloud.yml "$@"
  fi
}

require_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    cat <<'EOF'
未检测到 Docker。云服务器请先安装:
  curl -fsSL https://get.docker.com | sh
  sudo usermod -aG docker $USER
  # 重新登录 SSH 后再运行本脚本
EOF
    exit 1
  fi
}

ensure_env() {
  if [ ! -f ".env.cloud" ]; then
    cp ".env.cloud.example" ".env.cloud"
    echo "已创建 .env.cloud — 请先编辑填入 APIFY_TOKEN 和 GEMINI_API_KEY"
    echo "  nano .env.cloud"
    exit 1
  fi
  # shellcheck disable=SC1091
  set -a
  source ".env.cloud"
  set +a
  if [ -z "${APIFY_TOKEN:-}" ] || [ -z "${GEMINI_API_KEY:-}" ]; then
    echo "请在 .env.cloud 中填写 APIFY_TOKEN 和 GEMINI_API_KEY"
    exit 1
  fi
}

generate_caddyfile() {
  # shellcheck disable=SC1091
  source ".env.cloud"
  local domain="${CADDY_DOMAIN:-}"
  local auth_user="${CADDY_BASIC_AUTH_USER:-}"
  local auth_hash="${CADDY_BASIC_AUTH_HASH:-}"

  {
    if [ -n "$domain" ]; then
      echo "$domain {"
    else
      echo ":80 {"
    fi
    if [ -n "$auth_user" ] && [ -n "$auth_hash" ]; then
      echo "    basicauth /* {"
      echo "        $auth_user $auth_hash"
      echo "    }"
    fi
    echo "    reverse_proxy app:8501"
    echo "}"
  } > Caddyfile

  echo "已生成 Caddyfile"
  if [ -n "$domain" ]; then
    echo "  域名: https://${domain} （Caddy 自动申请 HTTPS 证书）"
    echo "  请确保 DNS 已指向本服务器公网 IP"
  else
    echo "  访问: http://<服务器公网IP> （未配置域名，无 HTTPS）"
  fi
}

open_firewall_hint() {
  cat <<'EOF'

【防火墙】若外网无法访问，在云控制台安全组 / 防火墙放行:
  - TCP 80
  - TCP 443  （有域名时）
EOF
}

case "${1:-up}" in
  up|start|deploy)
    require_docker
    ensure_env
    generate_caddyfile
    compose_cmd up -d --build
    echo ""
    echo "=========================================="
    echo "  云端部署完成"
    echo "=========================================="
    # shellcheck disable=SC1091
    source ".env.cloud"
    if [ -n "${CADDY_DOMAIN:-}" ]; then
      echo "访问: https://${CADDY_DOMAIN}"
    else
      PUBLIC_IP=""
      if command -v curl >/dev/null 2>&1; then
        PUBLIC_IP="$(curl -fsS --max-time 5 ifconfig.me 2>/dev/null || curl -fsS --max-time 5 icanhazip.com 2>/dev/null || true)"
      fi
      if [ -n "$PUBLIC_IP" ]; then
        echo "访问: http://${PUBLIC_IP}"
      else
        echo "访问: http://<你的服务器公网IP>"
      fi
    fi
    if [ -n "${CADDY_BASIC_AUTH_USER:-}" ]; then
      echo "登录: 用户名 ${CADDY_BASIC_AUTH_USER} + 你在 .env.cloud 设置的密码"
    fi
    echo ""
    echo "查看日志: bash deploy-cloud.sh logs"
    echo "停止服务: bash deploy-cloud.sh down"
    open_firewall_hint
    ;;
  down|stop)
    require_docker
    compose_cmd down
    ;;
  restart)
    require_docker
    compose_cmd restart
    ;;
  logs)
    require_docker
    compose_cmd logs -f --tail=200
    ;;
  status)
    require_docker
    compose_cmd ps
    ;;
  hash-password)
    read -r -s -p "输入访问密码: " pwd
    echo ""
    docker run --rm caddy:2-alpine caddy hash-password --plaintext "$pwd"
    echo ""
    echo "将上面 hash 填入 .env.cloud 的 CADDY_BASIC_AUTH_HASH"
    echo "并设置 CADDY_BASIC_AUTH_USER=你的用户名"
    ;;
  *)
    cat <<'EOF'
云端部署命令:
  bash deploy-cloud.sh up       构建并启动（首次部署）
  bash deploy-cloud.sh logs     查看日志
  bash deploy-cloud.sh restart  重启
  bash deploy-cloud.sh down     停止
  bash deploy-cloud.sh hash-password  生成 Caddy 访问密码 hash

首次部署步骤:
  1. 将 fb-competitor-ad-tool 文件夹上传到云服务器（git clone / scp）
  2. cp .env.cloud.example .env.cloud 并填入密钥
  3. （推荐）设置 CADDY_DOMAIN=你的域名
  4. bash deploy-cloud.sh up
EOF
    exit 1
    ;;
esac
