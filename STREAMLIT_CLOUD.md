# Streamlit Cloud 部署 — 营销工具套件

FB 广告库浅捞 + 投放数据 AI 分析（Gemini / Apify 密钥共用）。IG Sourcing 仍为独立本地工具。

## 访问地址

部署成功后：

```
https://fb-ad-tool.streamlit.app
```

（子域名可在创建应用时自定义，默认 `fb-ad-tool`。）

## 部署配置

| 字段 | 填写内容 |
|------|----------|
| Repository | `XingyuBillHou/ig-sourcing-tool` |
| Branch | `main` |
| **Main file path** | **`marketing_suite_app.py`** |
| Python version | `3.11`（推荐） |

## 一键打开部署页

```bash
bash deploy-streamlit-cloud.sh
```

## Secrets

在 App → Settings → Secrets 粘贴（参考 `.streamlit/secrets.toml.example`）：

```toml
[apify]
token = "apify_api_xxxxxxxx"

[gemini]
api_key = "AIzaSyxxxxxxxx"

[email]
# 可选
# smtp_host = "smtp.gmail.com"
# smtp_port = "587"
# smtp_user = "your@gmail.com"
# smtp_password = "your-app-password"
# from_name = "营销工具套件"
```

## 从旧版 FB 单工具迁移

若之前 Main file 为 `fb-competitor-ad-tool/fb_competitor_ad_app.py`：

1. 打开 https://share.streamlit.io → 你的应用 → **Settings**
2. 将 **Main file path** 改为 `marketing_suite_app.py`
3. 点击 **Save**，然后 **Reboot app**
4. 或删除旧应用后运行 `bash deploy-streamlit-cloud.sh` 重新创建

## 自动更新

`git push origin main` 后 Streamlit Cloud 会自动重新构建部署。

## 本地调试（与 Cloud 一致）

```bash
pip install -r requirements.txt
streamlit run marketing_suite_app.py
```
