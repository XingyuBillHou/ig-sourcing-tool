#!/bin/bash
# 打开 Streamlit Community Cloud 部署页（营销工具套件）
set -euo pipefail

REPO="${STREAMLIT_REPO:-XingyuBillHou/ig-sourcing-tool}"
BRANCH="${STREAMLIT_BRANCH:-main}"
MAIN_FILE="${STREAMLIT_MAIN_FILE:-marketing_suite_app.py}"
SUBDOMAIN="${STREAMLIT_SUBDOMAIN:-fb-ad-tool}"

DEPLOY_URL="https://share.streamlit.io/deploy?repository=${REPO}&branch=${BRANCH}&mainModule=${MAIN_FILE}&subdomain=${SUBDOMAIN}"

cat <<EOF
==========================================
  Streamlit Cloud — 营销工具套件
  FB 广告库浅捞 + 投放数据 AI 分析
==========================================

1. 浏览器将打开预填好的部署页
2. 用 GitHub 登录 Streamlit（若尚未登录）
3. Main file path 应为: ${MAIN_FILE}
4. 若已有旧应用，请在 App Settings 修改 Main file path 后 Reboot
5. Secrets 见 .streamlit/secrets.toml.example

访问: https://${SUBDOMAIN}.streamlit.app

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
