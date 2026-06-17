#!/bin/bash
# 部署脚本：局域网 / Docker / 云端
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APP_DIR"

MODE="${1:-lan}"

usage() {
  cat <<'EOF'
用法:
  bash deploy.sh lan      局域网部署（同一 WiFi 内其他电脑可访问）
  bash deploy.sh docker   本地 Docker（仅本机 http://localhost:8501）
  bash deploy.sh cloud    云端 VPS 部署（公网 HTTPS，见 deploy-cloud.sh）

局域网示例:
  1. 在本机运行: bash deploy.sh lan
  2. 其他电脑浏览器打开: http://<本机IP>:8501

云端示例:
  1. 上传代码到云服务器，cp .env.cloud.example .env.cloud 并填密钥
  2. bash deploy.sh cloud
  3. 浏览器打开 https://你的域名 或 http://服务器公网IP
EOF
}

ensure_secrets_example() {
  if [ ! -f ".streamlit/secrets.toml" ]; then
    if [ -f ".streamlit/secrets.toml.example" ]; then
      cp ".streamlit/secrets.toml.example" ".streamlit/secrets.toml"
      echo "已创建 .streamlit/secrets.toml，请编辑后填入 Apify / Gemini 密钥。"
    fi
  fi
}

case "$MODE" in
  lan)
    bash "$APP_DIR/start-lan.sh"
    ;;
  docker)
    if ! command -v docker >/dev/null 2>&1; then
      echo "未安装 Docker，请先安装: https://docs.docker.com/get-docker/"
      exit 1
    fi
    ensure_secrets_example
    mkdir -p exports temp_videos
    if docker compose version >/dev/null 2>&1; then
      docker compose up -d --build
    else
      docker-compose up -d --build
    fi
    echo ""
    echo "Docker 已启动。"
    echo "访问: http://localhost:8501"
    echo "查看日志: docker compose logs -f"
    echo "停止: docker compose down"
    ;;
  cloud)
    bash "$APP_DIR/deploy-cloud.sh" up
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    echo "未知模式: $MODE"
    usage
    exit 1
    ;;
esac
