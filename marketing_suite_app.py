# -*- coding: utf-8 -*-
"""
营销工具套件 — FB 广告库浅捞 + 投放数据 AI 分析
IG 红人 Sourcing 仍为独立应用（ig-sourcing-tool/）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent
FB_DIR = ROOT / "fb-competitor-ad-tool"
for path in (str(ROOT), str(FB_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

import ad_analysis_app
from fb_competitor_ad_core import (  # noqa: E402
    APIFY_TOKEN_SESSION_KEY,
    GEMINI_MODEL,
    GEMINI_MODEL_SESSION_KEY,
    _is_capable_gemini_video_model,
    _on_redetect_gemini_proxy,
    _sanitize_apify_token,
    _secret as fb_secret,
    apply_gemini_proxy,
    check_gemini_connectivity,
    get_gemini_model_label,
    get_gemini_video_model_options,
    init_gemini_proxy_for_session,
    render_fb_competitor_tool,
)
from suite_shared import (
    GEMINI_KEY_VALIDATION_VERSION,
    SUITE_DEPLOY_VERSION,
    SUITE_GEMINI_API_KEY,
    SUITE_SMTP_FROM_NAME,
    SUITE_SMTP_HOST,
    SUITE_SMTP_PASSWORD,
    SUITE_SMTP_PORT,
    SUITE_SMTP_USER,
    SUITE_TITLE,
    describe_gemini_key_input,
    get_gemini_api_key,
    is_gemini_auth_key,
    sanitize_gemini_api_key,
    secret,
)

PAGE_ICON = ROOT / "page_icon.png"

st.set_page_config(
    page_title=SUITE_TITLE,
    page_icon=str(PAGE_ICON) if PAGE_ICON.exists() else "🛠️",
    layout="wide",
)


def _init_shared_session_state() -> None:
    if APIFY_TOKEN_SESSION_KEY not in st.session_state:
        st.session_state[APIFY_TOKEN_SESSION_KEY] = secret("apify", "token")
    if SUITE_GEMINI_API_KEY not in st.session_state:
        st.session_state[SUITE_GEMINI_API_KEY] = secret("gemini", "api_key")
    if SUITE_SMTP_HOST not in st.session_state:
        st.session_state[SUITE_SMTP_HOST] = secret("email", "smtp_host", "smtp.gmail.com")
    if SUITE_SMTP_PORT not in st.session_state:
        st.session_state[SUITE_SMTP_PORT] = secret("email", "smtp_port", "587")
    if SUITE_SMTP_USER not in st.session_state:
        st.session_state[SUITE_SMTP_USER] = secret("email", "smtp_user")
    if SUITE_SMTP_PASSWORD not in st.session_state:
        st.session_state[SUITE_SMTP_PASSWORD] = secret("email", "smtp_password")
    if SUITE_SMTP_FROM_NAME not in st.session_state:
        st.session_state[SUITE_SMTP_FROM_NAME] = secret("email", "from_name", SUITE_TITLE)

    _model_options = get_gemini_video_model_options()
    _model_ids = [item["id"] for item in _model_options]
    _default_model = (secret("gemini", "model", GEMINI_MODEL) or GEMINI_MODEL).strip()
    if not _is_capable_gemini_video_model(_default_model) or _default_model not in _model_ids:
        _default_model = GEMINI_MODEL
    if GEMINI_MODEL_SESSION_KEY not in st.session_state:
        st.session_state[GEMINI_MODEL_SESSION_KEY] = _default_model

    if "suite_ad_analysis_model" not in st.session_state:
        st.session_state["suite_ad_analysis_model"] = ad_analysis_app.GEMINI_DEFAULT_MODEL


def _render_shared_sidebar() -> None:
    _init_shared_session_state()
    auto_proxy, auto_source = init_gemini_proxy_for_session()
    if "gemini_proxy_field" not in st.session_state:
        st.session_state.gemini_proxy_field = auto_proxy or secret("gemini", "proxy_url")

    with st.sidebar:
        st.header("🔑 全局配置")

        st.text_input(
            "Apify API Token",
            type="password",
            help="FB 广告库浅捞使用；Apify → Settings → Integrations 获取。",
            key=APIFY_TOKEN_SESSION_KEY,
        )
        secret_apify = _sanitize_apify_token(secret("apify", "token"))
        if secret_apify:
            st.caption("✅ Secrets 中已配置 Apify Token")
        elif (st.session_state.get(APIFY_TOKEN_SESSION_KEY) or "").strip():
            st.caption("✅ 使用侧边栏填写的 Apify Token")
        else:
            st.caption("⚠️ 未配置 Apify Token（仅影响 FB 浅捞）")

        st.markdown("---")
        st.text_input(
            "Google Gemini API Key",
            type="password",
            key=SUITE_GEMINI_API_KEY,
            help="两个工具共用；也可在 secrets.toml 的 [gemini] api_key 预填。",
        )
        if get_gemini_api_key():
            clean = get_gemini_api_key()
            key_type = "AQ. Auth Key" if is_gemini_auth_key(clean) else "AIza 标准 Key"
            if sanitize_gemini_api_key(st.session_state.get(SUITE_GEMINI_API_KEY, "")):
                st.caption(f"✅ Gemini Key 有效（来源：侧边栏，{key_type}）")
            else:
                st.caption(f"✅ Gemini Key 有效（来源：Secrets，{key_type}）")
        else:
            sidebar_raw = st.session_state.get(SUITE_GEMINI_API_KEY, "")
            if (sidebar_raw or "").strip():
                st.caption(
                    "⚠️ 侧边栏 Key 未能识别："
                    f"{describe_gemini_key_input(sidebar_raw)}"
                )
            elif secret("gemini", "api_key") or secret("google", "api_key"):
                st.caption(
                    "⚠️ Secrets 中 gemini.api_key 格式无效："
                    f"{describe_gemini_key_input(secret('gemini', 'api_key') or secret('google', 'api_key'))}"
                )

        st.caption(f"部署版本：{GEMINI_KEY_VALIDATION_VERSION}（若未显示此版本请 Reboot App）")

        col_proxy, col_redetect = st.columns([3, 1])
        with col_proxy:
            gemini_proxy = st.text_input(
                "HTTPS 代理",
                key="gemini_proxy_field",
                placeholder="如 http://127.0.0.1:7897",
            )
        with col_redetect:
            st.write("")
            st.write("")
            st.button(
                "🔄",
                help="重新检测代理",
                key="suite_redetect_gemini_proxy",
                on_click=_on_redetect_gemini_proxy,
            )

        if gemini_proxy:
            apply_gemini_proxy(gemini_proxy)
        elif auto_proxy:
            apply_gemini_proxy(auto_proxy)

        if auto_source:
            if auto_proxy:
                st.caption(f"✅ 代理：`{auto_proxy}`（{auto_source}）")
            else:
                st.caption(f"⚠️ {auto_source}")

        if st.button("🔌 测试 Gemini 连接", use_container_width=True, key="suite_test_gemini"):
            ok, err = check_gemini_connectivity(gemini_proxy or auto_proxy)
            if ok:
                st.success("✅ 可以连接 generativelanguage.googleapis.com")
            else:
                st.error(f"❌ {err}")

        st.markdown("---")
        with st.expander("🎬 FB 视频模型", expanded=False):
            model_options = get_gemini_video_model_options()
            model_ids = [item["id"] for item in model_options]
            st.selectbox(
                "Gemini 视频模型",
                options=model_ids,
                format_func=get_gemini_model_label,
                key=GEMINI_MODEL_SESSION_KEY,
            )

        with st.expander("📊 投放分析模型", expanded=False):
            st.selectbox(
                "文本分析模型",
                options=ad_analysis_app.GEMINI_MODELS,
                key="suite_ad_analysis_model",
            )
            st.slider(
                "Temperature（创意度）",
                0.0,
                1.0,
                0.5,
                0.1,
                key="suite_ad_analysis_temperature",
            )

        st.markdown("---")
        with st.expander("🎨 显示主题（投放分析）", expanded=False):
            theme_label = st.radio(
                "界面模式",
                options=list(ad_analysis_app.THEME_MODE_LABELS.values()),
                index=0,
                horizontal=True,
            )
            st.session_state["suite_theme_mode"] = next(
                k for k, v in ad_analysis_app.THEME_MODE_LABELS.items() if v == theme_label
            )

        with st.expander("📧 发邮件配置（可选）", expanded=False):
            st.text_input("SMTP 服务器", key=SUITE_SMTP_HOST)
            st.text_input("SMTP 端口", key=SUITE_SMTP_PORT)
            st.text_input("发件邮箱", key=SUITE_SMTP_USER)
            st.text_input("邮箱密码 / 应用专用密码", type="password", key=SUITE_SMTP_PASSWORD)
            st.text_input("发件人名称", key=SUITE_SMTP_FROM_NAME)

        st.markdown("---")
        st.caption("密钥仅在本地会话中使用；留空时会读取 secrets.toml / 环境变量。")


def main() -> None:
    _render_shared_sidebar()

    st.title(f"{SUITE_TITLE} · {SUITE_DEPLOY_VERSION}")
    st.caption("FB 广告库浅捞 · 投放数据 AI 分析（Gemini / Apify 密钥共用）")

    tab_fb, tab_analysis = st.tabs(["🎬 FB 广告库浅捞", "📊 投放数据 AI 分析"])

    with tab_fb:
        render_fb_competitor_tool(embedded=True)

    with tab_analysis:
        ad_analysis_app.main(embedded=True)


if __name__ == "__main__":
    main()
