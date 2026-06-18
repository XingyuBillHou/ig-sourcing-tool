#!/bin/bash
# 打开 Streamlit Community Cloud 部署页（仓库/分支/入口文件已预填）
set -euo pipefail

REPO="${STREAMLIT_REPO:-XingyuBillHou/ig-sourcing-tool}"
BRANCH="${STREAMLIT_BRANCH:-main}"
MAIN_FILE="${STREAMLIT_MAIN_FILE:-fb-competitor-ad-tool/fb_competitor_ad_app.py}"
SUBDOMAIN="${STREAMLIT_SUBDOMAIN:-fb-ad-tool}"

DEPLOY_URL="https://share.streamlit.io/deploy?repository=${REPO}&branch=${BRANCH}&mainModule=${MAIN_FILE}&subdomain=${SUBDOMAIN}"

cat <<EOF
==========================================
  Streamlit Cloud 一键部署
==========================================

1. 浏览器将打开预填好的部署页
2. 用 GitHub 登录 Streamlit（若尚未登录）
3. 确认配置后点击 Deploy
4. 部署完成后 → App Settings → Secrets，粘贴:

   见 .streamlit/secrets.toml.example

5. 访问: https://${SUBDOMAIN}.streamlit.app

预填部署链接:
${DEPLOY_URL}

EOF

if command -v open >/dev/null 2>&1; then
  open "$DEPLOY_URL"
elif command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$DEPLOY_URL"
else
  echo "请手动复制上方链接到浏览器打开"
fi
