# Streamlit Cloud 部署指南（GitHub + Streamlit）

将本工具部署到 [Streamlit Community Cloud](https://share.streamlit.io)，任意电脑通过浏览器访问，无需自建服务器。

## 前置条件

1. 代码已推送到 **GitHub** 仓库（公开仓库可用免费版；私有仓库需 Streamlit Teams）
2. 拥有 [Streamlit Cloud](https://share.streamlit.io) 账号（用 GitHub 登录）
3. 准备好 **Apify Token** 和 **Gemini API Key**

## 第一步：推送代码到 GitHub

```bash
cd meta-ads-uploader
git add fb-competitor-ad-tool/
git commit -m "Prepare FB ad tool for Streamlit Cloud"
git push origin main
```

> 本工具位于 monorepo 子目录 `fb-competitor-ad-tool/`，与根目录其他工具共存，互不影响。

## 第二步：在 Streamlit Cloud 创建应用

1. 打开 https://share.streamlit.io ，点击 **Create app**
2. 选择 **Yup, I have an app**
3. 填写部署信息：

| 字段 | 填写内容 |
|------|----------|
| Repository | 你的 GitHub 仓库（如 `XingyuBillHou/ig-sourcing-tool`） |
| Branch | `main` |
| Main file path | `fb-competitor-ad-tool/fb_competitor_ad_app.py` |
| App URL（可选） | 自定义子域名，如 `fb-ad-tool` → `https://fb-ad-tool.streamlit.app` |

4. 点击 **Advanced settings**：
   - **Python version**：建议 `3.11`
   - **Secrets**：粘贴下方 TOML（填入真实密钥）

```toml
[apify]
token = "apify_api_xxxxxxxx"

[gemini]
api_key = "AIzaSyxxxxxxxx"
model = "gemini-3.5-flash"
# Streamlit Cloud 服务器在海外，通常无需代理
# proxy_url = ""

[brand]
website = "https://www.nuagewears.com"
# context = "可选：品牌摘要"

[email]
# 可选：发邮件功能
# smtp_host = "smtp.gmail.com"
# smtp_port = "587"
# smtp_user = "your@gmail.com"
# smtp_password = "your-app-password"
# from_addr = "your@gmail.com"
# from_name = "FB广告库浅捞工具"
# default_recipients = "team@company.com"
```

5. 点击 **Deploy**，等待 2–5 分钟构建完成

## 第三步：访问与分享

部署成功后获得类似地址：

```
https://fb-ad-tool.streamlit.app
```

把链接发给同事即可使用。密钥保存在 Streamlit Secrets 中，访客看不到 API Key。

## 自动更新

之后每次 `git push` 到对应分支，Streamlit Cloud 会自动重新部署，无需手动操作。

## 本地调试（与云端一致）

从 **仓库根目录** 启动（与 Cloud 工作目录一致）：

```bash
cd meta-ads-uploader
streamlit run fb-competitor-ad-tool/fb_competitor_ad_app.py
```

## 常见问题

**构建失败 / 依赖安装报错**
- 确认 Main file path 使用正斜杠 `/`，不要用 `\`
- 依赖文件位于 `fb-competitor-ad-tool/requirements.txt`，Cloud 会自动识别

**Gemini 连接失败**
- Streamlit Cloud 服务器在海外，一般不需要 VPN/代理
- 确认 Secrets 中 `gemini.api_key` 正确
- 在侧边栏点击「测试 Gemini 连接」排查

**视频上传或处理失败**
- 免费版有资源与运行时间限制，大视频可能超时
- 优先使用「广告库抓取」流程（服务器下载视频），比本地上传更稳定

**Secrets 不生效**
- 侧边栏留空即可，程序会自动读取 Secrets 中的 `[apify]` / `[gemini]`
- 修改 Secrets 后点击 App → **Reboot app**

**配置文件位置**
- monorepo 子目录应用共用仓库根目录的 `.streamlit/config.toml`
- 各工具 Secrets 在 Streamlit Cloud 控制台单独配置

## 与 Docker VPS 方案对比

| | Streamlit Cloud | Docker VPS |
|--|-----------------|------------|
| 运维 | 零运维 | 需维护服务器 |
| 费用 | 免费（公开仓库） | VPS 月费 |
| HTTPS | 自动 | 需 Caddy/证书 |
| 资源限制 | 有 | 取决于机器配置 |
| 适用 | 团队内部分享、快速上线 | 大流量、需访问控制 |

Docker VPS 部署见 `deploy-cloud.sh` 与 `使用说明.txt`。
