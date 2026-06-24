"""
FB 广告库浅捞系统
====================================
工作流：
  ① 广告库关键词 -> Apify 抓取近7天视频 -> 按曝光量取 Top3 下载（打包下载）
  ② 用户上传本地 mp4 -> Gemini 识别 J/K 列 -> 打包 zip / 发邮件
"""

import io
import json
import mimetypes
import os
import re
import shutil
import socket
import ssl
import smtplib
import subprocess
import time
import uuid
import zipfile
import traceback
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Callable, Optional
from urllib.parse import quote

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import streamlit as st
from apify_client import ApifyClient
try:
    from apify_client.errors import ApifyApiError
except ImportError:
    ApifyApiError = Exception
import google.generativeai as genai

import sys

_SUITE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _SUITE_ROOT not in sys.path:
    sys.path.insert(0, _SUITE_ROOT)
from suite_shared import (
    SUITE_GEMINI_API_KEY,
    SUITE_SMTP_FROM_NAME,
    SUITE_SMTP_HOST,
    SUITE_SMTP_PASSWORD,
    SUITE_SMTP_PORT,
    SUITE_SMTP_USER,
    get_gemini_api_key,
)

# ------------------------------------------------------------------
# 全局配置
# ------------------------------------------------------------------
APP_DIR = os.path.dirname(os.path.abspath(__file__))
TEMP_DIR = os.path.join(APP_DIR, "temp_videos")
EXPORT_DIR = os.path.join(APP_DIR, "exports")
FB_ADS_ACTOR_ID = "apify/facebook-ads-scraper"
TOP_N = 3
FETCH_LOOKBACK_DAYS = 7
FETCH_LOOKBACK_STAGES = (7, 14, 30, 60)  # 优先 7 天；凑不满 Top3 再逐步放宽
MAX_VIDEO_DURATION_SEC = 60  # 超过 1 分钟的视频剔除
FETCH_CANDIDATE_POOL_LIMIT = 300  # 单次抓取候选池
MAX_FETCH_ROUNDS = 4  # 每个时间窗内的 Apify 重试次数
FALLBACK_TRUST_META_SEARCH = True  # 严格筛选凑不满 Top3 时，信任 Meta 关键词搜索补齐
TRUST_META_ON_GEMINI_FAILURE = True  # Gemini 调用异常时仍保留视频（信任 Meta 搜索结果）
RELEVANCE_CLIP_SECONDS = 15  # Gemini 判定视频与关键词相关性时分析前 N 秒
POLL_INTERVAL_SEC = 3
RESULTS_LIMIT = FETCH_CANDIDATE_POOL_LIMIT
DOWNLOAD_RETRIES = 3
DOWNLOAD_CHUNK_SIZE = 256 * 1024
DOWNLOAD_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
}
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
# Gmail 单封邮件约 25MB 上限，预留正文与编码开销
EMAIL_MAX_ATTACHMENT_BYTES = 18 * 1024 * 1024

# apify/facebook-ads-scraper 视频 URL 字段优先级（HD 优先）
_VIDEO_URL_KEYS = (
    "videoHdUrl",
    "video_hd_url",
    "watermarkedVideoHdUrl",
    "watermarked_video_hd_url",
    "videoSdUrl",
    "video_sd_url",
    "watermarkedVideoSdUrl",
    "watermarked_video_sd_url",
    "videoUrl",
    "video_url",
)

os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(EXPORT_DIR, exist_ok=True)

def _configure_page_standalone() -> None:
    st.set_page_config(page_title="FB 广告库浅捞工具", page_icon="🎬", layout="wide")


def _secret(section: str, key: str, default: str = "") -> str:
    """从环境变量或 .streamlit/secrets.toml 读取配置（云端部署优先读环境变量）。"""
    flat_env_keys = {
        ("apify", "token"): ("APIFY_TOKEN", "APIFY_API_TOKEN"),
        ("gemini", "api_key"): ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        ("gemini", "model"): ("GEMINI_MODEL",),
        ("gemini", "proxy_url"): ("GEMINI_PROXY_URL",),
        ("brand", "website"): ("BRAND_WEBSITE",),
        ("brand", "context"): ("BRAND_CONTEXT",),
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


# ------------------------------------------------------------------
# 模块二：Apify 抓取广告库视频池
# ------------------------------------------------------------------
def build_ad_library_url(
    search_keyword: str,
    country: str = "ALL",
    lookback_days: int = FETCH_LOOKBACK_DAYS,
    search_type: Optional[str] = None,
    apply_date_filter: bool = True,
) -> str:
    """构建 Meta Ad Library 全库关键词 + 仅视频 + 可选近 N 天投放 搜索 URL。"""
    q = search_keyword.strip()
    today = datetime.now().date()
    min_date = today - timedelta(days=lookback_days)
    if search_type is None:
        # 默认无序关键词，结果面比 exact_phrase 更广
        search_type = "keyword_unordered"
    url = (
        "https://www.facebook.com/ads/library/"
        f"?active_status=active"
        f"&ad_type=all"
        f"&country={quote(country)}"
        f"&media_type=video"
        f"&q={quote(q)}"
        f"&search_type={search_type}"
        f"&sort_data[mode]=total_impressions"
        f"&sort_data[direction]=desc"
    )
    if apply_date_filter:
        url += (
            f"&start_date[min]={min_date.isoformat()}"
            f"&start_date[max]={today.isoformat()}"
        )
    return url


def _dedupe_ads(items: list) -> list:
    """按广告 archive ID 去重，保留首次出现。"""
    seen = set()
    deduped = []
    for item in items:
        ad_id = (
            item.get("adArchiveID")
            or item.get("adArchiveId")
            or item.get("ad_archive_id")
            or item.get("adArchiveID")
        )
        key = ad_id or id(item)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _apify_run_dataset_id(run) -> str:
    """从 Apify run 结果提取 dataset ID（兼容 dict 与 apify-client v3+ 的 Run 模型）。"""
    if run is None:
        raise RuntimeError("Apify Actor 运行未返回结果")
    if isinstance(run, dict):
        dataset_id = run.get("defaultDatasetId") or run.get("default_dataset_id")
    else:
        dataset_id = getattr(run, "default_dataset_id", None)
    if not dataset_id:
        status = getattr(run, "status", None) if not isinstance(run, dict) else run.get("status")
        raise RuntimeError(f"Apify run 缺少 defaultDatasetId（status={status}）")
    return str(dataset_id)


def _sanitize_apify_token(token: str) -> str:
    """去除首尾空白与误粘贴的引号。"""
    return (token or "").strip().strip('"').strip("'")


def _looks_like_placeholder_apify_token(token: str) -> bool:
    lowered = token.lower()
    return "xxxx" in lowered or lowered in {"apify_api_", "your_token", "your-token"}


def check_apify_connectivity(apify_token: str, timeout: float = 12.0) -> tuple[bool, str]:
    """启动 Actor 前快速校验 Apify Token 是否有效。"""
    token = _sanitize_apify_token(apify_token)
    if not token:
        return False, "未填写 Apify API Token。"
    if _looks_like_placeholder_apify_token(token):
        return False, "Apify Token 仍是示例占位符，请替换为真实 Token。"
    if not token.startswith("apify_api_"):
        return False, "Apify Token 格式异常（应以 apify_api_ 开头），请检查是否复制完整。"

    try:
        client = ApifyClient(token)
        client.user().get()
        return True, ""
    except ApifyApiError as exc:
        msg = str(exc)
        if "not valid" in msg.lower() or "unauthorized" in msg.lower() or "not found" in msg.lower():
            return False, (
                "Apify Token 无效或已过期。"
                "请在 Apify 控制台 → Settings → Integrations 重新生成 Personal API token，"
                "并更新 Streamlit Cloud Secrets 或侧边栏输入框。"
            )
        return False, f"Apify 连接失败：{msg}"
    except Exception as exc:
        return False, f"Apify 连接失败：{exc}"


def _is_apify_auth_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    if isinstance(exc, ApifyApiError) and getattr(exc, "status_code", None) == 401:
        return True
    return "authentication token is not valid" in msg or "user was not found" in msg


def _apify_auth_help_markdown() -> str:
    return """**如何配置 Apify Token（Streamlit Cloud）**

1. 打开 [Apify → Settings → Integrations](https://console.apify.com/account/integrations)
2. 复制 **Personal API tokens**（格式类似 `apify_api_xxxxxxxx`）
3. 打开 Streamlit Cloud → 你的 App → **Settings → Secrets**，粘贴：

```toml
[apify]
token = "apify_api_你的真实Token"
```

4. 点击 **Save**，等待 App 自动重启后再试

也可在侧边栏 **Apify API Token** 输入框直接填写（会覆盖 Secrets 中的空值）。"""


def fetch_fb_ads(
    apify_token: str,
    search_keyword: str,
    country: str = "ALL",
    *,
    results_limit: int = RESULTS_LIMIT,
    search_type: Optional[str] = None,
    lookback_days: int = FETCH_LOOKBACK_DAYS,
    apply_date_filter: bool = True,
) -> list:
    """从 Meta Ad Library 全库按关键词抓取视频广告。"""
    apify_token = _sanitize_apify_token(apify_token)
    if not apify_token:
        raise RuntimeError("未配置 Apify API Token")
    client = ApifyClient(apify_token)
    ad_library_url = build_ad_library_url(
        search_keyword,
        country,
        lookback_days=lookback_days,
        search_type=search_type,
        apply_date_filter=apply_date_filter,
    )

    run_input = {
        "startUrls": [{"url": ad_library_url}],
        "resultsLimit": results_limit,
        "isDetailsPerAd": True,
        "activeStatus": "",
    }

    run = client.actor(FB_ADS_ACTOR_ID).call(run_input=run_input)
    dataset_id = _apify_run_dataset_id(run)

    items = list(client.dataset(dataset_id).iterate_items())
    return _dedupe_ads(items)


def _extract_ad_archive_id(item: dict) -> str:
    for key in ("adArchiveID", "adArchiveId", "ad_archive_id", "adId", "ad_id"):
        val = item.get(key)
        if val not in (None, "", 0):
            return str(val)
    return ""


def _truthy_flag(val) -> bool:
    if val is True:
        return True
    if isinstance(val, str):
        return val.strip().lower() in ("true", "yes", "1", "y")
    if isinstance(val, (int, float)):
        return val == 1
    return False


def _is_fully_ai_generated_ad(item: dict) -> bool:
    """
    仅判定「全篇 AI / 数字合成」广告（预筛阶段）。
    Meta 的 containsDigitalCreatedMedia 只表示含部分 AI 素材，不能用来剔除；
    含穿插 AI 片段的混剪视频应保留，由后续 Gemini 看视频再判是否全篇 AI。
    """
    _ = item
    return False


def _extract_brand_name(item: dict) -> str:
    for key in ("pageName", "page_name", "advertiserPageName"):
        val = item.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    snapshot = item.get("snapshot") or {}
    if isinstance(snapshot, dict):
        val = snapshot.get("pageName") or snapshot.get("page_name")
        if isinstance(val, str) and val.strip():
            return val.strip()
    return "未知品牌"


def _collect_video_urls(item: dict) -> dict:
    """递归收集 item 内所有视频 URL 字段。"""
    found = {}

    def walk(obj):
        if isinstance(obj, dict):
            for key, val in obj.items():
                if isinstance(val, str) and val.startswith("http"):
                    key_lower = key.lower()
                    if key in _VIDEO_URL_KEYS or (
                        "video" in key_lower and "url" in key_lower and "preview" not in key_lower
                    ):
                        found[key] = val
                else:
                    walk(val)
        elif isinstance(obj, list):
            for entry in obj:
                walk(entry)

    walk(item)
    return found


def _extract_video_url(item: dict):
    """尽量兼容 apify/facebook-ads-scraper 及同类 Actor 的返回结构。"""
    found = _collect_video_urls(item)
    for key in _VIDEO_URL_KEYS:
        url = found.get(key)
        if url:
            return url

    # creatives[] 等其它 Actor 结构
    creatives = item.get("creatives")
    if isinstance(creatives, list):
        for creative in creatives:
            if isinstance(creative, dict):
                url = creative.get("videoUrl") or creative.get("video_url")
                if url:
                    return url

    return next(iter(found.values()), None)


def _extract_start_date(item: dict):
    """兼容不同字段名与 Unix 时间戳格式的开始投放日期。"""
    containers = [item]
    snapshot = item.get("snapshot") or {}
    if isinstance(snapshot, dict):
        containers.append(snapshot)
    for details_key in ("ad_details", "adDetails", "details"):
        details = item.get(details_key) or {}
        if isinstance(details, dict):
            containers.append(details)

    date_keys = (
        "startDateFormatted",
        "start_date_formatted",
        "startDate",
        "start_date",
        "adDeliveryStartTime",
        "adStartDate",
        "deliveryStartTime",
        "delivery_start_time",
        "ad_delivery_start_time",
        "collationStartDate",
    )
    for container in containers:
        if not isinstance(container, dict):
            continue
        for key in date_keys:
            val = container.get(key)
            if val not in (None, "", 0):
                return val
    return None


def _start_date_sort_key(val) -> int:
    """排序键：统一转为可比较的整数（越早投放数值越小）。"""
    if val is None:
        return 99999999999999
    if isinstance(val, (int, float)):
        return int(val)
    s = str(val).strip()
    if not s:
        return 99999999999999
    if s.isdigit():
        return int(s)
    # ISO / 日期字符串：压缩为 YYYYMMDDHHMMSS 整数
    compact = "".join(ch for ch in s if ch.isdigit())
    if len(compact) >= 8:
        return int(compact[:14].ljust(14, "0"))
    return 99999999999999


def _parse_start_date_to_datetime(val) -> Optional[datetime]:
    """将投放开始时间解析为 UTC datetime。"""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        ts = int(val)
        if ts > 1_000_000_000_000:
            ts //= 1000
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OSError, ValueError):
            return None
    s = str(val).strip()
    if not s:
        return None
    if s.isdigit():
        ts = int(s)
        if ts > 1_000_000_000_000:
            ts //= 1000
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OSError, ValueError):
            return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        pass
    compact = "".join(ch for ch in s if ch.isdigit())
    if len(compact) >= 8:
        try:
            return datetime.strptime(compact[:8], "%Y%m%d").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


def _is_ad_started_within_lookback(
    item: dict,
    lookback_days: int = FETCH_LOOKBACK_DAYS,
    *,
    allow_missing_start_date: bool = False,
) -> bool:
    """广告是否在近 lookback_days 天内开始投放。"""
    start_val = _extract_start_date(item)
    if not start_val:
        return allow_missing_start_date
    start_dt = _parse_start_date_to_datetime(start_val)
    if not start_dt:
        return allow_missing_start_date
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    return start_dt >= cutoff


def _lookback_priority(item: dict, preferred_days: int) -> int:
    """数值越小越优先：在 preferred_days 内最佳，否则依次 14/30/60/90 天。"""
    seen = set()
    windows = []
    for days in (preferred_days, 14, 30, 60, 90):
        if days not in seen:
            windows.append(days)
            seen.add(days)
    for idx, days in enumerate(windows):
        if _is_ad_started_within_lookback(item, days, allow_missing_start_date=True):
            return idx
    return len(windows)


def _sort_pool_for_selection(pool: dict[str, dict], keyword: str, preferred_days: int) -> list[dict]:
    """优先近 preferred_days 天高曝光视频，再逐步放宽。"""
    return sorted(
        pool.values(),
        key=lambda ad: (
            _lookback_priority(ad.get("raw") or {}, preferred_days),
            -ad.get("impression_score", -1),
            -_keyword_relevance_score(ad.get("raw") or {}, keyword),
        ),
    )


def diagnose_ad_items(items: list) -> dict:
    """统计抓取结果，便于排查「无视频」问题。"""
    stats = {
        "total": len(items),
        "with_video_url": 0,
        "with_start_date": 0,
        "with_both": 0,
        "display_formats": {},
        "sample_page_names": [],
    }
    for item in items:
        if _extract_video_url(item):
            stats["with_video_url"] += 1
        if _extract_start_date(item):
            stats["with_start_date"] += 1
        if _extract_video_url(item) and _extract_start_date(item):
            stats["with_both"] += 1

        snapshot = item.get("snapshot") or {}
        fmt = None
        if isinstance(snapshot, dict):
            fmt = snapshot.get("displayFormat") or snapshot.get("display_format")
        fmt = fmt or item.get("format") or item.get("mediaType") or "UNKNOWN"
        stats["display_formats"][str(fmt)] = stats["display_formats"].get(str(fmt), 0) + 1

        name = item.get("pageName") or item.get("page_name")
        if name and name not in stats["sample_page_names"]:
            stats["sample_page_names"].append(name)

    stats["sample_page_names"] = stats["sample_page_names"][:5]
    return stats


def _keyword_tokens(keyword: str) -> list[str]:
    """将搜索词拆成匹配用词（支持英文词组与中文）。"""
    keyword = (keyword or "").strip().lower()
    if not keyword:
        return []
    tokens = re.findall(r"[a-z0-9]+", keyword)
    for seg in re.findall(r"[\u4e00-\u9fff]+", keyword):
        if len(seg) >= 2:
            tokens.append(seg)
    return tokens or [keyword]


def _token_variants(token: str) -> list[str]:
    variants = {token}
    if token.endswith("s") and len(token) > 3:
        variants.add(token[:-1])
    else:
        variants.add(token + "s")
    if token == "woman":
        variants.add("women")
    if token == "women":
        variants.add("woman")
    return list(variants)


def _token_in_text(token: str, text: str) -> bool:
    return any(variant in text for variant in _token_variants(token))


def _extract_searchable_ad_text(item: dict) -> str:
    """汇总广告品牌/文案/CTA 等可用于关键词匹配的文本（避免递归整棵 JSON 误匹配）。"""
    parts = [
        _extract_brand_name(item),
        _extract_ad_copy_text(item),
        _extract_cta_text(item),
    ]
    snapshot = item.get("snapshot") or {}
    if isinstance(snapshot, dict):
        for key in ("title", "body", "linkDescription", "caption", "pageName"):
            val = snapshot.get(key)
            if isinstance(val, str) and val.strip():
                parts.append(val.strip())
        for card in (snapshot.get("cards") or [])[:5]:
            if isinstance(card, dict):
                for key in ("title", "body"):
                    val = card.get(key)
                    if isinstance(val, str) and val.strip():
                        parts.append(val.strip())
    for key in ("title", "body", "adCreativeBody", "linkDescription", "caption", "pageName"):
        val = item.get(key)
        if isinstance(val, str) and val.strip():
            parts.append(val.strip())
    return " ".join(parts).lower()


def _keyword_relevance_score(item: dict, keyword: str) -> int:
    text = _extract_searchable_ad_text(item)
    kw = (keyword or "").strip().lower()
    score = 0
    if kw and kw in text:
        score += 1000
    for token in _keyword_tokens(keyword):
        if _token_in_text(token, text):
            score += 100
    return score


def _ad_matches_keyword(item: dict, keyword: str) -> bool:
    """严格判断广告文案/品牌是否与关键词相关（不再使用宽松部分匹配）。"""
    kw = (keyword or "").strip().lower()
    if not kw:
        return True
    text = _extract_searchable_ad_text(item)
    if kw in text:
        return True
    tokens = _keyword_tokens(keyword)
    if not tokens:
        return False
    if len(tokens) == 1:
        return _token_in_text(tokens[0], text)
    return all(_token_in_text(token, text) for token in tokens)


def rank_video_ad_candidates(
    items: list,
    search_keyword: str = "",
    lookback_days: int = FETCH_LOOKBACK_DAYS,
    *,
    exclude_ad_ids: Optional[set] = None,
    exclude_video_urls: Optional[set] = None,
    require_keyword_in_text: bool = False,
    allow_missing_start_date: bool = False,
    enforce_lookback: bool = True,
) -> tuple[list, dict]:
    """
    从 Apify 结果中筛出候选视频广告并排序（不截断 top_n）：
    含视频 · 近 N 天 · 非全篇 AI（全篇 AI 由 Gemini 下载后判定）
    关键词相关性：默认信任 Meta 搜索 URL（require_keyword_in_text=False），
    由后续 Gemini 看视频内容再验证。
    """
    exclude_ad_ids = exclude_ad_ids or set()
    exclude_video_urls = exclude_video_urls or set()
    all_video_ads = []
    ai_filtered = 0
    keyword_rejected = 0
    excluded_seen = 0
    no_video = 0
    no_start_date = 0
    outside_lookback = 0

    for item in items:
        ad_id = _extract_ad_archive_id(item)
        if ad_id and ad_id in exclude_ad_ids:
            excluded_seen += 1
            continue
        video_url = _extract_video_url(item)
        if not video_url:
            no_video += 1
            continue
        if video_url in exclude_video_urls:
            excluded_seen += 1
            continue
        if _is_fully_ai_generated_ad(item):
            ai_filtered += 1
            continue

        start_date = _extract_start_date(item)
        if not start_date:
            if allow_missing_start_date:
                start_date = ""
            else:
                no_start_date += 1
                continue
        if enforce_lookback and not _is_ad_started_within_lookback(
            item,
            lookback_days,
            allow_missing_start_date=allow_missing_start_date,
        ):
            outside_lookback += 1
            continue

        kw = (search_keyword or "").strip()
        if require_keyword_in_text and kw and not _ad_matches_keyword(item, kw):
            keyword_rejected += 1
            continue

        all_video_ads.append(
            {
                "ad_id": ad_id,
                "video_url": video_url,
                "start_date": start_date,
                "impression_label": _extract_impression_label(item),
                "impression_score": _extract_impression_sort_key(item),
                "raw": item,
            }
        )

    stats = {
        "total_items": len(items),
        "total_video": len(all_video_ads),
        "within_lookback": len(all_video_ads),
        "lookback_days": lookback_days,
        "keyword": (search_keyword or "").strip(),
        "keyword_matched": len(all_video_ads),
        "match_mode": "meta_search" if not require_keyword_in_text else "strict",
        "ai_filtered": ai_filtered,
        "keyword_rejected": keyword_rejected,
        "excluded_seen": excluded_seen,
        "no_video": no_video,
        "no_start_date": no_start_date,
        "outside_lookback": outside_lookback,
        "with_impression_data": sum(1 for ad in all_video_ads if ad["impression_score"] >= 0),
        "sort_by": "impression_desc",
    }

    if not all_video_ads:
        if ai_filtered or keyword_rejected:
            stats["match_mode"] = "none_after_filter"
        else:
            stats["match_mode"] = "none_in_window"
        return [], stats

    kw = (search_keyword or "").strip()
    # 同一视频 URL 只保留曝光最高的一条（避免多条广告共用素材占满候选池）
    by_video_url: dict[str, dict] = {}
    for ad in all_video_ads:
        vurl = ad["video_url"]
        prev = by_video_url.get(vurl)
        if prev is None or ad["impression_score"] > prev["impression_score"]:
            by_video_url[vurl] = ad
    all_video_ads = list(by_video_url.values())

    all_video_ads.sort(
        key=lambda ad: (
            -ad["impression_score"],
            -_keyword_relevance_score(ad["raw"], kw),
            -_start_date_sort_key(ad["start_date"]),
        )
    )
    return all_video_ads, stats


def filter_top_impression_videos(
    items: list,
    top_n: int = TOP_N,
    search_keyword: str = "",
    lookback_days: int = FETCH_LOOKBACK_DAYS,
) -> tuple[list, dict]:
    """按曝光排序取 top_n（供兼容调用；抓取流程请用 collect_validated_top_videos）。"""
    candidates, stats = rank_video_ad_candidates(
        items, search_keyword, lookback_days=lookback_days
    )
    if not candidates:
        stats["match_mode"] = stats.get("match_mode") or "none"
        return [], stats
    return candidates[:top_n], stats


def filter_top_longest_running_videos(
    items: list,
    top_n: int = TOP_N,
    search_keyword: str = "",
) -> tuple[list, dict]:
    """兼容旧函数名；实际按曝光量高→低排序。"""
    return filter_top_impression_videos(items, top_n, search_keyword)


# ------------------------------------------------------------------
# 模块三：静默下载视频
# ------------------------------------------------------------------
def _download_video_stream(video_url: str, local_path: str) -> None:
    """流式下载，支持断点续传（Range）。"""
    resume_from = 0
    if os.path.exists(local_path):
        resume_from = os.path.getsize(local_path)

    headers = dict(DOWNLOAD_HEADERS)
    if resume_from > 0:
        headers["Range"] = f"bytes={resume_from}-"

    with requests.get(
        video_url,
        stream=True,
        timeout=(15, 300),
        headers=headers,
    ) as resp:
        if resume_from > 0 and resp.status_code == 416:
            os.remove(local_path)
            return _download_video_stream(video_url, local_path)

        if resume_from > 0 and resp.status_code == 200:
            # 服务端不支持 Range，重新全量下载
            os.remove(local_path)
            resume_from = 0

        resp.raise_for_status()
        mode = "ab" if resume_from > 0 else "wb"
        with open(local_path, mode) as f:
            for chunk in resp.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                if chunk:
                    f.write(chunk)


def download_video(video_url: str, index: int) -> str:
    """下载 mp4，失败时自动重试并断点续传。"""
    local_path = os.path.join(TEMP_DIR, f"temp_video_{index}_{uuid.uuid4().hex[:6]}.mp4")
    last_err = None
    retriable = (
        requests.exceptions.ChunkedEncodingError,
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
    )

    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        try:
            _download_video_stream(video_url, local_path)
            if not os.path.exists(local_path) or os.path.getsize(local_path) == 0:
                raise RuntimeError("下载完成但文件为空")
            return local_path
        except retriable as exc:
            last_err = exc
        except OSError as exc:
            last_err = exc
        except Exception as exc:
            msg = str(exc)
            if "IncompleteRead" in msg or "Connection broken" in msg:
                last_err = exc
            else:
                if os.path.exists(local_path):
                    try:
                        os.remove(local_path)
                    except OSError:
                        pass
                raise

        if attempt < DOWNLOAD_RETRIES:
            time.sleep(3 * attempt)

    if os.path.exists(local_path):
        try:
            os.remove(local_path)
        except OSError:
            pass
    raise RuntimeError(
        f"下载失败（已重试 {DOWNLOAD_RETRIES} 次）：{last_err}"
    ) from last_err


def build_keyword_relevance_prompt(search_keyword: str, *, lenient: bool = False) -> str:
    """Gemini 判定视频：关键词相关性 + 是否全篇 AI。"""
    kw = (search_keyword or "").strip()
    lenient_note = ""
    if lenient:
        lenient_note = (
            f"\n注意：该广告来自 Meta 关键词「{kw}」搜索结果。"
            "除非视频明显属于完全无关品类，否则应判定 relevant=true。\n"
        )
    return f"""请观看这条广告视频（重点看画面、口播、展示的商品/场景），完成两项判定：

1. **relevant** — 是否与搜索关键词「{kw}」明显相关
   - 相关：推广的产品、品类、使用场景、目标受众或品牌与关键词一致或高度接近
   - 不相关：明显其它品类、误推广告、仅广告库配文沾边但视频内容无关

2. **fully_ai** — 是否**全篇**均为 AI / 数字合成生成（无真实实拍画面）
   - fully_ai=true：整段视频几乎都是 AI 虚拟人、AI 场景、纯数字合成，没有真实拍摄素材
   - fully_ai=false：以真实拍摄为主；或真人出镜 + 部分 AI 镜头/特效穿插；或混剪中含少量 AI 片段
{lenient_note}
只输出一行 JSON（不要 markdown）：
{{"relevant": true, "fully_ai": false}}
或
{{"relevant": false, "fully_ai": false, "reason": "简短原因"}}
或
{{"relevant": true, "fully_ai": true, "reason": "全篇AI"}}"""


def _parse_bool_field(val) -> Optional[bool]:
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.strip().lower() in ("true", "yes", "1")
    if isinstance(val, (int, float)):
        return val == 1
    return None


def parse_keyword_relevance_response(text: str) -> tuple[Optional[bool], Optional[bool], str]:
    """解析 Gemini 返回的 relevant / fully_ai 判定。"""
    raw = (text or "").strip()
    if not raw:
        return None, None, ""
    candidates = [raw]
    for pattern in (r"```json\s*(.*?)\s*```", r"```\s*(.*?)\s*```"):
        match = re.search(pattern, raw, re.DOTALL | re.IGNORECASE)
        if match:
            candidates.insert(0, match.group(1).strip())
    for candidate in candidates:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start < 0 or end <= start:
            continue
        try:
            data = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        relevant = _parse_bool_field(data.get("relevant"))
        fully_ai = _parse_bool_field(data.get("fully_ai", data.get("fullyAi")))
        reason = str(data.get("reason") or "").strip()
        if relevant is not None or fully_ai is not None:
            return relevant, fully_ai, reason
        lower = candidate.lower()
        if any(w in lower for w in ("false", "不相关", "无关", "irrelevant", "no")):
            return False, fully_ai, candidate[:120]
        if any(w in lower for w in ("true", "相关", "relevant", "yes")):
            return True, fully_ai, candidate[:120]
    return None, None, ""


def check_video_keyword_relevance_gemini(
    local_video_path: str,
    search_keyword: str,
    *,
    raw_item: Optional[dict] = None,
    model_name: Optional[str] = None,
    on_status: Optional[Callable[[str, str], None]] = None,
    lenient: bool = False,
) -> tuple[bool, bool, str]:
    """用 Gemini 看视频判定：关键词相关性 + 是否全篇 AI。返回 (relevant, fully_ai, reason)。"""
    prompt = build_keyword_relevance_prompt(search_keyword, lenient=lenient)
    video_file = None
    compressed_path = None
    is_temp_compressed = False
    try:
        upload_path, is_temp_compressed = compress_video_for_gemini(
            local_video_path,
            on_status=on_status,
            max_seconds=RELEVANCE_CLIP_SECONDS,
        )
        if is_temp_compressed:
            compressed_path = upload_path
        video_file = upload_and_wait_active(upload_path, on_status=on_status)
        if on_status:
            on_status(
                "Gemini 判定视频相关性及 AI 类型...",
                f"关键词：{search_keyword.strip()} · 前 {RELEVANCE_CLIP_SECONDS}s",
            )
        _, response = _generate_hook_content_with_models(
            video_file,
            prompt,
            model_name=model_name,
            on_status=on_status,
        )
        relevant, fully_ai, reason = parse_keyword_relevance_response(response.text or "")
        if fully_ai is None:
            fully_ai = False
        if relevant is None:
            return True, False, "Meta 关键词搜索结果，默认通过"
        return relevant, fully_ai, reason
    finally:
        if video_file is not None:
            try:
                genai.delete_file(video_file.name)
            except Exception:
                pass
        if is_temp_compressed and compressed_path and os.path.exists(compressed_path):
            try:
                os.remove(compressed_path)
            except OSError:
                pass


def _safe_remove_file(path: str) -> None:
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def _format_fetch_failure_diagnosis(stats: dict) -> str:
    """生成检索失败时的详细诊断文案。"""
    pool_size = stats.get("unique_video_candidates", stats.get("keyword_matched", 0))
    lines = [
        f"Apify 共返回 {stats.get('total_raw_items', stats.get('total_items', 0))} 条广告",
        f"去重后独立视频候选 {pool_size} 条",
        f"已尝试下载验证 {stats.get('candidates_tried', 0)} 条",
        f"剔除全篇AI {stats.get('ai_filtered', 0)} · 超 {MAX_VIDEO_DURATION_SEC}s {stats.get('duration_filtered', 0)} · "
        f"内容不相关 {stats.get('relevance_rejected', 0)} · 下载失败 {stats.get('download_failed', 0)} · "
        f"Gemini 校验失败 {stats.get('gemini_check_failed', 0)}",
    ]
    if stats.get("outside_lookback") or stats.get("no_video") or stats.get("no_start_date"):
        lines.append(
            f"最近一轮预处理排除：无视频 {stats.get('no_video', 0)} · "
            f"无开始日期 {stats.get('no_start_date', 0)} · "
            f"超出时间窗 {stats.get('outside_lookback', 0)}"
            f"（近 {stats.get('lookback_days', FETCH_LOOKBACK_DAYS)} 天）"
        )
    if stats.get("keyword_rejected"):
        lines.append(f"文案二次过滤 {stats.get('keyword_rejected', 0)} 条")
    stages = stats.get("lookback_stages_used")
    if stages:
        lines.append(f"已尝试时间窗（天）: {', '.join(str(d) for d in stages)} · Apify 轮次 {stats.get('fetch_rounds', 0)}")
    if stats.get("fallback_filled"):
        lines.append(f"信任 Meta 搜索补齐 {stats.get('fallback_count', 0)} 条（跳过 Gemini 二次验证）")
    if stats.get("unique_video_candidates"):
        lines.append(f"去重后独立视频候选 {stats.get('unique_video_candidates')} 条")
    return "\n".join(f"- {line}" for line in lines)


def _merge_impression_pool(pool: dict[str, dict], candidates: list[dict]) -> int:
    """按 video_url 合并候选，保留曝光最高的一条。返回新增 URL 数。"""
    new_count = 0
    for ad in candidates:
        vurl = ad.get("video_url") or ""
        if not vurl:
            continue
        prev = pool.get(vurl)
        if prev is None:
            new_count += 1
            pool[vurl] = ad
        elif ad.get("impression_score", -1) > prev.get("impression_score", -1):
            pool[vurl] = ad
    return new_count


def _try_select_video_candidate(
    ad: dict,
    *,
    slot: int,
    keyword: str,
    gemini_model: str,
    validation_stats: dict,
    require_gemini: bool,
    gemini_lenient: bool,
    on_progress: Optional[Callable[[float, str, str], None]] = None,
    ratio: float = 0.5,
) -> Optional[dict]:
    """下载并验证单条候选；成功返回 selected 条目，失败返回 None。"""
    brand_name = _extract_brand_name(ad.get("raw") or {})
    if on_progress:
        on_progress(
            ratio,
            f"[{slot}] 下载并验证候选视频",
            f"{brand_name} · {'Gemini验证' if require_gemini else '信任Meta搜索补齐'}",
        )

    local_path = ""
    try:
        local_path = download_video(ad["video_url"], slot)
    except Exception:
        validation_stats["download_failed"] += 1
        return None

    duration_sec = _get_video_duration_seconds(local_path)
    if duration_sec > MAX_VIDEO_DURATION_SEC:
        validation_stats["duration_filtered"] += 1
        _safe_remove_file(local_path)
        return None

    reason = "Meta 关键词搜索（信任模式补齐）"
    if require_gemini:
        def _rel_status(msg: str, detail: str = ""):
            if on_progress:
                on_progress(ratio + 0.02, msg, detail)

        use_lenient = gemini_lenient or validation_stats["relevance_rejected"] >= 1
        try:
            relevant, fully_ai, reason = check_video_keyword_relevance_gemini(
                local_path,
                keyword,
                raw_item=ad.get("raw") or {},
                model_name=gemini_model,
                on_status=_rel_status,
                lenient=use_lenient,
            )
        except Exception as gemini_exc:
            validation_stats["gemini_check_failed"] += 1
            if TRUST_META_ON_GEMINI_FAILURE:
                reason = f"Gemini 校验异常，信任 Meta 关键词搜索（{gemini_exc}）"
            else:
                _safe_remove_file(local_path)
                return None
        else:
            if fully_ai:
                validation_stats["ai_filtered"] += 1
                _safe_remove_file(local_path)
                return None
            if not relevant:
                validation_stats["relevance_rejected"] += 1
                _safe_remove_file(local_path)
                return None

    return {
        "index": slot,
        "brand_name": brand_name,
        "start_date": ad.get("start_date", ""),
        "video_url": ad["video_url"],
        "local_path": local_path,
        "raw": ad.get("raw") or {},
        "relevance_reason": reason,
    }


def collect_validated_top_videos(
    apify_token: str,
    search_keyword: str,
    country: str,
    *,
    top_n: int = TOP_N,
    gemini_api_key: str,
    gemini_proxy: str = "",
    gemini_model: str = "",
    on_progress: Optional[Callable[[float, str, str], None]] = None,
) -> tuple[list, dict]:
    """抓取 → 下载 → 时长/Gemini 验证；凑不满 top_n 时信任 Meta 搜索补齐。"""
    keyword = (search_keyword or "").strip()
    if not gemini_api_key.strip():
        raise RuntimeError("检索验证需要 Gemini API Key（用于判断视频内容与关键词是否相关）")

    configure_gemini_client(gemini_api_key, gemini_proxy, model=gemini_model)

    selected: list[dict] = []
    selected_video_urls: set[str] = set()
    excluded_ad_ids: set[str] = set()
    strict_tried_urls: set[str] = set()
    impression_pool: dict[str, dict] = {}
    validation_stats = {
        "duration_filtered": 0,
        "relevance_rejected": 0,
        "ai_filtered": 0,
        "download_failed": 0,
        "gemini_check_failed": 0,
        "candidates_tried": 0,
        "fetch_rounds": 0,
        "total_raw_items": 0,
        "lookback_stages_used": [],
        "fallback_filled": False,
        "fallback_count": 0,
        "unique_video_candidates": 0,
    }
    last_rank_stats: dict = {}

    def _progress(ratio: float, step: str, detail: str = ""):
        if on_progress:
            on_progress(ratio, step, detail)

    search_modes = ["keyword_unordered", "keyword_exact_phrase"]

    for stage_idx, lookback_days in enumerate(FETCH_LOOKBACK_STAGES):
        if len(selected) >= top_n:
            break

        if stage_idx > 0:
            prev_days = FETCH_LOOKBACK_STAGES[stage_idx - 1]
            _progress(
                0.06 + 0.04 * stage_idx,
                f"近 {prev_days} 天仅 {len(selected)}/{top_n} 条，放宽至 {lookback_days} 天",
                f"关键词：{keyword}",
            )

        validation_stats["lookback_stages_used"].append(lookback_days)
        results_limit = RESULTS_LIMIT
        allow_missing_start = True
        gemini_lenient = lookback_days > FETCH_LOOKBACK_DAYS

        for fetch_round in range(1, MAX_FETCH_ROUNDS + 1):
            if len(selected) >= top_n:
                break

            validation_stats["fetch_rounds"] += 1
            round_no = validation_stats["fetch_rounds"]
            search_type = search_modes[(fetch_round - 1) % len(search_modes)]
            apply_date_filter = fetch_round == 1
            if fetch_round > 1:
                apply_date_filter = False
                results_limit = min(results_limit + 100, 500)

            _progress(
                0.08 + 0.12 * min(round_no - 1, 8),
                f"Apify 抓取候选池（近 {lookback_days} 天 · 第 {round_no} 轮）",
                f"关键词：{keyword} · 上限 {results_limit} 条 · {search_type}"
                + (" · 宽松 Gemini" if gemini_lenient else ""),
            )
            raw_items = fetch_fb_ads(
                apify_token,
                keyword,
                country,
                results_limit=results_limit,
                search_type=search_type,
                lookback_days=lookback_days,
                apply_date_filter=apply_date_filter,
            )
            validation_stats["total_raw_items"] += len(raw_items)
            # URL 已带日期过滤时不再用 start_date 二次剔除（字段常与 Meta 页面不一致）
            candidates, rank_stats = rank_video_ad_candidates(
                raw_items,
                keyword,
                lookback_days=lookback_days,
                exclude_ad_ids=excluded_ad_ids,
                exclude_video_urls=selected_video_urls,
                require_keyword_in_text=False,
                allow_missing_start_date=allow_missing_start,
                enforce_lookback=not apply_date_filter,
            )
            last_rank_stats = rank_stats
            new_in_round = _merge_impression_pool(impression_pool, candidates)
            validation_stats["unique_video_candidates"] = len(impression_pool)

            ordered = _sort_pool_for_selection(impression_pool, keyword, lookback_days)
            new_strict_tried = 0
            for ad in ordered:
                if len(selected) >= top_n:
                    break
                vurl = ad.get("video_url") or ""
                if not vurl or vurl in selected_video_urls or vurl in strict_tried_urls:
                    continue
                strict_tried_urls.add(vurl)
                validation_stats["candidates_tried"] += 1
                new_strict_tried += 1

                ad_id = ad.get("ad_id") or ""
                if ad_id:
                    excluded_ad_ids.add(ad_id)

                slot = len(selected) + 1
                ratio = 0.25 + min(0.55, 0.55 * validation_stats["candidates_tried"] / max(len(impression_pool), top_n * 4))
                picked = _try_select_video_candidate(
                    ad,
                    slot=slot,
                    keyword=keyword,
                    gemini_model=gemini_model,
                    validation_stats=validation_stats,
                    require_gemini=True,
                    gemini_lenient=gemini_lenient,
                    on_progress=_progress,
                    ratio=ratio,
                )
                if picked:
                    selected.append(picked)
                    selected_video_urls.add(vurl)

            if len(selected) >= top_n:
                break
            # 候选池里还有未尝试的视频时，先不发起下一轮 Apify
            untried_in_pool = sum(
                1
                for ad in impression_pool.values()
                if (ad.get("video_url") or "") not in strict_tried_urls
                and (ad.get("video_url") or "") not in selected_video_urls
            )
            if untried_in_pool > 0:
                continue
            if new_in_round == 0 and new_strict_tried == 0:
                break

    if len(selected) < top_n and FALLBACK_TRUST_META_SEARCH and impression_pool:
        validation_stats["fallback_filled"] = True
        _progress(
            0.82,
            f"严格筛选仅 {len(selected)}/{top_n} 条，信任 Meta 搜索补齐",
            f"候选池 {len(impression_pool)} 个独立视频",
        )
        ordered = sorted(
            impression_pool.values(),
            key=lambda ad: -ad.get("impression_score", -1),
        )
        for ad in ordered:
            if len(selected) >= top_n:
                break
            vurl = ad.get("video_url") or ""
            if not vurl or vurl in selected_video_urls:
                continue
            slot = len(selected) + 1
            picked = _try_select_video_candidate(
                ad,
                slot=slot,
                keyword=keyword,
                gemini_model=gemini_model,
                validation_stats=validation_stats,
                require_gemini=False,
                gemini_lenient=True,
                on_progress=_progress,
                ratio=0.86,
            )
            if picked:
                selected.append(picked)
                selected_video_urls.add(vurl)
                validation_stats["fallback_count"] += 1

    for idx, item in enumerate(selected, start=1):
        item["index"] = idx

    combined_stats = {**last_rank_stats, **validation_stats, "selected": len(selected)}
    return selected, combined_stats


def _find_ffmpeg() -> Optional[str]:
    """优先使用 imageio-ffmpeg 内置二进制，其次系统 PATH。"""
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")


def _format_mb(path: str) -> str:
    try:
        size = os.path.getsize(path) / (1024 * 1024)
        return f"{size:.1f} MB"
    except OSError:
        return "?"


def compress_video_for_gemini(
    input_path: str,
    on_status: Optional[Callable[[str, str], None]] = None,
    max_seconds: Optional[int] = None,
) -> tuple:
    """
    压缩/降分辨率后再上传 Gemini，加快上传与云端处理。
    返回 (用于上传的路径, 是否为临时压缩文件)。
    """
    clip_seconds = GEMINI_HOOK_CLIP_SECONDS if max_seconds is None else max_seconds
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        if on_status:
            on_status(
                "未找到 ffmpeg，将使用原视频上传",
                "可安装 imageio-ffmpeg 或系统 ffmpeg 以启用压缩",
            )
        return input_path, False

    output_path = os.path.join(TEMP_DIR, f"gemini_{uuid.uuid4().hex[:8]}.mp4")
    if on_status:
        on_status(
            "正在压缩视频（降分辨率 + 截断）...",
            f"原片 {_format_mb(input_path)} → {GEMINI_VIDEO_MAX_HEIGHT}p · 前 {clip_seconds}s",
        )

    cmd = [
        ffmpeg,
        "-y",
        "-i",
        input_path,
        "-t",
        str(clip_seconds),
        "-vf",
        f"scale=-2:{GEMINI_VIDEO_MAX_HEIGHT}",
        "-c:v",
        "libx264",
        "-crf",
        str(GEMINI_VIDEO_CRF),
        "-preset",
        GEMINI_VIDEO_PRESET,
        "-c:a",
        "aac",
        "-b:a",
        GEMINI_AUDIO_BITRATE,
        "-movflags",
        "+faststart",
        output_path,
    ]

    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        if on_status:
            on_status("压缩失败，改用原视频上传", str(exc)[:120])
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except Exception:
                pass
        return input_path, False

    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        if on_status:
            on_status("压缩输出为空，改用原视频上传", "")
        return input_path, False

    if on_status:
        on_status(
            "视频压缩完成",
            f"{_format_mb(input_path)} → {_format_mb(output_path)}",
        )
    return output_path, True


# ------------------------------------------------------------------
# 模块四：Gemini 浅捞表格（对齐「捞视频.xlsx / 浅捞」sheet）
# ------------------------------------------------------------------
SHALLOW_TABLE_COLUMNS = [
    "视频编号",
    "视频/网址",
    "渠道来源",
    "视频时长",
    "Impression",
    "商品类目",
    "品牌名称",
    "品牌网站",
    "Hook类型",
    "HookVO",
    "Text Hook",
    "镜头切换频率",
    "前三秒镜头数",
    "设备 | 光线",
    "场景",
    "口播情绪基调",
    "音乐、音效、卡点",
    "CTA类型",
    "个人见解",
]

GEMINI_MODEL = "gemini-3.5-flash"
GEMINI_MODEL_SESSION_KEY = "gemini_model_selected"

# 已下线 / 弱模型：不提供选择，secrets 或旧会话命中时自动回退
GEMINI_VIDEO_MODEL_BLOCKLIST = frozenset({
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash-lite",
    "gemini-2.0-flash",
    "gemini-2.0-flash-001",
    "gemini-1.5-flash",
    "gemini-1.5-flash-8b",
    "gemini-1.5-pro",
    "gemini-1.5-pro-latest",
})

# 支持视频 File API 且当前可用的 Gemini 模型（2026-06 起 1.5 / 2.0 已下线）
GEMINI_VIDEO_MODEL_OPTIONS: list[dict[str, str]] = [
    {
        "id": "gemini-3.5-flash",
        "label": "3.5 Flash（默认 · 最新 · 视频理解最好）",
    },
    {
        "id": "gemini-2.5-flash",
        "label": "2.5 Flash（均衡 · 稳定）",
    },
    {
        "id": "gemini-2.5-pro",
        "label": "2.5 Pro（质量最高 · 较慢 · 配额较少）",
    },
    {
        "id": "gemini-3.1-pro-preview",
        "label": "3.1 Pro Preview（预览 · 高质量）",
    },
]

# 404 时按顺序自动回退
GEMINI_VIDEO_MODEL_FALLBACKS = (
    "gemini-3.5-flash",
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-3.1-pro-preview",
)


def _is_capable_gemini_video_model(model_id: str) -> bool:
    """排除已下线 / Lite 等不可用模型。"""
    mid = (model_id or "").strip().lower()
    if not mid:
        return False
    if mid in GEMINI_VIDEO_MODEL_BLOCKLIST:
        return False
    if mid.startswith("gemini-1.5") or mid.startswith("gemini-1.0"):
        return False
    if mid.startswith("gemini-2.0"):
        return False
    if "-lite" in mid or mid.endswith("-lite"):
        return False
    return True


def _gemini_model_candidates(preference: str = "") -> list[str]:
    """当前模型 + 可用回退链（去重）。"""
    primary = resolve_gemini_model(preference)
    chain: list[str] = []
    for mid in (primary, *GEMINI_VIDEO_MODEL_FALLBACKS):
        if mid and mid not in chain and _is_capable_gemini_video_model(mid):
            chain.append(mid)
    return chain or [GEMINI_MODEL]


def get_gemini_video_model_options() -> list[dict[str, str]]:
    """返回可选视频模型；secrets.toml 中的自定义 model 会追加到列表（Lite 除外）。"""
    options = list(GEMINI_VIDEO_MODEL_OPTIONS)
    known = {item["id"] for item in options}
    secret_model = (_secret("gemini", "model") or "").strip()
    if secret_model and secret_model not in known and _is_capable_gemini_video_model(secret_model):
        options.append(
            {
                "id": secret_model,
                "label": f"{secret_model}（secrets.toml 自定义）",
            }
        )
    return options


def get_gemini_model_label(model_id: str) -> str:
    for item in get_gemini_video_model_options():
        if item["id"] == model_id:
            return item["label"]
    return model_id


def resolve_gemini_model(preference: str = "") -> str:
    """侧边栏选择 > 调用参数 > secrets.toml > 默认（跳过 Lite 等弱模型）。"""
    pref = (preference or "").strip()
    if pref and _is_capable_gemini_video_model(pref):
        return pref
    try:
        session_model = str(st.session_state.get(GEMINI_MODEL_SESSION_KEY, "") or "").strip()
    except Exception:
        session_model = ""
    known = {item["id"] for item in get_gemini_video_model_options()}
    if session_model in known and _is_capable_gemini_video_model(session_model):
        return session_model
    secret_model = (_secret("gemini", "model") or "").strip()
    if secret_model and _is_capable_gemini_video_model(secret_model):
        return secret_model
    runtime_model = (_GEMINI_RUNTIME.get("model") or "").strip()
    if runtime_model and _is_capable_gemini_video_model(runtime_model):
        return runtime_model
    return GEMINI_MODEL

# J/K 列必须来自视频前 3 秒，禁止用广告库配文填充
SHALLOW_VIDEO_HOOK_COLUMNS = ("HookVO", "Text Hook")
# 本地 ffmpeg 可准确填写的列
SHALLOW_LOCAL_TECH_COLUMNS = (
    "视频时长",
    "镜头切换频率",
    "前三秒镜头数",
    "设备 | 光线",
)
SHALLOW_LOCAL_HEURISTIC_COLUMNS = (
    "Hook类型",
    "场景",
    "口播情绪基调",
    "音乐、音效、卡点",
)
# Gemini 只应覆盖 J/K 列；其余列由本地 ffmpeg + Apify 负责
SHALLOW_GEMINI_HOOK_COLUMNS = SHALLOW_VIDEO_HOOK_COLUMNS
SHALLOW_ALWAYS_BLANK_COLUMNS = frozenset({"个人见解"})
GEMINI_VIDEO_MAX_HEIGHT = 480
GEMINI_HOOK_CLIP_SECONDS = 3  # 仅分析前 3 秒：HookVO=台词，Text Hook=画面字幕
GEMINI_VIDEO_MAX_SECONDS = GEMINI_HOOK_CLIP_SECONDS
GEMINI_VIDEO_CRF = 28
GEMINI_VIDEO_PRESET = "veryfast"
GEMINI_AUDIO_BITRATE = "64k"
GEMINI_API_TIMEOUT_SEC = 600
GEMINI_UPLOAD_RETRIES = 3
GEMINI_UPLOAD_PROXY_ATTEMPTS = 3
GEMINI_UPLOAD_CHUNK_BYTES = 4 * 1024 * 1024  # 分块上传，降低代理 SSL 中断概率
GEMINI_API_HOST = "generativelanguage.googleapis.com"
GEMINI_UPLOAD_URL = f"https://{GEMINI_API_HOST}/upload/v1beta/files"
GEMINI_API_BASE_URL = f"https://{GEMINI_API_HOST}/v1beta"
COMMON_LOCAL_PROXY_PORTS = (7897, 7890, 1087, 10808, 1080, 8080, 33210, 6152)
GEMINI_PROXY_SESSION_KEY = "gemini_proxy_resolved"
APIFY_TOKEN_SESSION_KEY = "apify_token_field"
GEMINI_PROXY_SOURCE_KEY = "gemini_proxy_source"
LAST_EXPORT_KEY = "last_export_bundle"
_GEMINI_RUNTIME: dict[str, str] = {"api_key": "", "proxy": "", "model": ""}
_GEMINI_FILE_PROTO_FIELDS = frozenset({
    "name",
    "display_name",
    "mime_type",
    "size_bytes",
    "create_time",
    "update_time",
    "expiration_time",
    "sha256_hash",
    "uri",
    "state",
    "error",
    "video_metadata",
})
_GEMINI_VIDEO_METADATA_FIELDS = frozenset({"video_duration"})

DEFAULT_BRAND_WEBSITE = "https://www.nuagewears.com"
DEFAULT_BRAND_CONTEXT = """【我方品牌：Nuage · nuagewears.com】
- 定位：女性运动/日常舒适内裤与 wireless bras（Barely there Undies / Performance Underwear）
- 产品线：NuCloud（瑜伽/日常）、NuAir（户外/高强度）、NuShield（防骆驼趾）、NuForme（轻度塑形）、Wireless Bras
- 核心卖点：不卡档/不卷边/不勒腰（Pivot+ 腰头不滑落）、无痕 No VPL、透气、3D 立体剪裁、「穿上几乎忘记存在」
- 用户痛点：运动时摩擦、VPL、腰头往下滑/卷边、传统 thong 不适
- 品牌调性：舒适+运动表现、真实口碑（1000+ 五星）、生活化场景而非硬广"""


def get_brand_reference_context() -> str:
    """浅捞「个人见解」借鉴时使用的我方品牌背景（可 secrets.toml 覆盖）。"""
    custom = _secret("brand", "context")
    if custom:
        return custom.strip()
    website = _secret("brand", "website", DEFAULT_BRAND_WEBSITE) or DEFAULT_BRAND_WEBSITE
    return f"官网：{website}\n{DEFAULT_BRAND_CONTEXT}"


def _normalize_proxy_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if "://" not in url:
        return f"http://{url}"
    return url


def _read_macos_system_proxies() -> list[str]:
    """读取 macOS 系统网络代理设置（HTTP/HTTPS/SOCKS）。"""
    try:
        proc = subprocess.run(
            ["scutil", "--proxy"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode != 0:
            return []
    except Exception:
        return []

    text = proc.stdout or ""
    entries = {}
    for line in text.splitlines():
        match = re.match(r"\s*(\w+)\s*:\s*(.+)", line.strip())
        if match:
            entries[match.group(1)] = match.group(2).strip()

    def _enabled(key: str) -> bool:
        return entries.get(key) in ("1", "true", "True", "yes")

    urls = []
    if _enabled("HTTPSEnable") and entries.get("HTTPSProxy"):
        port = entries.get("HTTPSPort", "443")
        urls.append(f"http://{entries['HTTPSProxy']}:{port}")
    if _enabled("HTTPEnable") and entries.get("HTTPProxy"):
        port = entries.get("HTTPPort", "80")
        urls.append(f"http://{entries['HTTPProxy']}:{port}")
    if _enabled("SOCKSEnable") and entries.get("SOCKSProxy"):
        port = entries.get("SOCKSPort", "1080")
        # Clash 等工具的 SOCKS 端口通常也接受 HTTP CONNECT
        urls.append(f"http://{entries['SOCKSProxy']}:{port}")
    return urls


def _local_port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.35) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _probe_https_proxy(proxy_url: str, timeout: float = 4.0) -> bool:
    proxy_url = _normalize_proxy_url(proxy_url)
    if not proxy_url:
        return False
    try:
        resp = requests.head(
            f"https://{GEMINI_API_HOST}/",
            proxies={"http": proxy_url, "https": proxy_url},
            timeout=timeout,
            allow_redirects=True,
        )
        return resp.status_code < 500
    except Exception:
        return False


def auto_detect_https_proxy() -> tuple[str, str]:
    """
    按当前网络环境探测可用 HTTPS 代理。
    返回 (proxy_url, source_label)。
    """
    candidates: list[tuple[str, str]] = []

    secret = _secret("gemini", "proxy_url")
    if secret:
        candidates.append((_normalize_proxy_url(secret), "secrets.toml"))

    for env_key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        val = os.environ.get(env_key, "").strip()
        if val:
            candidates.append((_normalize_proxy_url(val), f"环境变量 {env_key}"))

    for url in _read_macos_system_proxies():
        candidates.append((_normalize_proxy_url(url), "macOS 系统代理"))

    for port in COMMON_LOCAL_PROXY_PORTS:
        if _local_port_open(port):
            candidates.append((f"http://127.0.0.1:{port}", f"本机端口 {port}"))

    seen = set()
    for url, source in candidates:
        if not url or url in seen:
            continue
        seen.add(url)
        if _probe_https_proxy(url):
            return url, source

    return "", "未检测到可用代理（直连 Google 失败）"


def init_gemini_proxy_for_session(force: bool = False) -> tuple[str, str]:
    """每个浏览器会话首次打开页面时自动检测并配置代理。"""
    if not force and GEMINI_PROXY_SESSION_KEY in st.session_state:
        return (
            st.session_state.get(GEMINI_PROXY_SESSION_KEY, ""),
            st.session_state.get(GEMINI_PROXY_SOURCE_KEY, ""),
        )

    proxy_url, source = auto_detect_https_proxy()
    st.session_state[GEMINI_PROXY_SESSION_KEY] = proxy_url
    st.session_state[GEMINI_PROXY_SOURCE_KEY] = source
    if proxy_url:
        apply_gemini_proxy(proxy_url)
    return proxy_url, source


def _on_redetect_gemini_proxy() -> None:
    """重新检测代理（须在 on_click 回调中改 widget 绑定的 session_state）。"""
    url, source = init_gemini_proxy_for_session(force=True)
    st.session_state.gemini_proxy_field = url
    st.session_state[GEMINI_PROXY_SOURCE_KEY] = source


def _resolve_gemini_proxy(proxy_url: str = "") -> str:
    """解析当前应使用的 HTTPS 代理地址。"""
    return (
        proxy_url.strip()
        or _secret("gemini", "proxy_url")
        or os.environ.get("HTTPS_PROXY", "")
        or os.environ.get("HTTP_PROXY", "")
    ).strip()


def _force_direct_gemini_network() -> None:
    """上传/请求强制直连（Clash TUN 场景下避免双重代理导致 SSL 失败）。"""
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        os.environ.pop(key, None)
    _GEMINI_RUNTIME["proxy"] = ""


def apply_gemini_proxy(proxy_url: str = "") -> str:
    """设置 HTTPS 代理，供 requests / google-generativeai 使用。"""
    proxy = _normalize_proxy_url(_resolve_gemini_proxy(proxy_url))
    if proxy:
        os.environ["HTTP_PROXY"] = proxy
        os.environ["HTTPS_PROXY"] = proxy
        os.environ["http_proxy"] = proxy
        os.environ["https_proxy"] = proxy
    else:
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            os.environ.pop(key, None)
    return proxy


def _requests_proxies(proxy_url: Optional[str] = None) -> Optional[dict[str, str]]:
    """构建 requests 代理 dict。None=自动；空字符串=强制直连。"""
    if proxy_url is None:
        resolved = _normalize_proxy_url(
            _GEMINI_RUNTIME.get("proxy", "") or _resolve_gemini_proxy()
        )
    else:
        resolved = _normalize_proxy_url(proxy_url)
    if not resolved:
        return None
    return {"http": resolved, "https": resolved}


def _gemini_upload_proxy_candidates(primary: str = "") -> list[tuple[str, str]]:
    """上传失败时依次尝试的代理（含直连，适配 Clash TUN）。"""
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(url: str, label: str) -> None:
        normalized = _normalize_proxy_url(url) if url else ""
        key = normalized or "__direct__"
        if key in seen:
            return
        seen.add(key)
        candidates.append((normalized, label))

    add(primary or _GEMINI_RUNTIME.get("proxy", ""), "当前代理")
    add(_secret("gemini", "proxy_url"), "secrets.toml")
    for port in COMMON_LOCAL_PROXY_PORTS:
        if _local_port_open(port):
            add(f"http://127.0.0.1:{port}", f"本机端口 {port}")
    add("", "系统直连（Clash TUN / 全局 VPN）")
    return candidates


def _gemini_requests_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST", "HEAD"]),
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _sanitize_api_error(msg: str) -> str:
    """错误信息中隐藏 API Key。"""
    return re.sub(r"key=[^&\s\"']+", "key=***", msg or "")


def _is_retryable_upload_error(exc: Exception) -> bool:
    if isinstance(
        exc,
        (
            OSError,
            TimeoutError,
            ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
            requests.exceptions.SSLError,
            requests.exceptions.ChunkedEncodingError,
            ssl.SSLError,
        ),
    ):
        return True
    text = str(exc).lower()
    return any(
        token in text
        for token in ("ssl", "eof occurred", "timed out", "timeout", "connection reset", "max retries")
    )


def _camel_to_snake_key(key: str) -> str:
    if not key or key.islower() or "_" in key:
        return key
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key).lower()


def _normalize_gemini_rest_dict(data: dict, *, _parent: str = "file") -> dict:
    """Gemini REST JSON 为 camelCase，protobuf File 需要 snake_case，且须过滤未知字段。"""
    allowed = (
        _GEMINI_VIDEO_METADATA_FIELDS
        if _parent == "video_metadata"
        else _GEMINI_FILE_PROTO_FIELDS
    )
    out: dict = {}
    for key, value in data.items():
        snake_key = _camel_to_snake_key(key)
        if snake_key not in allowed:
            continue
        if isinstance(value, dict):
            out[snake_key] = _normalize_gemini_rest_dict(value, _parent=snake_key)
        elif snake_key == "size_bytes" and value is not None:
            try:
                out[snake_key] = int(value)
            except (TypeError, ValueError):
                out[snake_key] = value
        else:
            out[snake_key] = value
    return out


def _file_from_gemini_json(data: dict):
    from google.generativeai.types import file_types

    return file_types.File(_normalize_gemini_rest_dict(data))


def upload_file_via_requests(
    local_video_path: str,
    *,
    api_key: str,
    proxy_url: Optional[str] = None,
    display_name: Optional[str] = None,
    session: Optional[requests.Session] = None,
):
    """
    通过 requests 走 HTTPS 代理上传视频（绕过 genai SDK 的 httplib2 直连问题）。
    使用 Gemini File API 可恢复上传协议；大文件分块上传。
    """
    api_key = (api_key or "").strip()
    if not api_key:
        raise ValueError("Gemini API Key 未配置")

    path = local_video_path
    mime_type, _ = mimetypes.guess_type(path)
    mime_type = mime_type or "video/mp4"
    num_bytes = os.path.getsize(path)
    display_name = display_name or os.path.basename(path)
    proxies = _requests_proxies(proxy_url)
    timeout = GEMINI_API_TIMEOUT_SEC
    http = session or _gemini_requests_session()
    own_session = session is None

    try:
        start_resp = http.post(
            f"{GEMINI_UPLOAD_URL}?key={api_key}",
            headers={
                "X-Goog-Upload-Protocol": "resumable",
                "X-Goog-Upload-Command": "start",
                "X-Goog-Upload-Header-Content-Length": str(num_bytes),
                "X-Goog-Upload-Header-Content-Type": mime_type,
                "Content-Type": "application/json",
            },
            json={"file": {"display_name": display_name}},
            proxies=proxies,
            timeout=timeout,
        )
        start_resp.raise_for_status()
        upload_url = start_resp.headers.get("x-goog-upload-url") or start_resp.headers.get(
            "X-Goog-Upload-Url"
        )
        if not upload_url:
            raise RuntimeError("Gemini 上传初始化失败：未返回 x-goog-upload-url")

        offset = 0
        payload = None
        with open(path, "rb") as f:
            while offset < num_bytes:
                chunk = f.read(min(GEMINI_UPLOAD_CHUNK_BYTES, num_bytes - offset))
                if not chunk:
                    break
                is_final = offset + len(chunk) >= num_bytes
                upload_resp = http.post(
                    upload_url,
                    headers={
                        "Content-Length": str(len(chunk)),
                        "X-Goog-Upload-Offset": str(offset),
                        "X-Goog-Upload-Command": "upload, finalize" if is_final else "upload",
                    },
                    data=chunk,
                    proxies=proxies,
                    timeout=timeout,
                )
                upload_resp.raise_for_status()
                offset += len(chunk)
                if is_final:
                    payload = upload_resp.json()
                    break

        if payload is None:
            raise RuntimeError("Gemini 上传未完成：未收到 finalize 响应")

        file_info = payload.get("file", payload)
        return _file_from_gemini_json(file_info)
    finally:
        if own_session:
            http.close()


def get_file_via_requests(
    name: str,
    *,
    api_key: str,
    proxy_url: Optional[str] = None,
):
    """通过 requests 查询 Gemini 文件状态（轮询 PROCESSING -> ACTIVE）。"""
    api_key = (api_key or "").strip()
    if "/" not in name:
        name = f"files/{name}"
    resp = requests.get(
        f"{GEMINI_API_BASE_URL}/{name}",
        params={"key": api_key},
        proxies=_requests_proxies(proxy_url if proxy_url is not None else _GEMINI_RUNTIME.get("proxy")),
        timeout=60,
    )
    resp.raise_for_status()
    return _file_from_gemini_json(resp.json())


def check_gemini_connectivity(proxy_url: str = "", timeout: float = 8.0) -> tuple[bool, str]:
    """检测 Gemini API 连通性（含上传端点握手，不仅 HEAD 根路径）。"""
    proxy = apply_gemini_proxy(proxy_url)
    proxies = {"http": proxy, "https": proxy} if proxy else None
    test_url = f"https://{GEMINI_API_HOST}/"
    try:
        resp = requests.head(test_url, timeout=timeout, proxies=proxies, allow_redirects=True)
        if resp.status_code >= 500:
            return False, f"Google API 返回 HTTP {resp.status_code}"
    except requests.exceptions.ProxyError as exc:
        return False, f"代理不可用：{_sanitize_api_error(str(exc))}"
    except requests.exceptions.SSLError as exc:
        hint = (
            "SSL 握手失败。若使用 Clash Verge：确认代理端口（常见 7897）正确；"
            "若已开 TUN 模式，可清空侧边栏代理改试「系统直连」。"
        )
        return False, f"{hint}\n详情：{_sanitize_api_error(str(exc))}"
    except requests.exceptions.ConnectTimeout:
        hint = "连接 generativelanguage.googleapis.com 超时"
        if not proxy:
            hint += "。国内网络通常需要 VPN 或在侧边栏填写 HTTPS 代理（如 http://127.0.0.1:7897）"
        return False, hint
    except requests.exceptions.ConnectionError as exc:
        msg = _sanitize_api_error(str(exc))
        if not proxy and ("timed out" in msg.lower() or "errno 60" in msg.lower()):
            return False, (
                "无法连接 Google API（网络超时）。"
                "请开启 VPN，或在侧边栏填写 HTTPS 代理后重试。"
            )
        return False, msg
    except Exception as exc:
        return False, _sanitize_api_error(str(exc))

    api_key = (_GEMINI_RUNTIME.get("api_key") or _secret("gemini", "api_key") or "").strip()
    if not api_key:
        return True, ""

    try:
        upload_probe = requests.post(
            f"{GEMINI_UPLOAD_URL}?key={api_key}",
            headers={
                "X-Goog-Upload-Protocol": "resumable",
                "X-Goog-Upload-Command": "start",
                "X-Goog-Upload-Header-Content-Length": "1",
                "X-Goog-Upload-Header-Content-Type": "video/mp4",
                "Content-Type": "application/json",
            },
            json={"file": {"display_name": "connectivity_probe"}},
            proxies=proxies,
            timeout=timeout,
        )
        if upload_probe.status_code in (200, 400, 401, 403):
            return True, ""
        if upload_probe.status_code >= 500:
            return False, f"Gemini 上传端点返回 HTTP {upload_probe.status_code}"
        return True, ""
    except requests.exceptions.SSLError as exc:
        hint = (
            "Gemini 上传端点 SSL 失败。建议：① 侧边栏代理填 http://127.0.0.1:7897；"
            "② 若 Clash 已开 TUN，清空代理用直连；③ 换节点后点「重新检测代理」。"
        )
        return False, f"{hint}\n详情：{_sanitize_api_error(str(exc))}"
    except requests.exceptions.RequestException as exc:
        return False, _sanitize_api_error(str(exc))
    except Exception as exc:
        return False, _sanitize_api_error(str(exc))


def configure_gemini_client(
    api_key: str,
    proxy_url: str = "",
    model: str = "",
) -> None:
    """配置 Gemini 客户端：代理、REST 传输、运行时上下文。"""
    resolved_proxy = apply_gemini_proxy(proxy_url)
    _GEMINI_RUNTIME["api_key"] = api_key.strip()
    _GEMINI_RUNTIME["proxy"] = resolved_proxy
    _GEMINI_RUNTIME["model"] = resolve_gemini_model(model)
    socket.setdefaulttimeout(GEMINI_API_TIMEOUT_SEC)
    # generate_content / delete_file 走 REST + 环境代理；上传/轮询走 requests 直连实现
    genai.configure(api_key=api_key, transport="rest")


def _gemini_supports_thinking_budget(model_name: str) -> bool:
    name = (model_name or "").lower()
    return "2.5" in name or name.startswith("gemini-3")


def _gemini_error_hint(exc: Exception) -> str:
    msg = _sanitize_api_error(str(exc))
    lower = msg.lower()
    if _is_gemini_quota_error(exc):
        return _gemini_quota_hint(exc)
    if "ssl" in lower or "eof occurred" in lower:
        return (
            f"{msg}\n\n"
            "💡 Gemini 上传 SSL 失败，常见原因与处理：\n"
            "1. Clash Verge：侧边栏代理填 http://127.0.0.1:7897，并确认节点可访问 Google\n"
            "2. 若已开启 Clash **TUN 模式**：清空侧边栏代理，改用系统直连\n"
            "3. 点击侧边栏「重新检测代理」后重试\n"
            "4. 仍失败可先用「本地免费填表」（零 Token，不依赖 Gemini）"
        )
    if "timed out" in lower or "errno 60" in lower or "timeout" in lower:
        return (
            f"{msg}\n\n"
            "💡 网络连接 Google 超时。建议：\n"
            "1. 开启 VPN，或在侧边栏填写 HTTPS 代理（如 http://127.0.0.1:7897）\n"
            "2. 确认代理/VPN 能访问 generativelanguage.googleapis.com\n"
            "3. 换网络后重试\n"
            f"4. 视频已自动压缩并截断至前 {GEMINI_HOOK_CLIP_SECONDS} 秒；若仍失败可换更短视频"
        )
    if "404" in msg or "not found" in lower or "is not supported for generatecontent" in lower:
        return (
            f"{msg}\n\n"
            "💡 该 Gemini 模型已下线或不可用。请在侧边栏改用 **3.5 Flash** 或 **2.5 Flash**。"
        )
    return msg


def _gemini_timeout_hint(exc: Exception) -> str:
    """兼容旧调用名。"""
    return _gemini_error_hint(exc)


def _is_gemini_quota_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return (
        "429" in msg
        or "quota" in msg
        or "rate limit" in msg
        or "rate-limit" in msg
        or "resource_exhausted" in msg
        or "exceeded your current quota" in msg
    )


def _gemini_quota_hint(exc: Exception) -> str:
    model = resolve_gemini_model()
    return (
        f"Gemini API 额度已用尽（当前模型：{model}；免费层限额因模型而异）。\n\n"
        "💡 建议：\n"
        "1. 改用「本地免费填表」模式（零限额，推荐）\n"
        "2. 侧边栏换用其他 Gemini 视频模型（各模型配额独立）\n"
        "3. 等待至明日额度重置后再试\n"
        "4. 在 [Google AI Studio](https://aistudio.google.com) 开通付费"
    )


def _gemini_quota_short_note() -> str:
    model = resolve_gemini_model()
    return f"Gemini 额度已用尽（{model}），本条 J/K 列未识别"


def _extract_brand_website(item: dict) -> str:
    snapshot = item.get("snapshot") or {}
    if not isinstance(snapshot, dict):
        return ""
    for key in ("linkUrl", "link_url", "caption", "linkDescription"):
        val = snapshot.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _parse_impression_magnitude(text: str) -> float:
    """将 Meta 曝光区间文本转为可排序数值（取区间上界，越大表示曝光越高）。"""
    if not text:
        return -1.0
    raw = str(text).strip().upper()
    raw = raw.replace("–", "-").replace("—", "-")
    raw = re.sub(r"\s+", "", raw)

    multipliers = {"K": 1_000.0, "M": 1_000_000.0, "B": 1_000_000_000.0}

    def _to_number(token: str) -> float:
        token = token.strip().replace(",", "")
        if not token:
            return -1.0
        for suffix, mult in multipliers.items():
            if token.endswith(suffix):
                try:
                    return float(token[:-1]) * mult
                except ValueError:
                    return -1.0
        digits = re.sub(r"[^\d.]", "", token)
        if not digits:
            return -1.0
        try:
            return float(digits)
        except ValueError:
            return -1.0

    if raw.startswith("<"):
        val = _to_number(raw[1:])
        return val - 1 if val >= 0 else -1.0

    if raw.endswith("+"):
        return _to_number(raw[:-1])

    if "-" in raw:
        parts = [p for p in raw.split("-") if p]
        values = [_to_number(p) for p in parts]
        values = [v for v in values if v >= 0]
        return max(values) if values else -1.0

    val = _to_number(raw)
    return val if val >= 0 else -1.0


def _extract_impression_sort_key(item: dict) -> float:
    """曝光量排序键，数值越大表示曝光越高；无数据时返回 -1。"""
    impressions = item.get("impressionsWithIndex") or item.get("impressions_with_index") or {}
    if isinstance(impressions, dict):
        text_score = _parse_impression_magnitude(str(impressions.get("impressionsText") or ""))
        if text_score >= 0:
            return text_score
        idx = impressions.get("impressionsIndex")
        if isinstance(idx, (int, float)) and idx >= 0:
            return float(idx)

    for key in ("impressions", "impressionsRange", "impressions_range"):
        val = item.get(key)
        if val not in (None, "", {}):
            score = _parse_impression_magnitude(str(val))
            if score >= 0:
                return score

    for key in ("reachEstimate", "reach_estimate"):
        val = item.get(key)
        if val not in (None, "", {}):
            score = _parse_impression_magnitude(str(val))
            if score >= 0:
                return score

    spend = item.get("spend")
    if isinstance(spend, (int, float)) and spend > 0:
        return float(spend)

    return -1.0


def _extract_impression_label(item: dict) -> str:
    impressions = item.get("impressionsWithIndex") or {}
    if isinstance(impressions, dict):
        text = impressions.get("impressionsText")
        if text:
            return str(text)
    for key in ("impressions", "impressionsRange", "reach_estimate"):
        val = item.get(key)
        if val not in (None, "", {}):
            return str(val)
    return "未知"


def _extract_ad_copy_text(item: dict) -> str:
    """从 Apify 广告 snapshot 提取文案，用作 Text Hook 参考。"""
    snapshot = item.get("snapshot") or {}
    parts = []

    def _add(val):
        if isinstance(val, str) and val.strip():
            text = val.strip()
            if text not in parts:
                parts.append(text)
        elif isinstance(val, dict):
            for key in ("text", "markup", "body"):
                nested = val.get(key)
                if isinstance(nested, str) and nested.strip():
                    text = nested.strip()
                    if text not in parts:
                        parts.append(text)

    if isinstance(snapshot, dict):
        for key in ("title", "body", "linkDescription", "caption"):
            _add(snapshot.get(key))
        for card in snapshot.get("cards") or []:
            if isinstance(card, dict):
                for key in ("title", "body"):
                    _add(card.get(key))

    for key in ("title", "body", "adCreativeBody"):
        _add(item.get(key))

    return " / ".join(parts)


def _extract_cta_text(item: dict) -> str:
    snapshot = item.get("snapshot") or {}
    if isinstance(snapshot, dict):
        cta = snapshot.get("ctaText") or snapshot.get("cta_text") or ""
        if isinstance(cta, str) and cta.strip():
            return cta.strip()
        for card in snapshot.get("cards") or []:
            if isinstance(card, dict):
                cta = card.get("ctaText") or card.get("cta_text") or ""
                if isinstance(cta, str) and cta.strip():
                    return cta.strip()
    return ""


def _get_video_duration_seconds(local_path: str) -> float:
    if not local_path or not os.path.exists(local_path):
        return 0.0
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        return 0.0
    try:
        proc = subprocess.run(
            [ffmpeg, "-i", local_path, "-f", "null", "-"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        combined = (proc.stderr or "") + (proc.stdout or "")
        match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", combined)
        if not match:
            return 0.0
        hours, minutes, seconds = int(match.group(1)), int(match.group(2)), float(match.group(3))
        return hours * 3600 + minutes * 60 + seconds
    except Exception:
        return 0.0


def _format_duration_label(seconds: float) -> str:
    if seconds <= 0:
        return "未知"
    if seconds >= 60:
        return f"{int(seconds // 60)}:{int(seconds % 60):02d}"
    return f"{int(seconds)}s"


def _get_video_duration_label(local_path: str) -> str:
    return _format_duration_label(_get_video_duration_seconds(local_path))


def _probe_video_dimensions(local_path: str) -> tuple[int, int]:
    ffmpeg = _find_ffmpeg()
    if not ffmpeg or not os.path.exists(local_path):
        return 0, 0
    try:
        proc = subprocess.run(
            [ffmpeg, "-i", local_path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        stderr = proc.stderr or ""
        match = re.search(r"Video:.*? (\d{2,5})x(\d{2,5})", stderr)
        if match:
            return int(match.group(1)), int(match.group(2))
    except Exception:
        pass
    return 0, 0


def _detect_scene_cut_times(local_path: str, max_seconds: Optional[float] = None) -> list[float]:
    ffmpeg = _find_ffmpeg()
    if not ffmpeg or not os.path.exists(local_path):
        return []
    cmd = [ffmpeg, "-hide_banner", "-i", local_path]
    if max_seconds:
        cmd.extend(["-t", str(max_seconds)])
    cmd.extend(["-vf", "select='gt(scene,0.32)',showinfo", "-an", "-f", "null", "-"])
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        times = []
        for line in (proc.stderr or "").splitlines():
            match = re.search(r"pts_time:([\d.]+)", line)
            if match:
                times.append(float(match.group(1)))
        return times
    except Exception:
        return []


def _estimate_brightness_label(local_path: str) -> str:
    ffmpeg = _find_ffmpeg()
    if not ffmpeg or not os.path.exists(local_path):
        return ""
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-i",
        local_path,
        "-t",
        "3",
        "-an",
        "-vf",
        "signalstats,metadata=print",
        "-f",
        "null",
        "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        values = [float(v) for v in re.findall(r"lavfi\.signalstats\.YAVG=([\d.]+)", proc.stderr or "")]
        if not values:
            return ""
        avg = sum(values) / len(values)
        if avg >= 150:
            return "整体偏亮"
        if avg <= 95:
            return "整体偏暗"
        return "光线适中"
    except Exception:
        return ""


def _analyze_audio_profile(local_path: str) -> dict:
    ffmpeg = _find_ffmpeg()
    profile = {
        "mean_volume_db": None,
        "max_volume_db": None,
        "has_prominent_audio": False,
        "speech_likely": False,
        "music_likely": False,
    }
    if not ffmpeg or not os.path.exists(local_path):
        return profile
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-i",
        local_path,
        "-t",
        "15",
        "-af",
        "volumedetect",
        "-f",
        "null",
        "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        stderr = proc.stderr or ""
        mean_match = re.search(r"mean_volume:\s*([-\d.]+)\s*dB", stderr)
        max_match = re.search(r"max_volume:\s*([-\d.]+)\s*dB", stderr)
        if mean_match:
            profile["mean_volume_db"] = float(mean_match.group(1))
        if max_match:
            profile["max_volume_db"] = float(max_match.group(1))
        mean_db = profile["mean_volume_db"]
        max_db = profile["max_volume_db"]
        if mean_db is not None and mean_db > -45:
            profile["has_prominent_audio"] = True
        if mean_db is not None and max_db is not None:
            if mean_db > -38 and (max_db - mean_db) < 12:
                profile["speech_likely"] = True
            elif mean_db > -42 and (max_db - mean_db) >= 12:
                profile["music_likely"] = True
    except Exception:
        pass
    return profile


def _format_cut_frequency_label(cut_times: list[float], duration_sec: float) -> str:
    if duration_sec <= 0:
        return "未知"
    if not cut_times:
        return "无明显切换"
    avg = duration_sec / (len(cut_times) + 1)
    if avg < 1.5:
        return "快切，约1秒一换"
    if avg < 3:
        return f"整体{avg:.0f}秒一换"
    return f"整体{avg:.0f}秒一换，偏慢"


def _infer_device_light_label(width: int, height: int, brightness: str) -> str:
    parts = []
    if width > 0 and height > 0:
        if height > width * 1.15:
            parts.append("疑似手机竖屏")
        elif width > height * 1.15:
            parts.append("疑似横屏")
        else:
            parts.append("接近1:1画幅")
        short_side = min(width, height)
        if short_side <= 480:
            parts.append("低清")
        elif short_side <= 720:
            parts.append("720p级")
        else:
            parts.append("高清")
    if brightness:
        parts.append(brightness)
    return "；".join(parts) if parts else "未知"


def extract_video_signals(local_path: str) -> dict:
    """本地 ffmpeg 分析：时长、画幅、镜头切换、音频特征。"""
    duration_sec = _get_video_duration_seconds(local_path)
    width, height = _probe_video_dimensions(local_path)
    cut_times = _detect_scene_cut_times(local_path)
    cuts_first_3s = [t for t in cut_times if t <= 3.0]
    brightness = _estimate_brightness_label(local_path)
    audio = _analyze_audio_profile(local_path)
    return {
        "duration_sec": duration_sec,
        "duration_label": _format_duration_label(duration_sec),
        "width": width,
        "height": height,
        "cut_times": cut_times,
        "cut_count": len(cut_times),
        "shots_first_3s": len(cuts_first_3s) + 1 if duration_sec > 0 else 0,
        "cut_frequency_label": _format_cut_frequency_label(cut_times, duration_sec),
        "device_light_label": _infer_device_light_label(width, height, brightness),
        "brightness_label": brightness,
        "audio": audio,
    }


def build_base_shallow_row(
    *,
    video_index: int,
    video_url: str,
    raw_item: Optional[dict] = None,
    local_path: str = "",
    brand_name: str = "",
    product_category: str = "",
) -> dict:
    """仅填 Apify 元数据 + 时长，不含创意字段。"""
    raw_item = raw_item or {}
    row = {col: "" for col in SHALLOW_TABLE_COLUMNS}
    row["视频编号"] = str(video_index)
    row["视频/网址"] = video_url
    row["渠道来源"] = "Meta"
    row["视频时长"] = _get_video_duration_label(local_path)
    row["Impression"] = _extract_impression_label(raw_item)
    row["商品类目"] = product_category.strip() or "未知"
    row["品牌名称"] = _extract_brand_name(raw_item) or brand_name or "未知"
    row["品牌网站"] = _extract_brand_website(raw_item)
    row["CTA类型"] = _extract_cta_text(raw_item) or "未知"
    return row


def build_algo_shallow_fields(signals: dict) -> dict:
    return {
        "视频时长": signals.get("duration_label") or "未知",
        "镜头切换频率": signals.get("cut_frequency_label") or "未知",
        "前三秒镜头数": str(signals.get("shots_first_3s") or "未知"),
        "设备 | 光线": signals.get("device_light_label") or "未知",
    }


def build_heuristic_creative_fields(
    base_row: dict,
    signals: dict,
    raw_item: Optional[dict] = None,
) -> dict:
    """纯本地规则推断创意字段，零 Token（不含个人见解，需 Gemini 看视频）。"""
    raw_item = raw_item or {}
    audio = signals.get("audio") or {}
    cut_count = signals.get("cut_count") or 0
    duration = signals.get("duration_sec") or 0.0
    shots_3s = signals.get("shots_first_3s") or 1
    width = signals.get("width") or 0
    height = signals.get("height") or 0
    vertical = height > width * 1.15 if width and height else False
    avg_cut_sec = duration / (cut_count + 1) if duration > 0 else 0.0
    category = base_row.get("商品类目") or "未知"

    if shots_3s >= 3 or avg_cut_sec < 1.5:
        hook_type = "快切吸睛"
    elif audio.get("speech_likely"):
        hook_type = "口播开场"
    elif vertical:
        hook_type = "竖屏场景展示"
    else:
        hook_type = "产品/场景展示"

    if category and category != "未知":
        scene = f"{category}、竖屏展示" if vertical else f"{category}、产品展示"
    else:
        scene = "室内产品展示" if vertical else "产品展示场景"

    if not audio.get("has_prominent_audio"):
        mood = "无口播/纯BGM或静音"
    elif audio.get("speech_likely"):
        mood = "偏快节奏口播" if avg_cut_sec < 2.5 else "平稳口播"
    else:
        mood = "轻叙述/背景人声"

    if audio.get("music_likely"):
        music = "有背景音乐；切镜与节拍大致同步" if avg_cut_sec < 2.5 else "有背景音乐，切镜偏慢"
    elif audio.get("speech_likely"):
        music = "以人声为主，BGM 弱或无"
    elif audio.get("has_prominent_audio"):
        music = "有环境音/音效，无明显 BGM"
    else:
        music = "音频不明显"

    return {
        "Hook类型": hook_type,
        "场景": scene,
        "口播情绪基调": mood,
        "音乐、音效、卡点": music,
    }


def build_local_shallow_row(
    *,
    video_index: int,
    video_url: str,
    raw_item: Optional[dict] = None,
    local_path: str = "",
    brand_name: str = "",
    product_category: str = "",
    on_status: Optional[Callable[[str, str], None]] = None,
) -> dict:
    """本地 ffmpeg + 规则推断，零 Token 填满浅捞行。"""
    if on_status:
        on_status("本地分析镜头/音频...", "ffmpeg 场景检测")
    base_row = build_base_shallow_row(
        video_index=video_index,
        video_url=video_url,
        raw_item=raw_item,
        local_path=local_path,
        brand_name=brand_name,
        product_category=product_category,
    )
    row = dict(base_row)
    if not local_path or not os.path.exists(local_path):
        row.update(build_heuristic_creative_fields(base_row, {}, raw_item))
        return row
    signals = extract_video_signals(local_path)
    if on_status:
        on_status("规则推断创意字段...", "零 Token，未调用大模型")
    row.update(build_algo_shallow_fields(signals))
    row.update(build_heuristic_creative_fields(base_row, signals, raw_item))
    return row


def build_local_shallow_result_entry(
    src: dict,
    *,
    product_category: str = "",
    error: Optional[str] = None,
    on_status: Optional[Callable[[str, str], None]] = None,
) -> dict:
    """单条视频本地免费填表结果（Gemini 失败/额度用尽时的兜底）。"""
    entry = {
        "index": src["index"],
        "brand_name": src.get("brand_name"),
        "start_date": src.get("start_date"),
        "video_url": src["video_url"],
        "local_path": src.get("local_path"),
        "raw": src.get("raw") or {},
        "product_category": product_category,
    }
    download_err = src.get("download_error")
    if download_err and not error:
        error = f"视频下载失败：{download_err}"
    try:
        row = build_local_shallow_row(
            video_index=src["index"],
            video_url=src["video_url"],
            raw_item=src.get("raw"),
            local_path=src.get("local_path") or "",
            brand_name=src.get("brand_name") or "",
            product_category=product_category,
            on_status=on_status,
        )
        entry["row"] = row
        entry["report"] = shallow_row_to_markdown_table(row)
        entry["error"] = error
    except Exception as exc:
        entry["row"] = None
        entry["report"] = None
        entry["error"] = error or f"本地填表失败：{exc}"
    return entry


def build_local_analysis_results(
    video_sources: list,
    product_category: str = "",
    on_status: Optional[Callable[[str, str], None]] = None,
) -> list:
    results = []
    for idx, src in enumerate(video_sources, start=1):
        if on_status:
            on_status(
                f"[{idx}/{len(video_sources)}] 本地免费填表",
                src.get("source_name") or src.get("brand_name") or "",
            )
        results.append(
            build_local_shallow_result_entry(
                src,
                product_category=product_category,
                on_status=on_status,
            )
        )
    return results


def _escape_md_cell(val) -> str:
    """Markdown 表格单元格转义（列名「设备 | 光线」含 |，必须转义否则拆列错位）。"""
    s = str(val or "")
    s = s.replace("\\", "\\\\")
    return s.replace("|", "\\|")


def _unescape_md_cell(val: str) -> str:
    s = val or ""
    out: list[str] = []
    i = 0
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s):
            nxt = s[i + 1]
            if nxt == "|":
                out.append("|")
                i += 2
                continue
            if nxt == "\\":
                out.append("\\")
                i += 2
                continue
        out.append(s[i])
        i += 1
    return "".join(out)


def shallow_row_to_markdown_table(row: dict) -> str:
    header = " | ".join(_escape_md_cell(c) for c in SHALLOW_TABLE_COLUMNS)
    sep = " | ".join(["---"] * len(SHALLOW_TABLE_COLUMNS))
    cells = " | ".join(_escape_md_cell(row.get(col, "")) for col in SHALLOW_TABLE_COLUMNS)
    return f"| {header} |\n| {sep} |\n| {cells} |"


def build_hook_extraction_prompt(raw_item: Optional[dict] = None) -> str:
    """仅提取视频前 3 秒的 HookVO / Text Hook，输出 JSON。"""
    ad_copy_ref = _extract_ad_copy_text(raw_item or {})
    ad_copy_block = ""
    if ad_copy_ref:
        ad_copy_block = f"""
【广告库配文 · 仅供参考，严禁直接抄写进下方两列】
{ad_copy_ref[:500]}
"""
    return f"""请只分析这条广告视频的 **前 3 秒（0:00~0:03）**，输出**纯 JSON**（不要 Markdown 表格、不要其它说明）。

{ad_copy_block}
## 两列定义（必须严格遵守）

1. **HookVO** = 前三秒里**听到的台词**是什么？
   - 包括：口播、旁白、画外音、人声对白
   - 按原语言**逐字转写**，不要概括、不要翻译（除非视频本身是中文）
   - 没有人声/台词 → 填「无口播」
   - 有人声但听不清 → 填「听不清」

2. **Text Hook** = 前三秒**画面里出现的字幕**是什么？
   - 仅指：烧录在画面上的字幕/caption/底部白字/大号 overlay 字幕
   - **不要**把以下内容当作 Text Hook：帖子标题、广告配文、按钮 CTA、贴纸装饰字、品牌 logo 字
   - 没有字幕 → 填「无字幕」
   - 有字幕但看不清 → 填「看不清」

输出格式（键名不可变，只输出一行 JSON）：
{{"HookVO": "...", "Text Hook": "..."}}
"""


def _finalize_hook_fields(hook: dict) -> dict:
    """Gemini 识别后规范化 J/K 列；空值用明确占位，避免落成「未知」。"""
    hook_vo = str(hook.get("HookVO") or "").strip()
    text_hook = str(hook.get("Text Hook") or "").strip()
    if text_hook in ("无文字", "无"):
        text_hook = ""
    if hook_vo in ("无",):
        hook_vo = ""
    if not hook_vo:
        hook_vo = "无口播"
    if not text_hook:
        text_hook = "无字幕"
    return {"HookVO": hook_vo, "Text Hook": text_hook}


def parse_hook_extraction_response(text: str) -> dict:
    """从 Gemini 返回中解析 HookVO / Text Hook。"""
    raw = (text or "").strip()
    if not raw:
        return {}

    candidates = [raw]
    for pattern in (r"```json\s*(.*?)\s*```", r"```\s*(.*?)\s*```"):
        match = re.search(pattern, raw, re.DOTALL | re.IGNORECASE)
        if match:
            candidates.insert(0, match.group(1).strip())

    for candidate in candidates:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start < 0 or end <= start:
            continue
        try:
            data = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        hook_vo = str(
            data.get("HookVO")
            or data.get("hookvo")
            or data.get("hook_vo")
            or data.get("台词")
            or data.get("hook_vo_lines")
            or ""
        ).strip()
        text_hook = str(
            data.get("Text Hook")
            or data.get("text_hook")
            or data.get("TextHook")
            or data.get("textHook")
            or data.get("字幕")
            or data.get("text_hook_caption")
            or ""
        ).strip()
        if hook_vo or text_hook:
            return _finalize_hook_fields({"HookVO": hook_vo, "Text Hook": text_hook})
    return {}


def _validate_hook_fields(hook: dict) -> bool:
    if not hook:
        return False
    return any(not _cell_is_empty(hook.get(col)) for col in SHALLOW_VIDEO_HOOK_COLUMNS)


def _apply_row_defaults(row: dict) -> dict:
    """空列填「未知」，个人见解等列保持空白。"""
    for col in SHALLOW_TABLE_COLUMNS:
        if col in SHALLOW_ALWAYS_BLANK_COLUMNS:
            row[col] = ""
        elif _cell_is_empty(row.get(col)):
            row[col] = "未知"
    return row


def upload_file_with_retry(
    local_video_path: str,
    on_status: Optional[Callable[[str, str], None]] = None,
):
    """上传文件到 Gemini：多代理/直连切换 + SSL 重试 + 分块上传。"""
    api_key = _GEMINI_RUNTIME.get("api_key", "")
    primary_proxy = _GEMINI_RUNTIME.get("proxy", "")
    last_err: Optional[Exception] = None
    session = _gemini_requests_session()

    try:
        for proxy_idx, (try_proxy, proxy_label) in enumerate(
            _gemini_upload_proxy_candidates(primary_proxy)
        ):
            route = try_proxy or "系统直连"
            for attempt in range(1, GEMINI_UPLOAD_PROXY_ATTEMPTS + 1):
                try:
                    if proxy_idx > 0 or attempt > 1:
                        if on_status:
                            on_status(
                                f"上传重试 ({attempt}/{GEMINI_UPLOAD_PROXY_ATTEMPTS})...",
                                f"线路：{proxy_label} · {route}",
                            )
                        time.sleep(min(3 * attempt, 10))
                    elif on_status:
                        detail = (
                            f"压缩后 {_format_mb(local_video_path)}，"
                            f"超时上限 {GEMINI_API_TIMEOUT_SEC}s · 分块 {GEMINI_UPLOAD_CHUNK_BYTES // (1024 * 1024)}MB"
                        )
                        if try_proxy:
                            detail += f" · 代理 {try_proxy}"
                        else:
                            detail += " · 系统直连"
                        on_status("正在上传视频到 Gemini 云端...", detail)

                    if try_proxy:
                        apply_gemini_proxy(try_proxy)
                        _GEMINI_RUNTIME["proxy"] = try_proxy
                    else:
                        _force_direct_gemini_network()

                    return upload_file_via_requests(
                        local_video_path,
                        api_key=api_key,
                        proxy_url=try_proxy,
                        session=session,
                    )
                except Exception as exc:
                    if not _is_retryable_upload_error(exc):
                        raise
                    last_err = exc
                    if on_status and attempt == GEMINI_UPLOAD_PROXY_ATTEMPTS:
                        on_status(
                            f"当前线路失败，切换下一代理...",
                            _sanitize_api_error(str(exc))[:120],
                        )
    finally:
        session.close()

    if last_err is not None:
        raise last_err
    raise RuntimeError("Gemini 上传失败：未知错误")


def upload_and_wait_active(
    local_video_path: str,
    on_status: Optional[Callable[[str, str], None]] = None,
):
    """上传视频到 Gemini 云端，并轮询状态直到 ACTIVE。"""
    video_file = upload_file_with_retry(local_video_path, on_status=on_status)
    poll_count = 0

    while True:
        file_state = video_file.state.name
        if file_state == "ACTIVE":
            if on_status:
                on_status("Gemini 视频处理完成", "即将开始浅捞分析")
            break
        elif file_state == "PROCESSING":
            poll_count += 1
            waited = poll_count * POLL_INTERVAL_SEC
            if on_status:
                on_status(
                    f"Gemini 正在转码/处理视频...（已等待 {waited} 秒）",
                    "这一步通常最耗时，程序仍在运行，请勿关闭页面",
                )
            time.sleep(POLL_INTERVAL_SEC)
            video_file = get_file_via_requests(
                video_file.name,
                api_key=_GEMINI_RUNTIME.get("api_key", ""),
                proxy_url=_GEMINI_RUNTIME.get("proxy", ""),
            )
        elif file_state == "FAILED":
            raise RuntimeError(f"Gemini 视频处理失败：{video_file.name}")
        else:
            poll_count += 1
            if on_status:
                on_status(
                    f"等待 Gemini 就绪...（状态：{file_state}）",
                    "程序仍在运行，请稍候",
                )
            time.sleep(POLL_INTERVAL_SEC)
            video_file = get_file_via_requests(
                video_file.name,
                api_key=_GEMINI_RUNTIME.get("api_key", ""),
                proxy_url=_GEMINI_RUNTIME.get("proxy", ""),
            )

    return video_file


def _is_gemini_model_not_found(exc: Exception) -> bool:
    msg = str(exc).lower()
    return (
        "404" in str(exc)
        or "not found" in msg
        or "is not supported for generatecontent" in msg
    )


def _generate_hook_content_with_models(
    video_file,
    prompt: str,
    *,
    model_name: Optional[str] = None,
    on_status: Optional[Callable[[str, str], None]] = None,
):
    """按模型链调用 generateContent；404 时自动换可用模型。"""
    last_err: Optional[Exception] = None
    for idx, candidate in enumerate(_gemini_model_candidates(model_name or "")):
        gen_config = {
            "temperature": 0,
            "max_output_tokens": 768,
            "response_mime_type": "application/json",
        }
        if _gemini_supports_thinking_budget(candidate):
            gen_config["thinking_config"] = {"thinking_budget": 0}
        model = genai.GenerativeModel(candidate)
        try:
            if idx > 0 and on_status:
                on_status(
                    "模型不可用，正在切换...",
                    f"{candidate}（回退 {idx + 1}/{len(_gemini_model_candidates(model_name or ''))}）",
                )
            try:
                return candidate, model.generate_content(
                    [video_file, prompt],
                    generation_config=gen_config,
                    request_options={"timeout": GEMINI_API_TIMEOUT_SEC},
                )
            except Exception as exc:
                if "thinking" in str(exc).lower() and "thinking_config" in gen_config:
                    gen_config.pop("thinking_config")
                    return candidate, model.generate_content(
                        [video_file, prompt],
                        generation_config=gen_config,
                        request_options={"timeout": GEMINI_API_TIMEOUT_SEC},
                    )
                if "response_mime_type" in gen_config and (
                    "response_mime_type" in str(exc).lower()
                    or "mime" in str(exc).lower()
                    or "json" in str(exc).lower()
                ):
                    gen_config.pop("response_mime_type")
                    return candidate, model.generate_content(
                        [video_file, prompt],
                        generation_config=gen_config,
                        request_options={"timeout": GEMINI_API_TIMEOUT_SEC},
                    )
                raise
        except Exception as exc:
            last_err = exc
            if _is_gemini_model_not_found(exc):
                continue
            raise
    if last_err:
        raise last_err
    raise RuntimeError("没有可用的 Gemini 视频模型")


def extract_video_hooks_with_gemini(
    local_video_path: str,
    *,
    video_index: int,
    video_url: str,
    raw_item: Optional[dict] = None,
    model_name: Optional[str] = None,
    on_status: Optional[Callable[[str, str], None]] = None,
) -> dict:
    """上传视频前 3 秒 -> 识别 HookVO（台词）/ Text Hook（画面字幕）。"""
    prompt = build_hook_extraction_prompt(raw_item)
    video_file = None
    compressed_path = None
    is_temp_compressed = False
    try:
        upload_path, is_temp_compressed = compress_video_for_gemini(
            local_video_path,
            on_status=on_status,
            max_seconds=GEMINI_HOOK_CLIP_SECONDS,
        )
        if is_temp_compressed:
            compressed_path = upload_path

        video_file = upload_and_wait_active(upload_path, on_status=on_status)
        if on_status:
            on_status(
                f"正在识别第 {video_index} 条 J/K 列（HookVO=台词 · Text Hook=字幕）...",
                f"模型：{resolve_gemini_model(model_name or '')} · 前 {GEMINI_HOOK_CLIP_SECONDS}s",
            )

        used_model, response = _generate_hook_content_with_models(
            video_file,
            prompt,
            model_name=model_name,
            on_status=on_status,
        )
        _GEMINI_RUNTIME["model"] = used_model
        hooks = parse_hook_extraction_response(response.text or "")
        if not hooks and (response.text or "").strip():
            hooks = _finalize_hook_fields({})
        return _sanitize_gemini_hook_fields(hooks, raw_item)
    except Exception as exc:
        raise RuntimeError(_gemini_error_hint(exc)) from exc
    finally:
        if video_file is not None:
            try:
                genai.delete_file(video_file.name)
            except Exception:
                pass
        if is_temp_compressed and compressed_path and os.path.exists(compressed_path):
            try:
                os.remove(compressed_path)
            except Exception:
                pass


def analyze_video_with_gemini(
    local_video_path: str,
    *,
    video_index: int,
    video_url: str,
    raw_item: Optional[dict] = None,
    model_name: Optional[str] = None,
    on_status: Optional[Callable[[str, str], None]] = None,
) -> dict:
    """兼容旧调用名；返回 HookVO / Text Hook 字典。"""
    return extract_video_hooks_with_gemini(
        local_video_path,
        video_index=video_index,
        video_url=video_url,
        raw_item=raw_item,
        model_name=model_name,
        on_status=on_status,
    )


class FetchProgress:
    """步骤①：抓取 + 下载进度。"""

    STEPS = ["Apify 抓取广告库", "筛选近7天曝光最高视频", "下载视频素材", "打包下载"]

    def __init__(self, video_count: int):
        self.video_count = max(video_count, 1)
        self._bar = st.progress(0.0)
        self._status = st.empty()
        self._detail = st.empty()
        self._checklist = st.empty()
        self._active = 0
        self._completed = set()
        self._render_checklist()

    def _render_checklist(self):
        lines = []
        for i, label in enumerate(self.STEPS):
            if i in self._completed:
                icon = "✅"
            elif i == self._active:
                icon = "🔄"
            else:
                icon = "⏳"
            lines.append(f"{icon} {i + 1}. {label}")
        self._checklist.markdown("  \n".join(lines))

    def set_step(self, step_index: int, detail: str = ""):
        self._active = step_index
        self._completed = set(range(step_index))
        self._render_checklist()
        if detail:
            self._detail.caption(detail)

    def complete_step(self, step_index: int):
        self._completed.add(step_index)
        self._render_checklist()

    def update(self, ratio: float, step: str, detail: str = ""):
        ratio = min(max(ratio, 0.0), 1.0)
        self._bar.progress(ratio, text=f"总进度 {int(ratio * 100)}%")
        self._status.info(f"**当前步骤：** {step}")
        if detail:
            self._detail.caption(detail)

    def finish(self):
        self._completed = set(range(len(self.STEPS)))
        self._active = len(self.STEPS)
        self._render_checklist()
        self.update(1.0, "下载完成！", "请下载压缩包，或在步骤②上传视频进行浅捞")
        self._status.success("**当前步骤：** 下载完成 ✅")


class LocalAnalyzeProgress:
    """步骤②：本地免费填表（零 Token）。"""

    STEPS = ["接收视频", "本地镜头/音频分析", "规则推断填表", "打包结果"]

    def __init__(self, video_count: int):
        self.video_count = max(video_count, 1)
        self._bar = st.progress(0.0)
        self._status = st.empty()
        self._detail = st.empty()
        self._checklist = st.empty()
        self._active = 0
        self._completed = set()
        self._render_checklist()

    def _render_checklist(self):
        lines = []
        for i, label in enumerate(self.STEPS):
            if i in self._completed:
                icon = "✅"
            elif i == self._active:
                icon = "🔄"
            else:
                icon = "⏳"
            lines.append(f"{icon} {i + 1}. {label}")
        self._checklist.markdown("  \n".join(lines))

    def set_step(self, step_index: int, detail: str = ""):
        self._active = step_index
        self._completed = set(range(step_index))
        self._render_checklist()
        if detail:
            self._detail.caption(detail)

    def update(self, ratio: float, step: str, detail: str = ""):
        ratio = min(max(ratio, 0.0), 1.0)
        self._bar.progress(ratio, text=f"总进度 {int(ratio * 100)}%")
        self._status.info(f"**当前步骤：** {step}")
        if detail:
            self._detail.caption(detail)

    def video_ratio(self, video_index: int, sub_progress: float) -> float:
        base = 0.12
        span = 0.72
        per = span / self.video_count
        return base + (video_index - 1) * per + per * sub_progress

    def finish(self):
        self._completed = set(range(len(self.STEPS)))
        self._active = len(self.STEPS)
        self._render_checklist()
        self.update(1.0, "填表完成！", "零 Token · 可下载压缩包")
        self._status.success("**当前步骤：** 填表完成 ✅")


class AnalyzeProgress:
    """步骤②：Gemini 浅捞进度。"""

    STEPS = ["接收上传视频", "Gemini 上传与处理", "识别 J/K 列", "打包结果"]

    def __init__(self, video_count: int):
        self.video_count = max(video_count, 1)
        self._bar = st.progress(0.0)
        self._status = st.empty()
        self._detail = st.empty()
        self._checklist = st.empty()
        self._active = 0
        self._completed = set()
        self._render_checklist()

    def _render_checklist(self):
        lines = []
        for i, label in enumerate(self.STEPS):
            if i in self._completed:
                icon = "✅"
            elif i == self._active:
                icon = "🔄"
            else:
                icon = "⏳"
            lines.append(f"{icon} {i + 1}. {label}")
        self._checklist.markdown("  \n".join(lines))

    def set_step(self, step_index: int, detail: str = ""):
        self._active = step_index
        self._completed = set(range(step_index))
        self._render_checklist()
        if detail:
            self._detail.caption(detail)

    def complete_step(self, step_index: int):
        self._completed.add(step_index)
        self._render_checklist()

    def update(self, ratio: float, step: str, detail: str = ""):
        ratio = min(max(ratio, 0.0), 1.0)
        self._bar.progress(ratio, text=f"总进度 {int(ratio * 100)}%")
        self._status.info(f"**当前步骤：** {step}")
        if detail:
            self._detail.caption(detail)

    def video_ratio(self, video_index: int, sub_progress: float) -> float:
        base = 0.15
        span = 0.75 / self.video_count
        return base + (video_index - 1) * span + sub_progress * span

    def finish(self):
        self._completed = set(range(len(self.STEPS)))
        self._active = len(self.STEPS)
        self._render_checklist()
        self.update(1.0, "浅捞完成！", "可下载压缩包或发送邮件")
        self._status.success("**当前步骤：** 浅捞完成 ✅")


class RunProgress:
    """兼容旧名 — 等同 AnalyzeProgress。"""
    PIPELINE_STEPS = AnalyzeProgress.STEPS

    def __init__(self, video_count: int):
        self._inner = AnalyzeProgress(video_count)

    def set_pipeline_step(self, step_index: int, detail: str = ""):
        self._inner.set_step(step_index, detail)

    def complete_pipeline_step(self, step_index: int):
        self._inner.complete_step(step_index)

    def update(self, ratio: float, step: str, detail: str = ""):
        self._inner.update(ratio, step, detail)

    def video_ratio(self, video_index: int, sub_progress: float) -> float:
        return self._inner.video_ratio(video_index, sub_progress)

    def finish(self):
        self._inner.finish()


# ------------------------------------------------------------------
# 模块五：导出压缩包 & 发邮件
# ------------------------------------------------------------------
def _safe_filename(text: str, fallback: str = "file") -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\s]+', "_", (text or "").strip())
    return cleaned[:60] or fallback


def _cell_is_empty(val) -> bool:
    s = str(val or "").strip()
    return not s or s.lower() in ("未知", "none", "nan", "n/a", "-")


def _canonical_column_name(name: str) -> str:
    n = (name or "").strip()
    if n in SHALLOW_TABLE_COLUMNS:
        return n
    compact = re.sub(r"\s+", "", n)
    for col in SHALLOW_TABLE_COLUMNS:
        if re.sub(r"\s+", "", col) == compact:
            return col
    return n


def _extract_markdown_table_text(report: str) -> str:
    text = report or ""
    fenced = re.search(r"```(?:markdown|md)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        return fenced.group(1).strip()
    return text.strip()


def _split_table_cells(line: str) -> list[str]:
    """按 | 拆 Markdown 表格行，忽略 \\| 转义。"""
    text = line.strip().strip("|")
    cells: list[str] = []
    buf: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text) and text[i + 1] == "|":
            buf.append("|")
            i += 2
            continue
        if text[i] == "|":
            cells.append("".join(buf).strip())
            buf = []
            i += 1
            continue
        buf.append(text[i])
        i += 1
    cells.append("".join(buf).strip())
    return cells


def _normalize_table_cells(cells: list[str]) -> list[str]:
    """兼容未转义表头：「设备 | 光线」被拆成两列时合并回 19 列。"""
    expected = len(SHALLOW_TABLE_COLUMNS)
    if len(cells) == expected:
        return [_unescape_md_cell(c) for c in cells]
    device_idx = SHALLOW_TABLE_COLUMNS.index("设备 | 光线")
    if len(cells) == expected + 1 and device_idx + 1 < len(cells):
        merged = (
            cells[:device_idx]
            + [f"{cells[device_idx]} | {cells[device_idx + 1]}"]
            + cells[device_idx + 2:]
        )
        if len(merged) == expected:
            return [_unescape_md_cell(c) for c in merged]
    return []


def _is_internal_shallow_report(report: str) -> bool:
    """是否为工具自己生成的 Markdown 表格（避免重复解析）。"""
    for line in _extract_markdown_table_text(report).splitlines():
        line = line.strip()
        if line.startswith("|") and "视频编号" in line and "个人见解" in line:
            return True
    return False


def _is_table_separator_row(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-+:?", c.replace(" ", "")) for c in cells)


def _is_table_header_row(cells: list[str]) -> bool:
    if not cells or cells[0] != "视频编号":
        return False
    return _canonical_column_name(cells[0]) == "视频编号"


def _validate_parsed_gemini_row(parsed: dict, expected_index: Optional[int] = None) -> bool:
    """兼容旧 Markdown 表格；新流程请用 _validate_hook_fields。"""
    return _validate_hook_fields(parsed)


def _ad_copy_overlap(val: str, ad_copy: str) -> bool:
    val_norm = _normalize_compare_text(val)
    if not val_norm:
        return False
    ad_norm = _normalize_compare_text(ad_copy)
    if ad_norm and (val_norm == ad_norm or (len(ad_norm) > 24 and ad_norm in val_norm)):
        return True
    for chunk in re.split(r"[。！？.!?\n]+", ad_copy):
        chunk = chunk.strip()
        if len(chunk) < 12:
            continue
        chunk_norm = _normalize_compare_text(chunk)
        if chunk_norm and (val_norm == chunk_norm or chunk_norm in val_norm):
            return True
    return False


def _sanitize_gemini_hook_fields(hook: dict, raw_item: Optional[dict] = None) -> dict:
    """剔除 Gemini 照搬广告库配文的 Text Hook（HookVO 为听到的台词，不做此过滤）。"""
    out = {col: str(hook.get(col) or "").strip() for col in SHALLOW_VIDEO_HOOK_COLUMNS}
    ad_copy = _extract_ad_copy_text(raw_item or {})
    if ad_copy:
        val = out.get("Text Hook") or ""
        # 仅当整段配文被照搬时清空，避免误杀短字幕
        if val and len(val) >= 24 and _ad_copy_overlap(val, ad_copy):
            out["Text Hook"] = ""
    return _finalize_hook_fields(out)


def merge_shallow_rows(*layers: dict) -> dict:
    """按层合并表格行，后层非空值覆盖前层。"""
    merged = {col: "" for col in SHALLOW_TABLE_COLUMNS}
    for layer in layers:
        if not layer:
            continue
        for col in SHALLOW_TABLE_COLUMNS:
            val = str(layer.get(col, "") or "").strip()
            if not _cell_is_empty(val):
                merged[col] = val
    return merged


def _normalize_compare_text(text: str) -> str:
    return re.sub(r"[\s/W/|/，。！？、；：""''（）【】]+", "", (text or "").lower())


def merge_shallow_row_layers(
    *,
    base: dict,
    local: dict,
    hook: dict,
    expected_index: Optional[int] = None,
    raw_item: Optional[dict] = None,
) -> dict:
    """元数据 + 本地列 + Gemini J/K 列（严格校验）。"""
    row = merge_shallow_rows(base, local)
    for col in SHALLOW_VIDEO_HOOK_COLUMNS:
        row[col] = ""

    if hook and _validate_hook_fields(hook):
        hook = _sanitize_gemini_hook_fields(hook, raw_item)
        for col in SHALLOW_GEMINI_HOOK_COLUMNS:
            val = str(hook.get(col, "") or "").strip()
            if not _cell_is_empty(val):
                row[col] = val

    row["个人见解"] = ""
    return row


def parse_markdown_table_row(report: str) -> dict:
    """从 Markdown 表格解析数据行；仅接受恰好 19 列，拒绝错位表头映射。"""
    text = _extract_markdown_table_text(report)
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = _normalize_table_cells(_split_table_cells(line))
        if not cells or len(cells) != len(SHALLOW_TABLE_COLUMNS):
            continue
        if _is_table_separator_row(cells) or _is_table_header_row(cells):
            continue
        return dict(zip(SHALLOW_TABLE_COLUMNS, cells))
    return {}


def _pick_local_layer(row: dict, *, include_heuristic: bool) -> dict:
    """从本地填表结果中抽取可安全合并的列（永不包含个人见解）。"""
    cols = list(SHALLOW_LOCAL_TECH_COLUMNS)
    if include_heuristic:
        cols.extend(SHALLOW_LOCAL_HEURISTIC_COLUMNS)
    picked = {}
    for col in cols:
        val = row.get(col)
        if not _cell_is_empty(val):
            picked[col] = val
    return picked


def _build_local_layer_for_result(result: dict, idx: int, *, include_heuristic: bool = False) -> dict:
    local_path = result.get("local_path", "")
    if not local_path or not os.path.exists(local_path):
        return {}
    try:
        full = build_local_shallow_row(
            video_index=idx,
            video_url=result.get("video_url", ""),
            raw_item=result.get("raw") or {},
            local_path=local_path,
            brand_name=result.get("brand_name", ""),
            product_category=result.get("product_category", ""),
        )
        return _pick_local_layer(full, include_heuristic=include_heuristic)
    except Exception:
        return {}


def result_to_shallow_row(result: dict) -> dict:
    """合并元数据 + 本地分析 + Gemini J/K 列，确保 Excel 各列对齐。"""
    idx = int(result.get("index") or 1)
    base = build_base_shallow_row(
        video_index=idx,
        video_url=result.get("video_url", ""),
        raw_item=result.get("raw") or {},
        local_path=result.get("local_path", ""),
        brand_name=result.get("brand_name", ""),
        product_category=result.get("product_category", ""),
    )

    hook_layer = dict(result.get("hook_fields") or {})
    gemini_source = result.get("raw_report") or result.get("report") or ""
    if not hook_layer and gemini_source and not _is_internal_shallow_report(str(gemini_source)):
        hook_layer = parse_hook_extraction_response(str(gemini_source))
        if not hook_layer:
            legacy = parse_markdown_table_row(str(gemini_source))
            hook_layer = {
                col: legacy.get(col, "")
                for col in SHALLOW_VIDEO_HOOK_COLUMNS
                if legacy.get(col)
            }

    local_layer = _build_local_layer_for_result(result, idx, include_heuristic=True)

    row = merge_shallow_row_layers(
        base=base,
        local=local_layer,
        hook=hook_layer,
        expected_index=idx,
        raw_item=result.get("raw") or {},
    )

    row["视频编号"] = str(idx)
    row["视频/网址"] = row["视频/网址"] or result.get("video_url", "")
    if result.get("brand_name"):
        row["品牌名称"] = row["品牌名称"] or result["brand_name"]

    return _apply_row_defaults(row)


def _results_used_gemini_video(results: list) -> bool:
    """结果是否包含 Gemini 识别的 J/K 列。"""
    for result in results:
        if result.get("hook_fields"):
            return True
        raw_report = str(result.get("raw_report") or "")
        if raw_report and not _is_internal_shallow_report(raw_report):
            if parse_hook_extraction_response(raw_report):
                return True
    return False


def ensure_shallow_table_results(
    results: list,
    expected_count: Optional[int] = None,
) -> list:
    """保证 Excel 按视频编号 1..N 连续输出，失败条目保留占位行（避免缺第 3 行）。"""
    if not results and not expected_count:
        return []
    max_idx = max((int(r.get("index") or 0) for r in results), default=0)
    expected = expected_count or max(max_idx, len(results), TOP_N)
    by_idx: dict[int, dict] = {}
    for item in results:
        idx = int(item.get("index") or 0)
        if idx > 0:
            by_idx[idx] = item

    complete: list[dict] = []
    for i in range(1, expected + 1):
        if i in by_idx:
            complete.append(by_idx[i])
            continue
        complete.append(
            {
                "index": i,
                "brand_name": "",
                "start_date": "",
                "video_url": "",
                "local_path": "",
                "raw": {},
                "product_category": "",
                "report": None,
                "row": None,
                "error": "该条视频未处理（下载或分析失败，已保留空行占位）",
            }
        )
    return complete


def finalize_shallow_results(
    results: list,
    expected_count: Optional[int] = None,
) -> list:
    """导出前统一合并各行并回写 report/row，保证 Excel 与 UI 一致。"""
    finalized = []
    for item in ensure_shallow_table_results(results, expected_count):
        entry = dict(item)
        if entry.get("report") and not entry.get("raw_report") and not _is_internal_shallow_report(entry["report"]):
            entry["raw_report"] = entry["report"]
        row = result_to_shallow_row(entry)
        entry["row"] = row
        entry["report"] = shallow_row_to_markdown_table(row)
        finalized.append(entry)
    return finalized


def build_shallow_xlsx_bytes(results: list, expected_count: Optional[int] = None) -> bytes:
    ordered = ensure_shallow_table_results(results, expected_count)
    rows = []
    for r in sorted(ordered, key=lambda x: int(x.get("index") or 0)):
        stored = r.get("row")
        if isinstance(stored, dict) and str(stored.get("视频编号") or "").strip():
            row = {col: stored.get(col, "") for col in SHALLOW_TABLE_COLUMNS}
            row = _apply_row_defaults(row)
        else:
            row = result_to_shallow_row(r)
        rows.append(row)
    df = pd.DataFrame(rows, columns=SHALLOW_TABLE_COLUMNS)
    buf = io.BytesIO()
    df.to_excel(buf, index=False, sheet_name="浅捞")
    return buf.getvalue()


def _ad_metadata_records(ads: list) -> list:
    records = []
    for ad in ads:
        records.append(
            {
                "index": ad.get("index"),
                "brand_name": ad.get("brand_name"),
                "start_date": ad.get("start_date"),
                "video_url": ad.get("video_url"),
            }
        )
    return records


def collect_video_sources(
    *,
    use_server_videos: bool,
    batch: Optional[dict],
    uploaded_files,
) -> tuple[list, list]:
    """收集步骤②待处理的视频来源，返回 (video_sources, temp_paths)。"""
    video_sources = []
    temp_paths = []

    if use_server_videos:
        valid_ads = [
            ad for ad in (batch or {}).get("ads", [])
            if ad.get("local_path") and os.path.exists(ad["local_path"])
        ]
        if not valid_ads:
            raise ValueError("步骤①的视频文件已不存在，请重新下载或改用手动上传。")
        for ad in valid_ads[:TOP_N]:
            video_sources.append(
                {
                    "index": ad["index"],
                    "local_path": ad["local_path"],
                    "brand_name": ad.get("brand_name", ""),
                    "start_date": ad.get("start_date", ""),
                    "video_url": ad.get("video_url", ""),
                    "raw": ad.get("raw") or {},
                    "source_name": os.path.basename(ad["local_path"]),
                }
            )
    else:
        if not uploaded_files:
            raise ValueError("请先上传至少 1 个 mp4 视频，或勾选「直接使用步骤①已下载的视频」。")
        meta_list = match_metadata_for_uploads(len(uploaded_files), batch)
        for i, uf in enumerate(uploaded_files):
            local_path = save_uploaded_video(uf, i + 1)
            temp_paths.append(local_path)
            meta = meta_list[i]
            video_sources.append(
                {
                    "index": i + 1,
                    "local_path": local_path,
                    "brand_name": meta.get("brand_name") or uf.name,
                    "start_date": meta.get("start_date", ""),
                    "video_url": meta.get("video_url", uf.name),
                    "raw": meta.get("raw") or {},
                    "source_name": uf.name,
                }
            )

    return video_sources, temp_paths


def save_export_bundle(
    results: list,
    zip_bytes: bytes,
    zip_filename: str,
    keyword: str,
    country: str,
) -> None:
    """保存导出结果到 session，供下载/发邮件在 Streamlit rerun 后仍可用。"""
    st.session_state[LAST_EXPORT_KEY] = {
        "results": results,
        "zip_bytes": zip_bytes,
        "zip_filename": zip_filename,
        "keyword": keyword,
        "country": country,
    }


def get_export_bundle() -> Optional[dict]:
    return st.session_state.get(LAST_EXPORT_KEY)


def clear_export_bundle() -> None:
    st.session_state.pop(LAST_EXPORT_KEY, None)


def render_export_download_section(
    results: list,
    zip_bytes: bytes,
    zip_filename: str,
    label_kw: str,
    label_country: str,
    *,
    default_recipients: str,
    smtp_host: str,
    smtp_port: str,
    smtp_user: str,
    smtp_password: str,
    smtp_from_name: str,
    key_prefix: str = "analyze",
):
    bundle = get_export_bundle() or {}
    zip_bytes = zip_bytes or bundle.get("zip_bytes") or b""
    zip_filename = zip_filename or bundle.get("zip_filename") or "export.zip"
    if not results and bundle.get("results"):
        results = bundle["results"]
    label_kw = label_kw or bundle.get("keyword") or "export"
    label_country = label_country or bundle.get("country") or "ALL"

    st.subheader("📦 下载 & 发邮件")
    dl_col, mail_col = st.columns(2)
    with dl_col:
        st.download_button(
            label="📥 下载压缩包（浅捞表格 + 视频）",
            data=zip_bytes,
            file_name=zip_filename,
            mime="application/zip",
            use_container_width=True,
            key=f"dl_{key_prefix}",
        )
        st.caption("包含：`浅捞表格.xlsx` · `videos/*.mp4` · `浅捞汇总.md`")

    with mail_col:
        mail_to = st.text_input(
            "收件人邮箱（多个用逗号分隔）",
            value=default_recipients,
            key=f"mail_recipients_{key_prefix}",
        )
        mail_light_only = st.checkbox(
            "邮件不含视频（推荐，避免 Gmail 25MB 超限）",
            value=True,
            key=f"mail_light_{key_prefix}",
        )
        if zip_bytes:
            st.caption(f"完整压缩包大小：{_format_bytes(len(zip_bytes))}")
        if st.button("📧 发送压缩包到邮箱", use_container_width=True, key=f"send_mail_{key_prefix}"):
            if not zip_bytes:
                st.error("❌ 压缩包不存在或已过期，请重新生成浅捞结果后再发送。")
            else:
                try:
                    attach_bytes, attach_name, size_note = prepare_email_attachment(
                        zip_bytes,
                        zip_filename,
                        results=results,
                        keyword=label_kw,
                        country=label_country,
                        prefer_light=mail_light_only,
                    )
                except ValueError as exc:
                    st.error(f"❌ {exc}")
                else:
                    smtp_override = {
                        "host": smtp_host.strip(),
                        "port": smtp_port.strip(),
                        "user": smtp_user.strip(),
                        "password": (
                            smtp_password.strip()
                            or st.session_state.get("smtp_password_field", "").strip()
                            or _secret("email", "smtp_password")
                        ),
                        "from_addr": smtp_user.strip() or _secret("email", "from_addr"),
                        "from_name": smtp_from_name.strip(),
                    }
                    ok, msg = send_export_email(
                        _parse_email_list(mail_to),
                        attach_bytes,
                        attach_name,
                        label_kw,
                        label_country,
                        smtp_override=smtp_override,
                        results=results,
                    )
                    if ok:
                        st.success(f"{msg}\n{size_note}".strip())
                    else:
                        st.error(msg)

    st.subheader("📊 浅捞结果")
    if results and not _results_used_gemini_video(results):
        st.caption(
            "💡 本地模式下 J/K 列为「未知」：HookVO=前三秒台词、Text Hook=前三秒画面字幕，需 Gemini 视频模式识别。"
        )
    tabs = st.tabs([f"#{r['index']} {r.get('brand_name', '')}" for r in results])
    for tab, r in zip(tabs, results):
        with tab:
            if r.get("local_path") and os.path.exists(r["local_path"]):
                st.video(r["local_path"])
            if r.get("error"):
                st.error(f"❌ {r['error']}")
            else:
                with st.expander("📋 查看浅捞表格", expanded=True):
                    st.markdown(r["report"])


def _shallow_zip_filename(suffix: str = "") -> str:
    """压缩包命名：MMDD浅捞.zip，例如 0617浅捞.zip"""
    date_tag = datetime.now().strftime("%m%d")
    return f"{date_tag}浅捞{suffix}.zip"


def build_videos_only_zip(ads: list, search_keyword: str, country: str) -> tuple:
    """步骤①：仅打包视频 + 广告元数据，不含浅捞表格。"""
    zip_name = _shallow_zip_filename("_视频")
    buf = io.BytesIO()
    meta = {
        "search_keyword": search_keyword,
        "country": country,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "videos": _ad_metadata_records(ads),
    }

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("广告元数据.json", json.dumps(meta, ensure_ascii=False, indent=2))
        for ad in ads:
            idx = ad.get("index", 0)
            brand = _safe_filename(ad.get("brand_name") or f"video_{idx}", f"video_{idx}")
            local_path = ad.get("local_path")
            if local_path and os.path.exists(local_path):
                zf.write(local_path, arcname=f"videos/{idx:02d}_{brand}.mp4")

    return buf.getvalue(), zip_name


def cleanup_fetched_batch(batch: Optional[dict]) -> None:
    if not batch:
        return
    for ad in batch.get("ads", []):
        path = ad.get("local_path")
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass


def save_uploaded_video(uploaded_file, index: int) -> str:
    local_path = os.path.join(TEMP_DIR, f"upload_{index}_{uuid.uuid4().hex[:6]}.mp4")
    with open(local_path, "wb") as f:
        f.write(uploaded_file.getbuffer())
    return local_path


def match_metadata_for_uploads(upload_count: int, fetched_batch: Optional[dict]) -> list:
    """按顺序将步骤①元数据匹配到上传的视频。"""
    matched = []
    cached = (fetched_batch or {}).get("ads") or []
    for i in range(upload_count):
        meta = cached[i] if i < len(cached) else {}
        matched.append(
            {
                "index": i + 1,
                "brand_name": meta.get("brand_name", ""),
                "start_date": meta.get("start_date", ""),
                "video_url": meta.get("video_url", ""),
                "raw": meta.get("raw") or {},
            }
        )
    return matched


def _format_bytes(num_bytes: int) -> str:
    if num_bytes >= 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.1f} MB"
    if num_bytes >= 1024:
        return f"{num_bytes / 1024:.0f} KB"
    return f"{num_bytes} B"


def _build_export_summary_lines(results: list, search_keyword: str, country: str) -> list[str]:
    summary_lines = [
        "# FB 广告库浅捞结果",
        f"- 搜索关键词：{search_keyword}",
        f"- 国家/地区：{country}",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]
    for r in results:
        idx = r.get("index", 0)
        summary_lines.append(f"## 视频 {idx} · {r.get('brand_name', '')}")
        summary_lines.append(f"- 开始投放：{r.get('start_date', '')}")
        summary_lines.append(f"- 链接：{r.get('video_url', '')}")
        if r.get("error"):
            summary_lines.append(f"- 错误：{r['error']}")
        elif r.get("report"):
            summary_lines.append("")
            summary_lines.append(r["report"])
        summary_lines.append("")
    return summary_lines


def build_export_zip(
    results: list,
    search_keyword: str,
    country: str,
    *,
    include_videos: bool = True,
) -> tuple:
    """生成 zip。include_videos=False 时仅含 Excel + 汇总（适合邮件附件）。"""
    suffix = "" if include_videos else "_仅表格"
    zip_name = _shallow_zip_filename(suffix)
    buf = io.BytesIO()
    summary_lines = _build_export_summary_lines(results, search_keyword, country)

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        expected_rows = max(
            (int(r.get("index") or 0) for r in results),
            default=TOP_N,
        )
        zf.writestr(
            "浅捞表格.xlsx",
            build_shallow_xlsx_bytes(results, expected_count=expected_rows),
        )
        if include_videos:
            for r in results:
                idx = r.get("index", 0)
                brand = _safe_filename(r.get("brand_name") or f"video_{idx}", f"video_{idx}")
                local_path = r.get("local_path")
                if local_path and os.path.exists(local_path):
                    zf.write(local_path, arcname=f"videos/{idx:02d}_{brand}.mp4")
        zf.writestr("浅捞汇总.md", "\n".join(summary_lines))

    return buf.getvalue(), zip_name


def prepare_email_attachment(
    zip_bytes: bytes,
    zip_filename: str,
    *,
    results: Optional[list] = None,
    keyword: str = "",
    country: str = "",
    prefer_light: bool = False,
) -> tuple:
    """
    准备邮件附件。过大或用户选择轻量模式时，改发仅表格版。
    返回 (attachment_bytes, attachment_filename, note_for_user)。
    """
    if prefer_light and results:
        light_bytes, light_name = build_export_zip(
            results, keyword, country, include_videos=False
        )
        note = "已发送轻量包（浅捞表格.xlsx + 浅捞汇总.md，不含视频）。完整包请在本页下载。"
        return light_bytes, light_name, note

    if len(zip_bytes) <= EMAIL_MAX_ATTACHMENT_BYTES:
        return zip_bytes, zip_filename, ""

    if not results:
        raise ValueError(
            f"附件 {_format_bytes(len(zip_bytes))} 超过 Gmail 限制（约 25MB），"
            "且无法生成轻量包。请勾选「邮件不含视频」或使用页面下载完整包。"
        )

    light_bytes, light_name = build_export_zip(
        results, keyword, country, include_videos=False
    )
    if len(light_bytes) > EMAIL_MAX_ATTACHMENT_BYTES:
        raise ValueError(
            f"即使不含视频，附件仍达 {_format_bytes(len(light_bytes))}，超过邮件限制。"
        )

    note = (
        f"完整包 {_format_bytes(len(zip_bytes))} 超过 Gmail 限制，"
        f"已自动改发轻量包（{_format_bytes(len(light_bytes))}，不含 mp4）。"
        "视频请在本工具页面点击「下载压缩包」。"
    )
    return light_bytes, light_name, note


def _parse_email_list(text: str) -> list:
    addrs = []
    for part in re.split(r"[,;\s\n]+", text or ""):
        addr = part.strip()
        if addr and EMAIL_RE.match(addr):
            addrs.append(addr)
    return addrs


def _sanitize_smtp_password(password: str) -> str:
    return re.sub(r"\s+", "", (password or "").strip())


def _get_smtp_config(override: Optional[dict] = None) -> dict:
    override = override or {}
    port_raw = override.get("port") or _secret("email", "smtp_port") or "587"
    try:
        port = int(port_raw)
    except (TypeError, ValueError):
        port = 587

    user = (override.get("user") or _secret("email", "smtp_user")).strip()
    password = _sanitize_smtp_password(override.get("password") or _secret("email", "smtp_password"))
    host = (override.get("host") or _secret("email", "smtp_host")).strip()
    from_addr = (override.get("from_addr") or _secret("email", "from_addr") or user).strip()
    use_tls = override.get("use_tls")
    if use_tls is None:
        use_tls = _secret("email", "use_tls", default="true").lower() != "false"
    use_ssl = override.get("use_ssl")
    if use_ssl is None:
        use_ssl = port == 465 or _secret("email", "use_ssl", default="false").lower() == "true"

    if not host or not user or not password or not from_addr:
        return {}
    return {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "from_addr": from_addr,
        "from_name": override.get("from_name") or _secret("email", "from_name") or "FB广告库浅捞工具",
        "use_tls": use_tls and not use_ssl,
        "use_ssl": use_ssl,
    }


def _smtp_send(cfg: dict, msg: MIMEMultipart, to_addrs: list) -> None:
    context = ssl.create_default_context()
    timeout = 60
    if cfg.get("use_ssl"):
        with smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=timeout, context=context) as smtp:
            smtp.login(cfg["user"], cfg["password"])
            smtp.sendmail(cfg["from_addr"], to_addrs, msg.as_string())
        return

    with smtplib.SMTP(cfg["host"], cfg["port"], timeout=timeout) as smtp:
        smtp.ehlo()
        if cfg.get("use_tls", True):
            smtp.starttls(context=context)
            smtp.ehlo()
        smtp.login(cfg["user"], cfg["password"])
        smtp.sendmail(cfg["from_addr"], to_addrs, msg.as_string())


def send_export_email(
    to_addrs: list,
    zip_bytes: bytes,
    zip_filename: str,
    search_keyword: str,
    country: str,
    smtp_override: Optional[dict] = None,
    *,
    results: Optional[list] = None,
) -> tuple:
    cfg = _get_smtp_config(smtp_override)
    if not cfg:
        return False, "未配置 SMTP。请在侧边栏填写发信配置，或在 secrets.toml 中配置 [email]。"
    if not to_addrs:
        return False, "请填写至少一个有效的收件人邮箱。"

    def _build_message(payload: bytes, filename: str, extra_note: str = "") -> MIMEMultipart:
        subject = f"FB 广告库浅捞 · {search_keyword} ({country})"
        msg = MIMEMultipart()
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = formataddr((cfg["from_name"], cfg["from_addr"]))
        msg["To"] = ", ".join(to_addrs)
        body = f"""您好，

附件为 FB 广告库浅捞结果，包含：
- 浅捞表格.xlsx
- 浅捞汇总.md
{"- videos/ 目录下的 mp4 视频" if "tables-only" not in filename else "- （本邮件为轻量包，不含 mp4 视频，请在本工具页面下载完整包）"}

搜索关键词：{search_keyword}
国家/地区：{country}
附件大小：{_format_bytes(len(payload))}
生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
{extra_note}

此邮件由工具自动发送，请勿直接回复。
"""
        msg.attach(MIMEText(body, "plain", "utf-8"))
        attachment = MIMEApplication(payload, Name=filename)
        attachment.add_header("Content-Disposition", "attachment", filename=filename)
        msg.attach(attachment)
        return msg

    try:
        msg = _build_message(zip_bytes, zip_filename)
        _smtp_send(cfg, msg, to_addrs)
        return True, f"✅ 已发送至：{', '.join(to_addrs)}"
    except smtplib.SMTPDataError as e:
        err_text = str(e).lower()
        if results and ("552" in err_text or "size" in err_text or "maxsize" in err_text):
            try:
                light_bytes, light_name, note = prepare_email_attachment(
                    zip_bytes,
                    zip_filename,
                    results=results,
                    keyword=search_keyword,
                    country=country,
                    prefer_light=True,
                )
                msg = _build_message(light_bytes, light_name, extra_note=note)
                _smtp_send(cfg, msg, to_addrs)
                return True, f"✅ 已发送至：{', '.join(to_addrs)}（{note}）"
            except Exception as retry_exc:
                return False, f"❌ 邮件过大且轻量包重试仍失败：{retry_exc}"
        return False, f"❌ SMTP 错误：{e}"
    except smtplib.SMTPAuthenticationError as e:
        return False, f"❌ SMTP 登录失败：{e}。Gmail 请使用应用专用密码。"
    except smtplib.SMTPException as e:
        return False, f"❌ SMTP 错误：{e}"
    except Exception as e:
        return False, f"❌ 邮件发送失败：{e}"


def render_fb_competitor_tool(*, embedded: bool = False) -> None:
    # ------------------------------------------------------------------
    # 侧边栏 — 模块一
    # ------------------------------------------------------------------
    if not embedded:
        with st.sidebar:
                st.header("🔑 全局配置")
                if APIFY_TOKEN_SESSION_KEY not in st.session_state:
                    st.session_state[APIFY_TOKEN_SESSION_KEY] = _secret("apify", "token")
                apify_token = st.text_input(
                    "Apify API Token",
                    type="password",
                    help="在 Apify → Settings → Integrations 获取。也可在 Streamlit Secrets 配置 [apify] token",
                    key=APIFY_TOKEN_SESSION_KEY,
                )
                _secret_apify = _sanitize_apify_token(_secret("apify", "token"))
                if _secret_apify:
                    st.caption("✅ Secrets 中已配置 Apify Token")
                elif (apify_token or "").strip():
                    st.caption("✅ 使用侧边栏填写的 Apify Token")
                else:
                    st.caption("⚠️ 未配置 Apify Token")
                st.markdown("---")
                _auto_proxy, _auto_proxy_source = init_gemini_proxy_for_session()
                with st.expander("🎬 Gemini 视频模式（检索验证 + 浅捞必填）", expanded=False):
                    st.text_input(
                        "Google Gemini API Key",
                        type="password",
                        key=SUITE_GEMINI_API_KEY,
                        help="步骤①检索需用 Gemini 验证视频与关键词是否相关；步骤②浅捞识别 Hook 列。也可在 secrets.toml 预填。",
                    )
        
                    _model_options = get_gemini_video_model_options()
                    _model_ids = [item["id"] for item in _model_options]
                    _default_model = (_secret("gemini", "model", GEMINI_MODEL) or GEMINI_MODEL).strip()
                    if not _is_capable_gemini_video_model(_default_model) or _default_model not in _model_ids:
                        _default_model = GEMINI_MODEL
                    if GEMINI_MODEL_SESSION_KEY not in st.session_state:
                        st.session_state[GEMINI_MODEL_SESSION_KEY] = _default_model
                    elif st.session_state.get(GEMINI_MODEL_SESSION_KEY) not in _model_ids:
                        st.session_state[GEMINI_MODEL_SESSION_KEY] = _default_model
        
                    gemini_model = st.selectbox(
                        "Gemini 视频模型",
                        options=_model_ids,
                        format_func=get_gemini_model_label,
                        key=GEMINI_MODEL_SESSION_KEY,
                        help="仅列出当前 API 可用的视频模型（1.5 / 2.0 已下线）",
                    )
        
                    auto_proxy, auto_source = _auto_proxy, _auto_proxy_source
                    if "gemini_proxy_field" not in st.session_state:
                        st.session_state.gemini_proxy_field = auto_proxy or _secret("gemini", "proxy_url")
        
                    col_proxy, col_redetect = st.columns([3, 1])
                    with col_proxy:
                        gemini_proxy = st.text_input(
                            "HTTPS 代理",
                            key="gemini_proxy_field",
                            placeholder="启动时自动检测，如 http://127.0.0.1:7897",
                            help="每次打开页面会自动探测系统代理 / 常见本地端口；可手动修改",
                        )
                    with col_redetect:
                        st.write("")
                        st.write("")
                        st.button(
                            "🔄",
                            help="重新检测代理",
                            key="redetect_gemini_proxy",
                            on_click=_on_redetect_gemini_proxy,
                        )
        
                    if gemini_proxy:
                        apply_gemini_proxy(gemini_proxy)
                    elif auto_proxy:
                        apply_gemini_proxy(auto_proxy)
        
                    if auto_source:
                        if auto_proxy:
                            st.caption(f"✅ 已自动配置：`{auto_proxy}`（来源：{auto_source}）")
                        else:
                            st.caption(f"⚠️ {auto_source}。请开启 Clash/VPN 后点 🔄 重新检测。")
        
                    if st.button("🔌 测试 Gemini 连接", use_container_width=True, key="test_gemini"):
                        ok, err = check_gemini_connectivity(gemini_proxy or auto_proxy)
                        if ok:
                            st.success("✅ 可以连接 generativelanguage.googleapis.com")
                        else:
                            st.error(f"❌ {err}")
                    st.caption(
                        f"当前模型：`{gemini_model}` · "
                        f"前 {GEMINI_HOOK_CLIP_SECONDS}s · HookVO=台词 / Text Hook=字幕 · "
                        f"压缩 {GEMINI_VIDEO_MAX_HEIGHT}p · 已排除 Lite 弱模型"
                    )
                st.markdown("---")
                with st.expander("📧 发邮件配置（可选）", expanded=False):
                    if SUITE_SMTP_HOST not in st.session_state:
                        st.session_state[SUITE_SMTP_HOST] = _secret("email", "smtp_host", "smtp.gmail.com")
                    if SUITE_SMTP_PORT not in st.session_state:
                        st.session_state[SUITE_SMTP_PORT] = _secret("email", "smtp_port", "587")
                    if SUITE_SMTP_USER not in st.session_state:
                        st.session_state[SUITE_SMTP_USER] = _secret("email", "smtp_user")
                    if SUITE_SMTP_PASSWORD not in st.session_state:
                        st.session_state[SUITE_SMTP_PASSWORD] = _secret("email", "smtp_password")
                    if SUITE_SMTP_FROM_NAME not in st.session_state:
                        st.session_state[SUITE_SMTP_FROM_NAME] = _secret("email", "from_name", "FB广告库浅捞工具")
                    st.text_input("SMTP 服务器", key=SUITE_SMTP_HOST)
                    st.text_input("SMTP 端口", key=SUITE_SMTP_PORT)
                    st.text_input("发件邮箱", key=SUITE_SMTP_USER)
                    st.text_input(
                        "邮箱密码 / 应用专用密码",
                        type="password",
                        key=SUITE_SMTP_PASSWORD,
                    )
                    st.text_input("发件人名称", key=SUITE_SMTP_FROM_NAME)
                    default_recipients = _secret("email", "default_recipients")
                    st.caption("Gmail：端口 587 + 应用专用密码；SSL 端口 465 也支持。可在 secrets.toml 的 [email] 预填。")
                st.markdown("---")
                st.caption("提示：密钥仅在本次会话中使用；侧边栏留空时会尝试读取 secrets.toml。")

    apify_token = st.session_state.get(APIFY_TOKEN_SESSION_KEY, "") or _secret("apify", "token")
    gemini_api_key = get_gemini_api_key()
    _auto_proxy, _auto_proxy_source = init_gemini_proxy_for_session()
    gemini_proxy = (
        st.session_state.get("gemini_proxy_field", "")
        or st.session_state.get(GEMINI_PROXY_SESSION_KEY, "")
        or _auto_proxy
        or _secret("gemini", "proxy_url")
    )
    gemini_model = st.session_state.get(GEMINI_MODEL_SESSION_KEY) or _secret("gemini", "model", GEMINI_MODEL) or GEMINI_MODEL
    smtp_host = st.session_state.get(SUITE_SMTP_HOST, _secret("email", "smtp_host", "smtp.gmail.com"))
    smtp_port = st.session_state.get(SUITE_SMTP_PORT, _secret("email", "smtp_port", "587"))
    smtp_user = st.session_state.get(SUITE_SMTP_USER, _secret("email", "smtp_user"))
    smtp_password = st.session_state.get(SUITE_SMTP_PASSWORD, _secret("email", "smtp_password"))
    smtp_from_name = st.session_state.get(SUITE_SMTP_FROM_NAME, _secret("email", "from_name", "FB广告库浅捞工具"))
    default_recipients = _secret("email", "default_recipients")

    if not embedded:
        st.title("🎬 FB 广告库浅捞工具")
        st.caption("① 广告库捞视频 → ② 本地免费填表（默认零 Token）→ 打包 / 发邮件")

    tab_fetch, tab_analyze = st.tabs(["① 捞视频下载", "② 上传视频浅捞"])

    # ==================================================================
    # 步骤①：Apify 抓取 + 下载 + Gemini 内容相关性验证
    # ==================================================================
    with tab_fetch:
        col_kw, col_country = st.columns([2, 1])
        with col_kw:
            search_keyword = st.text_input(
                "广告库搜索关键词",
                placeholder="例如：laundry detergent、shapewear、pet food",
                help=(
                    "Meta Ad Library 关键词搜索 · 优先近 7 天，不足 3 条自动放宽至 14/30/60 天 · "
                    "剔除全篇 AI 视频（穿插 AI 片段可保留）· 时长≤60s · "
                    "Gemini 验证视频内容与关键词相关 · 曝光 Top 3"
                ),
                key="fetch_keyword",
            )
        with col_country:
            country = st.selectbox(
                "投放国家/地区",
                options=["ALL", "US", "GB", "CA", "AU", "DE", "FR", "JP", "SG"],
                key="fetch_country",
            )

        fetch_button = st.button(
            f"📥 智能检索 Top 3（优先7天 · 相关 · 非全篇AI · ≤60s）",
            type="primary",
            use_container_width=True,
        )

        if fetch_button:
            clear_export_bundle()
            apify_token = _sanitize_apify_token(apify_token) or _sanitize_apify_token(_secret("apify", "token"))
            if not apify_token:
                st.error("请先在侧边栏填写 Apify API Token，或在 Streamlit Cloud Secrets 中配置 [apify] token。")
                st.stop()
            apify_ok, apify_err = check_apify_connectivity(apify_token)
            if not apify_ok:
                st.error(f"❌ Apify Token 校验失败：{apify_err}")
                with st.expander("如何配置 Apify Token", expanded=True):
                    st.markdown(_apify_auth_help_markdown())
                st.stop()
            effective_gemini_key = (gemini_api_key or "").strip() or _secret("gemini", "api_key")
            if not effective_gemini_key:
                st.error(
                    "检索验证需要 Gemini API Key（用于判断视频内容与关键词是否相关）。"
                    "请在侧边栏 Gemini 区域填写，或在 Secrets 中配置 [gemini] api_key。"
                )
                st.stop()
            if not search_keyword.strip():
                st.error("请输入广告库搜索关键词。")
                st.stop()

            effective_gemini_proxy = (
                gemini_proxy.strip()
                or st.session_state.get(GEMINI_PROXY_SESSION_KEY, "")
                or _auto_proxy
            )
            ok, conn_err = check_gemini_connectivity(effective_gemini_proxy)
            if not ok:
                st.error(f"❌ 无法连接 Gemini API：{conn_err}")
                st.stop()

            cleanup_fetched_batch(st.session_state.get("fetched_batch"))
            progress = FetchProgress(TOP_N)
            progress.set_step(0, "Apify 抓取 + Gemini 内容验证，通常需 3~10 分钟")

            try:
                def _fetch_progress(ratio: float, step: str, detail: str = ""):
                    if ratio < 0.35:
                        progress.set_step(0, detail or step)
                    elif ratio < 0.85:
                        progress.set_step(1, detail or step)
                    else:
                        progress.set_step(2, detail or step)
                    progress.update(ratio, step, detail)

                downloaded_ads, kw_stats = collect_validated_top_videos(
                    apify_token,
                    search_keyword.strip(),
                    country,
                    top_n=TOP_N,
                    gemini_api_key=effective_gemini_key,
                    gemini_proxy=effective_gemini_proxy,
                    gemini_model=resolve_gemini_model(gemini_model),
                    on_progress=_fetch_progress,
                )

                progress.complete_step(0)
                progress.complete_step(1)

                if not downloaded_ads:
                    st.warning(f"⚠️ 未能凑满 {TOP_N} 条符合要求的视频。")
                    with st.expander("查看筛选详情", expanded=True):
                        st.markdown(_format_fetch_failure_diagnosis(kw_stats))
                    st.info(
                        "常见原因：① 独立视频候选不足（Apify 返回的多条广告可能共用同一视频）；"
                        "② 视频均超过 60 秒；③ 视频下载失败；④ Gemini 不可用（现已自动降级为信任 Meta 搜索）。"
                        "建议换更热门关键词，或在 Meta Ad Library 网页确认有足够独立视频。"
                    )
                    st.stop()

                if len(downloaded_ads) < TOP_N:
                    extra = ""
                    if kw_stats.get("fallback_filled"):
                        extra = f" · 信任Meta补齐 {kw_stats.get('fallback_count', 0)} 条"
                    st.warning(
                        f"⚠️ 仅找到 {len(downloaded_ads)}/{TOP_N} 条视频（"
                        f"独立候选 {kw_stats.get('unique_video_candidates', '?')} · "
                        f"超时长 {kw_stats.get('duration_filtered', 0)} · "
                        f"Gemini不相关 {kw_stats.get('relevance_rejected', 0)}{extra}）"
                    )
                elif kw_stats.get("fallback_count"):
                    st.info(
                        f"ℹ️ 其中 {kw_stats.get('fallback_count')} 条由 Meta 关键词搜索信任模式补齐"
                        "（Gemini 未通过的视频已跳过二次内容验证）。"
                    )

                progress.update(
                    0.88,
                    "筛选完成",
                    f"独立视频候选 {kw_stats.get('unique_video_candidates', 0)} · "
                    f"已验证 {kw_stats.get('candidates_tried', 0)} · "
                    f"最终 {len(downloaded_ads)} 条",
                )

                progress.complete_step(2)
                progress.set_step(3)
                progress.update(0.9, "打包视频压缩包...")
                zip_bytes, zip_name = build_videos_only_zip(
                    downloaded_ads, search_keyword.strip(), country
                )

                st.session_state["fetched_batch"] = {
                    "keyword": search_keyword.strip(),
                    "country": country,
                    "ads": downloaded_ads,
                }
                progress.finish()

                video_source_payload = [
                    {
                        "index": ad["index"],
                        "local_path": ad["local_path"],
                        "brand_name": ad.get("brand_name", ""),
                        "start_date": ad.get("start_date", ""),
                        "video_url": ad.get("video_url", ""),
                        "raw": ad.get("raw") or {},
                        "source_name": os.path.basename(ad["local_path"]) if ad.get("local_path") else "",
                        "download_error": ad.get("download_error"),
                    }
                    for ad in downloaded_ads
                ]
                meta_results = build_local_analysis_results(
                    video_source_payload,
                    product_category=search_keyword.strip(),
                )
                meta_results = finalize_shallow_results(
                    meta_results,
                    expected_count=len(downloaded_ads),
                )
                full_zip_bytes, full_zip_name = build_export_zip(
                    meta_results, search_keyword.strip(), country
                )
                save_export_bundle(
                    meta_results,
                    full_zip_bytes,
                    full_zip_name,
                    search_keyword.strip(),
                    country,
                )

                st.success(f"✅ 已下载 {len(downloaded_ads)} 条视频。")
                st.download_button(
                    "📥 下载完整包（浅捞表格 + 视频）",
                    data=full_zip_bytes,
                    file_name=full_zip_name,
                    mime="application/zip",
                    use_container_width=True,
                    key="fetch_full_zip",
                )
                st.download_button(
                    "📥 仅下载视频包（mp4 + 元数据.json）",
                    data=zip_bytes,
                    file_name=zip_name,
                    mime="application/zip",
                    use_container_width=True,
                    key="fetch_videos_only_zip",
                )
                st.caption(
                    "已用本地 ffmpeg + 规则推断填表（零 Token）。"
                    "Hook/场景等为算法推测，可在 Excel 中微调。"
                )

                st.subheader("已下载视频预览")
                for ad in downloaded_ads:
                    st.markdown(f"**#{ad['index']} {ad['brand_name']}** · 开始投放：{ad['start_date']}")
                    if ad["local_path"] and os.path.exists(ad["local_path"]):
                        st.video(ad["local_path"])
                    st.caption(ad["video_url"])

            except Exception as e:
                if _is_apify_auth_error(e):
                    st.error("❌ Apify Token 无效或已过期，无法抓取广告库。")
                    with st.expander("如何配置 Apify Token", expanded=True):
                        st.markdown(_apify_auth_help_markdown())
                else:
                    st.error(f"❌ 抓取失败：{e}")
                    with st.expander("错误详情"):
                        st.code(traceback.format_exc())

        elif st.session_state.get("fetched_batch"):
            batch = st.session_state["fetched_batch"]
            st.info(
                f"当前会话已有下载批次：关键词「{batch.get('keyword')}」· "
                f"{len(batch.get('ads', []))} 条视频。可在步骤②上传或直接使用。"
            )
            for ad in batch.get("ads", []):
                st.markdown(f"**#{ad['index']} {ad.get('brand_name', '')}**")
                if ad.get("local_path") and os.path.exists(ad["local_path"]):
                    st.video(ad["local_path"])

        fetch_bundle = get_export_bundle()
        if fetch_bundle and fetch_bundle.get("zip_bytes"):
            st.markdown("---")
            render_export_download_section(
                fetch_bundle.get("results") or [],
                fetch_bundle.get("zip_bytes") or b"",
                fetch_bundle.get("zip_filename") or "export.zip",
                fetch_bundle.get("keyword") or "",
                fetch_bundle.get("country") or "ALL",
                default_recipients=default_recipients,
                smtp_host=smtp_host,
                smtp_port=smtp_port,
                smtp_user=smtp_user,
                smtp_password=smtp_password,
                smtp_from_name=smtp_from_name,
                key_prefix="fetch",
            )
    with tab_analyze:
        st.markdown(
            "将步骤①下载的视频（或任意 mp4）生成浅捞表格。"
            " **默认「本地免费填表」：零 Token，不上传视频。**"
        )

        batch = st.session_state.get("fetched_batch")
        use_server_videos = False
        if batch and batch.get("ads"):
            use_server_videos = st.checkbox(
                "直接使用步骤①已下载的视频（跳过上传）",
                value=True,
                help="若你已在步骤①下载，可勾选此项免去重新上传。",
            )

        uploaded_files = None
        if not use_server_videos:
            uploaded_files = st.file_uploader(
                "上传视频文件（mp4，最多 3 个，按顺序对应视频编号 1~3）",
                type=["mp4"],
                accept_multiple_files=True,
            )
            if uploaded_files and len(uploaded_files) > TOP_N:
                st.warning(f"最多处理 {TOP_N} 个视频，将只取前 {TOP_N} 个。")
                uploaded_files = uploaded_files[:TOP_N]

        analyze_keyword = st.text_input(
            "批次标签 / 搜索关键词（用于打包文件名 & 商品类目）",
            value=(batch or {}).get("keyword", ""),
            placeholder="可与步骤①一致，或自行填写",
            key="analyze_keyword",
        )
        _country_options = ["ALL", "US", "GB", "CA", "AU", "DE", "FR", "JP", "SG"]
        _default_country = (batch or {}).get("country", "ALL")
        analyze_country = st.selectbox(
            "地区标签",
            options=_country_options,
            index=_country_options.index(_default_country) if _default_country in _country_options else 0,
            key="analyze_country",
        )

        analyze_mode = st.radio(
            "分析方式",
            options=["local", "gemini"],
            index=0,
            format_func=lambda x: {
                "local": "本地免费填表（零 Token，推荐）",
                "gemini": "Gemini 视频浅捞（需 VPN）",
            }[x],
            horizontal=True,
            help="本地模式不填 J/K 列。Gemini 模式识别：HookVO=前三秒台词，Text Hook=前三秒画面字幕。",
        )
        if analyze_mode == "local":
            st.caption("本地模式不填 J/K 列与个人见解；Gemini 模式仅精准识别 J/K 列。")
        if analyze_mode == "gemini":
            _active_model = resolve_gemini_model()
            st.caption(
                f"当前 Gemini 模型：`{_active_model}` · {get_gemini_model_label(_active_model)} · "
                f"仅分析前 {GEMINI_HOOK_CLIP_SECONDS}s：HookVO=台词，Text Hook=画面字幕 · 个人见解列留空"
            )

        analyze_button = st.button(
            "🆓 开始本地免费填表" if analyze_mode == "local" else "🤖 开始 Gemini 浅捞",
            type="primary",
            use_container_width=True,
        )

        if analyze_button:
            clear_export_bundle()
            video_sources = []
            temp_paths = []
            try:
                video_sources, temp_paths = collect_video_sources(
                    use_server_videos=use_server_videos,
                    batch=batch,
                    uploaded_files=uploaded_files,
                )
            except ValueError as e:
                st.error(str(e))
                st.stop()

            label_kw = analyze_keyword.strip() or "manual-upload"
            label_country = analyze_country
            results = []

            try:
                if analyze_mode == "local":
                    progress = LocalAnalyzeProgress(len(video_sources))
                    progress.set_step(0, f"共 {len(video_sources)} 个视频")
                    progress.update(0.08, "已接收视频", "即将本地分析（零 Token）")

                    def _local_status(msg: str, detail: str = ""):
                        idx_match = re.search(r"\[(\d+)/(\d+)\]", msg)
                        idx = int(idx_match.group(1)) if idx_match else 1
                        if "规则" in msg:
                            progress.set_step(2)
                            sub = 0.7
                        else:
                            progress.set_step(1)
                            sub = 0.35
                        progress.update(progress.video_ratio(idx, sub), msg, detail)

                    results = build_local_analysis_results(
                        video_sources,
                        product_category=label_kw,
                        on_status=_local_status,
                    )
                    results = finalize_shallow_results(
                        results,
                        expected_count=len(video_sources),
                    )
                    progress.set_step(3)
                    progress.update(0.92, "打包浅捞结果...")
                else:
                    gemini_api_key = gemini_api_key.strip() or _secret("gemini", "api_key")
                    if not gemini_api_key:
                        st.error("请先在侧边栏 Gemini 区域填写 API Key。")
                        st.stop()

                    effective_gemini_proxy = (
                        gemini_proxy.strip()
                        or st.session_state.get(GEMINI_PROXY_SESSION_KEY, "")
                        or _auto_proxy
                    )
                    configure_gemini_client(
                        gemini_api_key,
                        effective_gemini_proxy,
                        model=resolve_gemini_model(),
                    )

                    ok, conn_err = check_gemini_connectivity(effective_gemini_proxy)
                    if not ok:
                        st.error(f"❌ 无法连接 Gemini API：{conn_err}")
                        st.info("可改用「本地免费填表」模式（零 Token，无需 VPN）。")
                        st.stop()

                    progress = AnalyzeProgress(len(video_sources))
                    progress.set_step(0, f"共 {len(video_sources)} 个视频待浅捞")
                    progress.update(0.05, "已接收视频", "即将上传至 Gemini")

                    gemini_quota_exhausted = False
                    for idx, src in enumerate(video_sources, start=1):
                        if gemini_quota_exhausted:
                            progress.set_step(2)
                            progress.update(
                                progress.video_ratio(idx, 0.7),
                                f"[{idx}/{len(video_sources)}] 额度已用尽，本地免费填表",
                                src.get("brand_name") or "",
                            )
                            results.append(
                                build_local_shallow_result_entry(
                                    src,
                                    product_category=label_kw,
                                    error=f"⚠️ {_gemini_quota_short_note()}",
                                )
                            )
                            continue

                        progress.set_step(1)
                        progress.update(progress.video_ratio(idx, 0.1), f"[{idx}/{len(video_sources)}] 准备 Gemini 分析")

                        def _gemini_status(msg: str, detail: str = "", _idx=idx):
                            sub = 0.35
                            if "压缩" in msg:
                                sub = 0.12
                            elif "上传" in msg:
                                sub = 0.2
                            elif "转码" in msg or "处理" in msg or "等待" in msg:
                                sub = 0.55
                            elif "生成" in msg:
                                sub = 0.88
                                progress.set_step(2)
                            progress.update(
                                progress.video_ratio(_idx, sub),
                                f"[{_idx}/{len(video_sources)}] {msg}",
                                detail,
                            )

                        try:
                            hook_fields = extract_video_hooks_with_gemini(
                                src["local_path"],
                                video_index=src["index"],
                                video_url=src["video_url"],
                                raw_item=src.get("raw"),
                                on_status=_gemini_status,
                            )
                            results.append(
                                {
                                    "index": src["index"],
                                    "brand_name": src.get("brand_name"),
                                    "start_date": src.get("start_date"),
                                    "video_url": src["video_url"],
                                    "local_path": src["local_path"],
                                    "raw": src.get("raw") or {},
                                    "product_category": label_kw,
                                    "hook_fields": hook_fields,
                                    "raw_report": json.dumps(hook_fields, ensure_ascii=False),
                                    "report": None,
                                    "error": None,
                                }
                            )
                        except Exception as e:
                            if _is_gemini_quota_error(e):
                                gemini_quota_exhausted = True
                                st.warning(_gemini_quota_hint(e))
                                results.append(
                                    build_local_shallow_result_entry(
                                        src,
                                        product_category=label_kw,
                                        error=f"⚠️ {_gemini_quota_short_note()}",
                                    )
                                )
                            else:
                                results.append(
                                    {
                                        "index": src["index"],
                                        "brand_name": src.get("brand_name"),
                                        "start_date": src.get("start_date"),
                                        "video_url": src["video_url"],
                                        "local_path": src["local_path"],
                                        "raw": src.get("raw") or {},
                                        "product_category": label_kw,
                                        "report": None,
                                        "error": _gemini_error_hint(e),
                                    }
                                )

                    progress.set_step(3)
                    progress.update(0.92, "打包浅捞结果...")

                results = finalize_shallow_results(
                    results,
                    expected_count=len(video_sources),
                )
                zip_bytes, zip_filename = build_export_zip(results, label_kw, label_country)
                save_export_bundle(results, zip_bytes, zip_filename, label_kw, label_country)
                progress.finish()

            except Exception as e:
                st.error(f"❌ 浅捞过程出错：{e}")
                st.code(traceback.format_exc())
            finally:
                for path in temp_paths:
                    try:
                        if path and os.path.exists(path):
                            os.remove(path)
                    except Exception:
                        pass

        export_bundle = get_export_bundle()
        if export_bundle and export_bundle.get("zip_bytes"):
            render_export_download_section(
                export_bundle.get("results") or [],
                export_bundle.get("zip_bytes") or b"",
                export_bundle.get("zip_filename") or "export.zip",
                export_bundle.get("keyword") or "",
                export_bundle.get("country") or "ALL",
                default_recipients=default_recipients,
                smtp_host=smtp_host,
                smtp_port=smtp_port,
                smtp_user=smtp_user,
                smtp_password=smtp_password,
                smtp_from_name=smtp_from_name,
                key_prefix="analyze",
            )


if __name__ == "__main__":
    # Community Cloud 无法修改已部署应用的 Main file path；
    # 旧入口仍指向本文件时，自动启动根目录的营销工具套件。
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from marketing_suite_app import main as run_marketing_suite

    run_marketing_suite()
