# -*- coding: utf-8 -*-
"""Unified marketing suite — shared session-state keys and helpers."""

from __future__ import annotations

import os
import re
import unicodedata

import streamlit as st

SUITE_GEMINI_API_KEY = "suite_gemini_api_key"
SUITE_TITLE = "NuageWears工具组"
SUITE_SMTP_HOST = "suite_smtp_host"
SUITE_SMTP_PORT = "suite_smtp_port"
SUITE_SMTP_USER = "suite_smtp_user"
SUITE_SMTP_PASSWORD = "suite_smtp_password"
SUITE_SMTP_FROM_NAME = "suite_smtp_from_name"
GEMINI_KEY_VALIDATION_VERSION = "2026-07-13e"
SUITE_DEPLOY_VERSION = GEMINI_KEY_VALIDATION_VERSION

GEMINI_STANDARD_KEY_PATTERN = re.compile(r"AIza[0-9A-Za-z_-]{20,}", re.I)
GEMINI_AUTH_KEY_PATTERN = re.compile(r"AQ\.[!-~]{8,}", re.I)
GEMINI_AUTH_KEY_PREFIX = re.compile(r"(?i)^aq\.")


def _normalize_key_input(raw: str) -> str:
    text = unicodedata.normalize("NFKC", raw or "")
    text = text.strip().strip('"').strip("'").strip()
    text = re.sub(r"[\u200b-\u200d\ufeff\u00a0]", "", text)
    text = text.strip("[](){}<>`")
    for src, dst in (
        ("\u2024", "."),
        ("\uff0e", "."),
        ("\u00b7", "."),
        ("．", "."),
    ):
        text = text.replace(src, dst)
    return text


def is_gemini_auth_key(api_key: str) -> bool:
    clean = sanitize_gemini_api_key(api_key)
    return bool(clean and GEMINI_AUTH_KEY_PREFIX.match(clean))


def _normalize_gemini_auth_key(key: str) -> str:
    if key.upper().startswith("AQ."):
        return "AQ." + key[3:]
    return key


def sanitize_gemini_api_key(api_key: str) -> str:
    """去除杂质并提取 Gemini API Key（支持 AIza 标准 Key 与 AQ. Auth Key）。"""
    raw = _normalize_key_input(api_key)
    if not raw:
        return ""

    for pattern in (GEMINI_AUTH_KEY_PATTERN, GEMINI_STANDARD_KEY_PATTERN):
        match = pattern.search(raw)
        if match:
            return _normalize_gemini_auth_key(match.group(0).rstrip(".,;"))

    compact = re.sub(r"\s+", "", raw)
    for pattern in (GEMINI_AUTH_KEY_PATTERN, GEMINI_STANDARD_KEY_PATTERN):
        if pattern.fullmatch(compact.rstrip(".,;")):
            return _normalize_gemini_auth_key(compact.rstrip(".,;"))

    if GEMINI_AUTH_KEY_PREFIX.match(compact) and len(compact) >= 12:
        return _normalize_gemini_auth_key(compact.rstrip(".,;"))

    if compact.upper().startswith("AIZA") and len(compact) >= 35:
        return compact

    loose_auth = re.search(r"(?i)aq\.[!-~]{8,}", compact)
    if loose_auth:
        return _normalize_gemini_auth_key(loose_auth.group(0).rstrip(".,;"))

    loose_std = re.search(r"(?i)AIza[0-9A-Za-z_-]{20,}", compact)
    if loose_std:
        return loose_std.group(0)

    return ""


def build_gemini_http_headers(api_key: str, *, for_openai_compat: bool = False) -> dict:
    """
    构建 Gemini HTTP 请求头。
    原生 Gemini API 与 AQ. Auth Key 均使用 x-goog-api-key。
    OpenAI 兼容端点仅适用于 AIza 标准 Key（Bearer）。
    """
    clean = sanitize_gemini_api_key(api_key) or (api_key or "").strip()
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if is_gemini_auth_key(clean) or not for_openai_compat:
        headers["x-goog-api-key"] = clean
    else:
        headers["Authorization"] = f"Bearer {clean}"
    return headers


def secret(section: str, key: str, default: str = "") -> str:
    """Read from env vars or st.secrets (same layout as tool apps)."""
    flat_env_keys = {
        ("apify", "token"): ("APIFY_TOKEN", "APIFY_API_TOKEN"),
        ("gemini", "api_key"): ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        ("gemini", "model"): ("GEMINI_MODEL",),
        ("gemini", "proxy_url"): ("GEMINI_PROXY_URL",),
        ("email", "smtp_host"): ("SMTP_HOST",),
        ("email", "smtp_port"): ("SMTP_PORT",),
        ("email", "smtp_user"): ("SMTP_USER",),
        ("email", "smtp_password"): ("SMTP_PASSWORD",),
        ("email", "from_addr"): ("SMTP_FROM_ADDR",),
        ("email", "from_name"): ("SMTP_FROM_NAME",),
        ("email", "default_recipients"): ("EMAIL_DEFAULT_RECIPIENTS",),
    }
    for env_name in flat_env_keys.get((section, key), ()):
        env_val = os.environ.get(env_name, "").strip()
        if env_val:
            return env_val

    section_upper = section.upper()
    key_upper = re.sub(r"[^A-Za-z0-9]", "_", key).upper()
    env_val = os.environ.get(f"{section_upper}_{key_upper}", "").strip()
    if env_val:
        return env_val

    try:
        val = st.secrets[section][key]
        return str(val).strip() if val else default
    except Exception:
        pass

    if (section, key) == ("gemini", "api_key"):
        for flat_key in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "gemini_api_key"):
            try:
                val = st.secrets[flat_key]
                if val:
                    return str(val).strip()
            except Exception:
                continue

    return default


def _gemini_key_raw_candidates() -> list[str]:
    return [
        st.session_state.get(SUITE_GEMINI_API_KEY, ""),
        secret("gemini", "api_key"),
        secret("google", "api_key"),
    ]


def get_gemini_api_key() -> str:
    """侧边栏优先；无效或留空时回退 Secrets / 环境变量，并统一清洗。"""
    best = ""
    for raw in _gemini_key_raw_candidates():
        clean = sanitize_gemini_api_key(raw)
        if clean and len(clean) > len(best):
            best = clean

    if best:
        return best

    for raw in _gemini_key_raw_candidates():
        text = _normalize_key_input(raw)
        compact = re.sub(r"\s+", "", text)
        if re.match(r"(?i)^aq\.", compact) and len(compact) >= 10:
            return _normalize_gemini_auth_key(compact)

    return ""


def describe_gemini_key_input(raw: str) -> str:
    """生成不含完整密钥的诊断信息，便于排查格式问题。"""
    text = _normalize_key_input(raw)
    if not text:
        return "（空）"
    compact = re.sub(r"\s+", "", text)
    prefix = compact[:6]
    if len(compact) > 6:
        prefix += "…"
    return f"长度 {len(compact)}，前缀 `{prefix}`"


def sync_gemini_key_session_state() -> None:
    """将侧边栏 / Secrets 中的 Key 清洗后写回 session，兼容旧版 ad_analysis。"""
    clean = get_gemini_api_key()
    if clean:
        st.session_state[SUITE_GEMINI_API_KEY] = clean


def apply_ad_analysis_gemini_patches() -> None:
    """
    兼容旧版 ad_analysis_app（仅识别 AIza）的运行时补丁。
    Cloud 若卡在 f2dea07 等旧提交，Reboot 后由新版入口注入 AQ. 支持。
    """
    try:
        import ad_analysis_app as ad
    except Exception:
        return

    try:
        ad._sanitize_api_key = sanitize_gemini_api_key  # type: ignore[attr-defined]
    except Exception:
        pass

    def _patched_gemini_key_error_hint(raw_key: str = "") -> str:
        sidebar_raw = str(st.session_state.get(SUITE_GEMINI_API_KEY, "")).strip()
        secret_raw = str(
            secret("gemini", "api_key") or secret("google", "api_key")
        ).strip()
        raw_candidates = [raw_key] if (raw_key or "").strip() else [sidebar_raw, secret_raw]
        raw = next((str(r).strip() for r in raw_candidates if str(r).strip()), "")

        if not raw:
            return (
                "请先在左侧侧边栏填写 Gemini API Key（AIza 或 AQ. 开头），"
                "或在 Streamlit Secrets 的 [gemini] api_key 中配置。"
            )
        if re.search(r"[\u4e00-\u9fff]", raw):
            return (
                "检测到 Key 中含中文，可能是误粘贴了说明文字。"
                "请只保留完整的英文 Key（AIza... 或 AQ....）。"
            )
        lowered = raw.lower()
        if "xxxx" in lowered:
            return (
                "检测到占位符或示例 Key（含 xxxx）。"
                "请从 [Google AI Studio](https://aistudio.google.com/apikey) 复制真实 Key。"
            )
        if lowered.startswith("apify"):
            return "误填了 Apify Token。请在「Google Gemini API Key」栏填写 AIza 或 AQ. 开头的 Key。"
        if raw.startswith("sk-"):
            return "误填了 OpenAI Key（sk-）。请填写 Gemini Key（AIza 或 AQ. 开头）。"
        return (
            "Gemini API Key 格式无效。"
            f"侧栏：{describe_gemini_key_input(sidebar_raw)}；"
            f"Secrets：{describe_gemini_key_input(secret_raw)}。"
            f"（部署版本应为 {GEMINI_KEY_VALIDATION_VERSION}；"
            "若仍看到「以 AIza 开头」说明 Cloud 未更新，请到 Manage app → Reboot。）"
            "请从 [Google AI Studio](https://aistudio.google.com/apikey) 复制完整 Key"
            "（新版以 **AQ.** 开头，旧版以 **AIzaSy** 开头）。"
        )

    try:
        ad._gemini_key_error_hint = _patched_gemini_key_error_hint  # type: ignore[attr-defined]
    except Exception:
        pass
