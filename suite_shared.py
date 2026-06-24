# -*- coding: utf-8 -*-
"""Unified marketing suite — shared session-state keys and helpers."""

from __future__ import annotations

import os
import re

import streamlit as st

SUITE_GEMINI_API_KEY = "suite_gemini_api_key"
SUITE_TITLE = "NuageWears工具组"
SUITE_SMTP_HOST = "suite_smtp_host"
SUITE_SMTP_PORT = "suite_smtp_port"
SUITE_SMTP_USER = "suite_smtp_user"
SUITE_SMTP_PASSWORD = "suite_smtp_password"
SUITE_SMTP_FROM_NAME = "suite_smtp_from_name"


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
        return default


def get_gemini_api_key() -> str:
    return (
        st.session_state.get(SUITE_GEMINI_API_KEY, "")
        or secret("gemini", "api_key")
        or ""
    ).strip()
