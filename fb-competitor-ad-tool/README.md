# FB 竞品爆款广告拆解工具

Streamlit 应用：监控 Facebook 竞品广告库，筛选投放最久的 Top 3 视频，并用 Gemini 生成深度拆解与分镜脚本。

## 工作流

```
Page Name → Apify 抓取 FB 广告库 → 按 startDate 筛选 Top 3 视频
         → 下载 mp4 → Gemini 1.5 Pro 分析 → 展示拆解报告
```

## 快速开始

```bash
cd fb-competitor-ad-tool
pip install -r requirements.txt
streamlit run fb_competitor_ad_app.py
```

Mac / Windows 同事可直接双击 `start.command` / `start.bat`，详见 [使用说明.txt](./使用说明.txt)。

## 配置

| 密钥 | 用途 |
|------|------|
| Apify API Token | 调用 `apify/facebook-ads-scraper` 抓取广告库 |
| Google Gemini API Key | 上传视频并生成拆解报告 |

可在侧边栏填写，或复制 `.streamlit/secrets.toml.example` 为 `secrets.toml` 预填：

```toml
[apify]
token = "apify_api_xxx"

[gemini]
api_key = "AIzaSyxxx"
```

## 在 monorepo 中的位置

本工具位于 `meta-ads-uploader/fb-competitor-ad-tool/`，与 `ig-sourcing-tool`、`ad-analysis-tool` 同级，统一由父仓库管理版本。

## 依赖

- Python 3.10+
- streamlit, apify-client, google-generativeai, requests
