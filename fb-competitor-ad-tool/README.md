# FB 竞品爆款广告拆解工具

Streamlit 应用：搜索 Meta 广告库，抓取近 7 天曝光 Top 3 视频，用 Gemini 识别 Hook 列并生成浅捞表格。

## 工作流

```
关键词 → Apify 抓取 FB 广告库 → 近7天 + 曝光 Top3 视频
      → 下载 mp4 → ffmpeg 本地填表 + Gemini 识别 J/K 列 → 打包下载/发邮件
```

## 部署方式

| 方式 | 说明 |
|------|------|
| **Streamlit Cloud**（推荐） | GitHub 推送 + [share.streamlit.io](https://share.streamlit.io) 部署，见 [STREAMLIT_CLOUD.md](./STREAMLIT_CLOUD.md) |
| 本机 | `bash start.sh` 或双击 `start.command` / `start.bat` |
| 局域网 | `bash deploy.sh lan` |
| Docker VPS | `bash deploy-cloud.sh up` |

### Streamlit Cloud 关键配置

- **Main file path**: `fb-competitor-ad-tool/fb_competitor_ad_app.py`
- **Secrets**: 见 `.streamlit/secrets.toml.example`
- **Python**: 3.11 推荐

## 本地开发

```bash
# 从 monorepo 根目录启动（与 Streamlit Cloud 一致）
cd ..
streamlit run fb-competitor-ad-tool/fb_competitor_ad_app.py
```

或在工具目录内：

```bash
cd fb-competitor-ad-tool
pip install -r requirements.txt
streamlit run fb_competitor_ad_app.py
```

## 配置

| 密钥 | 用途 |
|------|------|
| Apify API Token | 调用 `apify/facebook-ads-scraper` 抓取广告库 |
| Google Gemini API Key | 分析视频前 5 秒，识别 HookVO / Text Hook |

可在侧边栏填写，或通过 Streamlit Secrets / `secrets.toml` 预填：

```toml
[apify]
token = "apify_api_xxx"

[gemini]
api_key = "AIzaSyxxx"
model = "gemini-3.5-flash"
```

## 在 monorepo 中的位置

本工具位于 `meta-ads-uploader/fb-competitor-ad-tool/`，与 `ig-sourcing-tool`、`ad-analysis-tool` 同级。

Streamlit Cloud 使用仓库根目录的 `.streamlit/config.toml`；依赖文件在本目录的 `requirements.txt`。

## 依赖

- Python 3.10+
- streamlit, apify-client, google-generativeai, requests, pandas, openpyxl, imageio-ffmpeg

`imageio-ffmpeg` 自带 ffmpeg 二进制，Streamlit Cloud 无需额外安装系统包。
