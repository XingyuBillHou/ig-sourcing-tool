# -*- coding: utf-8 -*-
"""
Instagram 红人 Sourcing 工具
============================
功能概述:
    1. 抓取目标品牌/竞品 IG 账号最近 30-50 条公开贴文 (通过 Apify, 非 Meta 官方 API)
    2. 从贴文中提取联合创作者 (co_authors) 与被标记用户 (tagged_users) 作为潜在红人
    3. 二次调用 Apify 获取红人主页数据 (粉丝数 + 最近贴文互动数据)
    4. 计算预估互动率, 整理成 DataFrame 并支持一键导出 CSV

运行方式:
    pip install -r requirements.txt
    streamlit run ig_influencer_sourcing_app.py

注意:
    - 仅抓取公开数据, 请遵守 Apify 与 Instagram 的使用条款
    - Apify Actor 会消耗你账户的额度 (Compute Units / 按结果计费), 请控制抓取数量
"""

import re
from datetime import datetime
from typing import Literal, Optional

import pandas as pd
import streamlit as st
from apify_client import ApifyClient

# =========================================================================
# 全局配置: Apify Actor ID 占位变量
# =========================================================================
POST_SCRAPER_ACTOR_ID = "apify/instagram-post-scraper"
PROFILE_SCRAPER_ACTOR_ID = "apify/instagram-profile-scraper"

# Apify Starter Plan 用量配置 (月额度约 $29, 按官方 Actor 按量计费估算)
# Post Scraper: $1.00 / 1,000 条贴文 | Profile Scraper: $1.60 / 1,000 个主页
APIFY_STARTER_MONTHLY_CREDIT_USD = 29.0
POST_COST_PER_1K_USD = 1.00
PROFILE_COST_PER_1K_USD = 1.60

DEFAULT_POSTS_LIMIT = 200
DEFAULT_MAX_PROFILES = 75
STARTER_MAX_POSTS_LIMIT = 250
STARTER_MAX_PROFILES = 100
ENGAGEMENT_SAMPLE_POSTS = 15

# =========================================================================
# 品牌账号过滤配置
# =========================================================================
BRAND_CATEGORY_KEYWORDS = [
    "brand", "clothing", "apparel", "shopping", "retail", "store", "shop",
    "product/service", "company", "e-commerce", "ecommerce", "boutique",
    "jewelry", "cosmetics", "footwear", "accessories", "website",
    "advertising", "marketing agency", "media/news", "magazine",
    "furniture", "home decor", "restaurant", "cafe", "hotel", "travel agency",
    "beauty supplier", "wholesale", "manufacturer", "local business",
]
CREATOR_CATEGORY_KEYWORDS = [
    "creator", "blogger", "public figure", "influencer", "artist", "athlete",
    "model", "fitness", "coach", "personal blog", "video creator",
    "entrepreneur", "author", "dancer", "actor", "musician", "photographer",
    "health/beauty", "just for fun", "entertainer", "comedian", "chef",
    "digital creator", "content creator", "lifestyle", "fashion model",
]
BRAND_BIO_KEYWORDS = [
    "shop now", "official store", "free shipping", "use code", "discount code",
    "order now", "link in bio", "wholesale", "retail", "customer service",
    "flagship", "boutique", "collection", "new arrival", "official account",
]
CREATOR_BIO_KEYWORDS = [
    "collab", "collaboration", "dm for", "ugc", "content creator",
    "ambassador", "personal account", "my life", "daily vlog",
]
BRAND_URL_KEYWORDS = [
    "shop", "store", "myshopify", "amazon", "etsy", "taobao", "tmall",
    "shopee", "lazada", "shopify", "bigcartel", "woocommerce",
]
BRAND_USERNAME_TOKENS = (
    "shop", "store", "official", "brand", "co_", "hq", "global", "usa",
    "inc", "ltd", "boutique", "collection",
)

AccountType = Literal["brand", "influencer", "unknown"]

# =========================================================================
# 地区 → 国家 映射表 (键为小写)
# =========================================================================
REGION_TO_COUNTRY = {
    "uk": "United Kingdom", "england": "United Kingdom", "scotland": "United Kingdom",
    "wales": "United Kingdom", "usa": "United States", "america": "United States",
    "south korea": "South Korea", "korea": "South Korea", "uae": "United Arab Emirates",
    "hong kong": "Hong Kong", "macau": "Macao",
    "california": "United States", "new york": "United States", "texas": "United States",
    "florida": "United States", "nevada": "United States", "illinois": "United States",
    "washington": "United States", "georgia": "United States", "arizona": "United States",
    "colorado": "United States", "hawaii": "United States", "oregon": "United States",
    "massachusetts": "United States", "utah": "United States", "tennessee": "United States",
    "north carolina": "United States", "new jersey": "United States", "ohio": "United States",
    "michigan": "United States", "pennsylvania": "United States", "virginia": "United States",
    "los angeles": "United States", "nyc": "United States", "new york city": "United States",
    "miami": "United States", "san francisco": "United States", "chicago": "United States",
    "las vegas": "United States", "houston": "United States", "dallas": "United States",
    "atlanta": "United States", "seattle": "United States", "san diego": "United States",
    "london": "United Kingdom", "manchester": "United Kingdom",
    "paris": "France", "tokyo": "Japan", "osaka": "Japan", "kyoto": "Japan",
    "seoul": "South Korea", "sydney": "Australia", "melbourne": "Australia",
    "toronto": "Canada", "vancouver": "Canada", "montreal": "Canada",
    "dubai": "United Arab Emirates", "shanghai": "China", "beijing": "China",
    "hangzhou": "China", "shenzhen": "China", "guangzhou": "China",
    "taipei": "Taiwan", "bangkok": "Thailand", "bali": "Indonesia", "jakarta": "Indonesia",
    "mexico city": "Mexico", "sao paulo": "Brazil", "são paulo": "Brazil",
    "rio de janeiro": "Brazil", "berlin": "Germany", "munich": "Germany",
    "milan": "Italy", "rome": "Italy", "madrid": "Spain", "barcelona": "Spain",
    "amsterdam": "Netherlands", "mumbai": "India", "delhi": "India", "new delhi": "India",
    "manila": "Philippines", "kuala lumpur": "Malaysia", "ho chi minh city": "Vietnam",
    "hanoi": "Vietnam", "lisbon": "Portugal", "stockholm": "Sweden", "copenhagen": "Denmark",
    "zurich": "Switzerland", "vienna": "Austria", "dublin": "Ireland", "auckland": "New Zealand",
    "tel aviv": "Israel", "istanbul": "Turkey", "cape town": "South Africa",
}

RESERVED_IG_PATHS = {"p", "reel", "reels", "stories", "explore", "accounts", "tv", "direct"}


def estimate_run_cost_usd(posts_limit: int, profiles_limit: int) -> float:
    """按 Apify 官方 Instagram Actor 单价估算单次运行成本 (美元)。"""
    post_cost = posts_limit / 1000 * POST_COST_PER_1K_USD
    profile_cost = profiles_limit / 1000 * PROFILE_COST_PER_1K_USD
    return round(post_cost + profile_cost, 3)


def estimate_monthly_runs(posts_limit: int, profiles_limit: int) -> int:
    """估算 Starter 月额度下可支持的完整检索次数。"""
    per_run = estimate_run_cost_usd(posts_limit, profiles_limit)
    if per_run <= 0:
        return 0
    return int(APIFY_STARTER_MONTHLY_CREDIT_USD / per_run)


def select_top_candidates(influencer_dates: dict[str, str], limit: int) -> list[str]:
    """按最近合作日期优先选取候选账号, 尽量保留最新合作关系。"""
    ranked = sorted(
        influencer_dates.items(),
        key=lambda item: item[1] or "",
        reverse=True,
    )
    return [username for username, _ in ranked[:limit]]


def parse_instagram_url(url: str) -> str:
    """从单个 IG 主页链接中解析 username (仅支持一个链接)。"""
    text = (url or "").strip()
    if not text:
        raise ValueError("请输入 Instagram 主页链接。")

    if not re.search(r"instagram\.com|instagr\.am", text, re.I):
        raise ValueError("请提供完整的 Instagram 主页链接, 例如 https://www.instagram.com/nike/")

    if re.search(r"[\n,;]", text):
        raise ValueError("每次仅支持输入一个 IG 主页链接, 请勿输入多个链接。")

    matches = re.findall(r"(?:instagram\.com|instagr\.am)/([^/?#\s]+)", text, re.I)
    if len(matches) > 1:
        raise ValueError("每次仅支持输入一个 IG 主页链接, 请勿输入多个链接。")
    if not matches:
        raise ValueError("无法从链接中解析 IG 用户名, 请检查链接格式。")

    username = matches[0].lower()
    if username in RESERVED_IG_PATHS:
        raise ValueError("链接指向的是贴文/功能页, 请提供账号主页链接, 例如 https://www.instagram.com/nike/")
    return username


def account_type_label(account_type: AccountType) -> str:
    return {"brand": "品牌", "influencer": "红人", "unknown": "待确认"}[account_type]


def _get_default_dataset_id(run) -> Optional[str]:
    """从 Apify run 结果提取 dataset ID（兼容 dict 与 apify-client v3+ 的 Run 模型）。"""
    if run is None:
        return None
    if isinstance(run, dict):
        return run.get("defaultDatasetId") or run.get("default_dataset_id")
    return getattr(run, "default_dataset_id", None)


# =========================================================================
# 模块二 + 模块三: 核心抓取与数据处理类 (OOP 封装)
# =========================================================================
class InfluencerSourcer:
    """封装 Apify 调用与红人挖掘逻辑的核心类。"""

    def __init__(
        self,
        api_token: str,
        instagram_url: str,
        posts_limit: int = DEFAULT_POSTS_LIMIT,
        max_profiles: int = DEFAULT_MAX_PROFILES,
    ):
        self.account_handle = parse_instagram_url(instagram_url)
        self.posts_limit = posts_limit
        self.max_profiles = max_profiles
        self.client = ApifyClient(api_token)

    def scrape_brand_posts(self) -> list[dict]:
        """调用 Apify 的 Instagram Post Scraper, 抓取账号最近的公开贴文。"""
        run_input = {
            "username": [self.account_handle],
            "resultsLimit": self.posts_limit,
        }

        try:
            run = self.client.actor(POST_SCRAPER_ACTOR_ID).call(run_input=run_input)
        except Exception as e:
            raise RuntimeError(f"调用贴文抓取 Actor 失败: {e}") from e

        dataset_id = _get_default_dataset_id(run)
        if not dataset_id:
            raise RuntimeError("Actor 运行结束但未返回数据集 ID, 请检查 Actor 配置。")

        try:
            items = list(self.client.dataset(dataset_id).iterate_items())
        except Exception as e:
            raise RuntimeError(f"读取贴文数据集失败: {e}") from e

        if not items:
            raise RuntimeError(
                f"未抓取到 @{self.account_handle} 的任何贴文。"
                "可能原因: 账号不存在 / 私密账号 / 链接错误。"
            )
        return items

    def extract_influencers(self, posts: list[dict]) -> dict[str, str]:
        """遍历贴文, 提取 co-authors 与 tagged users, 过滤输入账号自身。"""
        influencer_last_date: dict[str, str] = {}

        for post in posts:
            raw_ts = post.get("timestamp") or post.get("takenAt") or ""
            post_date = self._parse_date(raw_ts)
            candidates: list[str] = []

            for co in (post.get("coauthorProducers") or post.get("co_authors") or []):
                username = self._safe_username(co)
                if username:
                    candidates.append(username)

            for tagged in (post.get("taggedUsers") or post.get("tagged_users") or []):
                username = self._safe_username(tagged)
                if username:
                    candidates.append(username)

            for username in candidates:
                if username.lower() == self.account_handle:
                    continue
                existing = influencer_last_date.get(username)
                if existing is None or (post_date and post_date > existing):
                    influencer_last_date[username] = post_date or ""

        return influencer_last_date

    def enrich_influencers(
        self,
        influencer_dates: dict[str, str],
        input_account_type: AccountType,
        progress_callback=None,
    ) -> tuple[list[dict], list[str], AccountType, dict[str, int]]:
        """批量抓取候选账号主页, 按用户指定的输入类型做反向筛选。"""
        total_candidates = len(influencer_dates)
        candidate_usernames = select_top_candidates(influencer_dates, self.max_profiles)
        if not candidate_usernames:
            return [], [], input_account_type, {
                "total_candidates": total_candidates,
                "profiles_scraped": 0,
            }

        input_is_brand = input_account_type == "brand"
        run_input = {"usernames": candidate_usernames}

        if progress_callback:
            progress_callback(0.1, f"正在抓取 {len(candidate_usernames)} 个候选账号的主页数据...")

        try:
            run = self.client.actor(PROFILE_SCRAPER_ACTOR_ID).call(run_input=run_input)
        except Exception as e:
            raise RuntimeError(f"调用主页抓取 Actor 失败: {e}") from e

        dataset_id = _get_default_dataset_id(run)
        if not dataset_id:
            raise RuntimeError("主页 Actor 运行结束但未返回数据集 ID。")

        try:
            profiles = list(self.client.dataset(dataset_id).iterate_items())
        except Exception as e:
            raise RuntimeError(f"读取主页数据集失败: {e}") from e

        if progress_callback:
            progress_callback(0.6, "正在分类候选账号并计算互动率...")

        results: list[dict] = []
        filtered_out: list[str] = []
        for profile in profiles:
            try:
                username = (profile.get("username") or "").strip()
                if not username:
                    continue

                candidate_type = self._classify_account(profile)
                if not self._should_keep_candidate(candidate_type, input_is_brand):
                    filtered_out.append(username)
                    continue

                followers = (
                    profile.get("followersCount")
                    or profile.get("followers")
                    or profile.get("edge_followed_by", {}).get("count")
                    or 0
                )
                latest_posts = (profile.get("latestPosts") or profile.get("latest_posts") or [])[:ENGAGEMENT_SAMPLE_POSTS]
                engagement_rate = self._calc_engagement_rate(latest_posts, followers)
                active_countries = self._extract_active_countries(profile, latest_posts)

                results.append({
                    "username": username,
                    "profile_url": f"https://www.instagram.com/{username}/",
                    "followers": int(followers),
                    "engagement_rate": engagement_rate,
                    "active_countries": active_countries,
                    "last_collab_date": influencer_dates.get(username, ""),
                    "account_type": account_type_label(candidate_type),
                })
            except Exception:
                continue

        if progress_callback:
            progress_callback(1.0, "数据处理完成!")
        return results, filtered_out, input_account_type, {
            "total_candidates": total_candidates,
            "profiles_scraped": len(candidate_usernames),
        }

    @staticmethod
    def build_dataframe(records: list[dict]) -> pd.DataFrame:
        columns = ["账号主页链接", "账号类型", "粉丝数", "互动率评估", "活跃国家 (Top 3)", "最近合作日期"]
        if not records:
            return pd.DataFrame(columns=columns)

        df = pd.DataFrame(records)
        df["互动率评估"] = df["engagement_rate"].apply(
            lambda x: f"{x * 100:.2f}%" if x is not None else "数据不足"
        )
        df = df.rename(columns={
            "profile_url": "账号主页链接",
            "account_type": "账号类型",
            "followers": "粉丝数",
            "active_countries": "活跃国家 (Top 3)",
            "last_collab_date": "最近合作日期",
        })
        df = df.sort_values(by="粉丝数", ascending=False).reset_index(drop=True)
        return df[columns]

    @staticmethod
    def _should_keep_candidate(candidate_type: AccountType, input_is_brand: bool) -> bool:
        """输入为品牌时保留红人/待确认; 输入为红人时保留品牌/待确认。"""
        if candidate_type == "unknown":
            return True
        if input_is_brand:
            return candidate_type == "influencer"
        return candidate_type == "brand"

    @staticmethod
    def _classify_account(profile: dict) -> AccountType:
        """多信号打分判定账号类型, 无法明确区分时返回 unknown 而非强行归类。"""
        brand_score = 0
        creator_score = 0

        category = (
            profile.get("businessCategoryName")
            or profile.get("category")
            or profile.get("categoryName")
            or ""
        ).lower()
        for kw in CREATOR_CATEGORY_KEYWORDS:
            if kw in category:
                creator_score += 2
        for kw in BRAND_CATEGORY_KEYWORDS:
            if kw in category:
                brand_score += 2

        biography = (profile.get("biography") or profile.get("bio") or "").lower()
        for kw in CREATOR_BIO_KEYWORDS:
            if kw in biography:
                creator_score += 1
        for kw in BRAND_BIO_KEYWORDS:
            if kw in biography:
                brand_score += 1

        external_url = (profile.get("externalUrl") or profile.get("external_url") or "").lower()
        if external_url:
            if any(kw in external_url for kw in BRAND_URL_KEYWORDS):
                brand_score += 2
            elif any(host in external_url for host in ("linktr.ee", "beacons.ai", "bio.site")):
                creator_score += 1

        username = (profile.get("username") or "").lower()
        if any(token in username for token in BRAND_USERNAME_TOKENS):
            brand_score += 1

        if profile.get("isBusinessAccount") or profile.get("is_business_account"):
            brand_score += 1
        if profile.get("isProfessionalAccount") or profile.get("is_professional_account"):
            creator_score += 1

        followers = int(profile.get("followersCount") or profile.get("followers") or 0)
        following = int(
            profile.get("followsCount")
            or profile.get("following")
            or profile.get("follows")
            or 0
        )
        posts_count = int(profile.get("postsCount") or profile.get("posts") or 0)
        if followers >= 5000 and following > 0 and followers / following >= 20:
            brand_score += 1
        if followers >= 1000 and posts_count >= 20 and following > 0 and followers / following <= 5:
            creator_score += 1

        full_name = (profile.get("fullName") or profile.get("full_name") or "").lower()
        if any(token in full_name for token in ("official", "store", "shop", "brand")):
            brand_score += 1

        if brand_score >= 3 and brand_score > creator_score + 1:
            return "brand"
        if creator_score >= 2 and creator_score > brand_score:
            return "influencer"
        if brand_score >= 4:
            return "brand"
        if creator_score >= 4:
            return "influencer"
        return "unknown"

    @staticmethod
    def _normalize_to_country(segment: str) -> Optional[str]:
        import pycountry

        seg = segment.strip().strip(".").lower()
        if not seg:
            return None
        if seg in REGION_TO_COUNTRY:
            return REGION_TO_COUNTRY[seg]
        try:
            country = pycountry.countries.lookup(segment.strip())
            return getattr(country, "common_name", None) or country.name
        except LookupError:
            return None

    @staticmethod
    def _extract_active_countries(profile: dict, sample_posts: list[dict]) -> str:
        from collections import Counter
        import json

        counter: Counter = Counter()

        def count_location(raw: str, weight: int = 1):
            segments = [s for s in str(raw).split(",") if s.strip()]
            for seg in reversed(segments):
                country = InfluencerSourcer._normalize_to_country(seg)
                if country:
                    counter[country] += weight
                    return

        try:
            raw_addr = profile.get("businessAddressJson") or profile.get("business_address_json")
            if raw_addr:
                addr = json.loads(raw_addr) if isinstance(raw_addr, str) else raw_addr
                for field in ("country_code", "city_name"):
                    val = (addr.get(field) or "").strip()
                    if val:
                        count_location(val, weight=2)
                        break
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass

        for post in sample_posts:
            try:
                loc = post.get("locationName") or post.get("location") or ""
                if isinstance(loc, dict):
                    loc = loc.get("name") or ""
                if str(loc).strip():
                    count_location(loc, weight=1)
            except (AttributeError, TypeError):
                continue

        if not counter:
            return "暂无数据"
        top_countries = [c for c, _ in counter.most_common(3)]
        return " / ".join(top_countries)

    @staticmethod
    def _safe_username(user_obj) -> Optional[str]:
        if isinstance(user_obj, dict):
            return (user_obj.get("username") or user_obj.get("user_name") or "").strip() or None
        if isinstance(user_obj, str):
            return user_obj.strip() or None
        return None

    @staticmethod
    def _parse_date(raw_ts: str) -> str:
        if not raw_ts:
            return ""
        try:
            dt = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
            return dt.strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            return str(raw_ts)[:10]

    @staticmethod
    def _calc_engagement_rate(posts: list[dict], followers) -> Optional[float]:
        try:
            followers = int(followers)
            if followers <= 0 or not posts:
                return None
            total_likes = sum(
                int(p.get("likesCount") or p.get("likes") or p.get("likeCount") or 0)
                for p in posts
            )
            total_comments = sum(
                int(p.get("commentsCount") or p.get("comments") or p.get("commentCount") or 0)
                for p in posts
            )
            avg_interaction = (total_likes + total_comments) / len(posts)
            return round(avg_interaction / followers, 4)
        except (ValueError, TypeError, ZeroDivisionError):
            return None


THEME_OPTIONS = {
    "system": "跟随系统",
    "light": "浅色",
    "dark": "深色",
}


def apply_theme_to_page(theme_mode: str) -> None:
    """在父页面 html 上设置 data-theme，供 CSS 与系统偏好联动。"""
    import streamlit.components.v1 as components

    if theme_mode == "system":
        script = "parent.document.documentElement.removeAttribute('data-theme');"
    else:
        script = f"parent.document.documentElement.setAttribute('data-theme', '{theme_mode}');"
    components.html(f"<script>{script}</script>", height=0, width=0)


def apply_layout_to_page(settings_dock_open: bool) -> None:
    """标记设置面板展开/收起状态，供布局 CSS 使用。"""
    import streamlit.components.v1 as components

    flag = "true" if settings_dock_open else "false"
    components.html(
        f'<script>parent.document.documentElement.setAttribute("data-dock-open", "{flag}");</script>',
        height=0,
        width=0,
    )


def init_app_settings_state() -> None:
    if "scale_preset" not in st.session_state:
        st.session_state.scale_preset = "balanced"
    if "theme_mode" not in st.session_state:
        st.session_state.theme_mode = "system"
    if "use_custom_scale" not in st.session_state:
        st.session_state.use_custom_scale = False
    if "posts_limit" not in st.session_state:
        st.session_state.posts_limit = DEFAULT_POSTS_LIMIT
    if "max_profiles" not in st.session_state:
        st.session_state.max_profiles = DEFAULT_MAX_PROFILES
    if "api_token" not in st.session_state:
        st.session_state.api_token = ""


def render_app_settings_form() -> tuple[int, int, str]:
    """左侧设置面板内容。"""
    st.markdown('<p class="dock-section-label">显示主题</p>', unsafe_allow_html=True)
    st.radio(
        "界面主题",
        options=list(THEME_OPTIONS.keys()),
        format_func=lambda x: THEME_OPTIONS[x],
        key="theme_mode",
        help="跟随系统会根据电脑/浏览器的浅色或深色模式自动切换",
    )
    apply_theme_to_page(st.session_state.theme_mode)

    st.markdown('<p class="dock-section-label">抓取规模</p>', unsafe_allow_html=True)
    st.radio(
        "快速预设",
        options=["save", "balanced", "deep"],
        format_func=lambda x: {
            "save": "省额度 · 适合试跑",
            "balanced": "推荐 · 默认平衡",
            "deep": "深度挖掘 · 结果更多",
        }[x],
        key="scale_preset",
        help="Starter 计划建议日常使用「推荐」预设",
    )
    preset_posts, preset_profiles = apply_scale_preset(st.session_state.scale_preset)
    st.toggle("自定义抓取数量", key="use_custom_scale")

    if st.session_state.use_custom_scale:
        st.slider(
            "扫描贴文数量",
            min_value=50,
            max_value=STARTER_MAX_POSTS_LIMIT,
            value=st.session_state.posts_limit,
            step=25,
            key="posts_limit",
        )
        st.slider(
            "主页抓取数量",
            min_value=20,
            max_value=STARTER_MAX_PROFILES,
            value=st.session_state.max_profiles,
            step=5,
            key="max_profiles",
        )
    else:
        st.session_state.posts_limit = preset_posts
        st.session_state.max_profiles = preset_profiles
        st.caption(f"{preset_posts} 条贴文 · 最多 {preset_profiles} 个主页")

    estimated_cost = estimate_run_cost_usd(
        st.session_state.posts_limit,
        st.session_state.max_profiles,
    )
    estimated_runs = estimate_monthly_runs(
        st.session_state.posts_limit,
        st.session_state.max_profiles,
    )
    st.markdown(
        f'<div class="dock-cost-box">本次约 <strong>${estimated_cost:.2f}</strong><br>'
        f"Starter 月额度约可跑 <strong>{estimated_runs}</strong> 次</div>",
        unsafe_allow_html=True,
    )

    st.markdown('<p class="dock-section-label">Apify 凭证</p>', unsafe_allow_html=True)
    st.text_input(
        "API Token",
        type="password",
        placeholder="apify_api_xxxxxxxx",
        help="在 Apify 控制台 → Settings → Integrations 中复制",
        key="api_token",
        label_visibility="collapsed",
    )
    st.caption("Token 仅用于本次运行，不会保存。")
    st.link_button("打开 Apify 控制台", "https://console.apify.com/account/integrations", use_container_width=True)

    return (
        st.session_state.posts_limit,
        st.session_state.max_profiles,
        st.session_state.api_token.strip(),
    )


def render_settings_dock_shell(main_renderer) -> None:
    """左侧吸附设置面板 + 主内容区。"""
    if "settings_dock_open" not in st.session_state:
        st.session_state.settings_dock_open = True

    dock_open = st.session_state.settings_dock_open
    inject_custom_css()
    apply_layout_to_page(dock_open)
    apply_theme_to_page(st.session_state.theme_mode)

    settings = (
        st.session_state.posts_limit,
        st.session_state.max_profiles,
        st.session_state.api_token.strip(),
    )

    if dock_open:
        dock_col, main_col = st.columns([0.27, 0.73], gap="small")
        with dock_col:
            st.markdown('<span class="settings-dock-marker"></span>', unsafe_allow_html=True)
            head_left, head_right = st.columns([5, 1])
            with head_left:
                st.markdown("##### ⚙️ 设置")
            with head_right:
                if st.button("◀", key="dock_close", help="收起设置面板"):
                    st.session_state.settings_dock_open = False
                    st.rerun()
            settings = render_app_settings_form()
        with main_col:
            st.markdown('<span class="main-content-marker"></span>', unsafe_allow_html=True)
            main_renderer(*settings)
    else:
        reopen_col, main_col = st.columns([0.035, 0.965], gap="small")
        with reopen_col:
            st.markdown('<span class="dock-reopen-marker"></span>', unsafe_allow_html=True)
            if st.button("⚙️\n设置", key="dock_reopen", help="展开左侧设置面板", use_container_width=True):
                st.session_state.settings_dock_open = True
                st.rerun()
        with main_col:
            st.markdown('<span class="main-content-marker"></span>', unsafe_allow_html=True)
            main_renderer(*settings)


def inject_custom_css() -> None:
    st.markdown(
        """
        <style>
        :root {
            --app-bg: #f4f6f9;
            --main-bg: #ffffff;
            --sidebar-bg: #f8fafc;
            --surface: #ffffff;
            --surface-muted: #f1f5f9;
            --text-primary: #0f172a;
            --text-secondary: #475569;
            --text-muted: #64748b;
            --border-color: #e2e8f0;
            --border-subtle: #eef2f7;
            --hero-bg: linear-gradient(135deg, #f8fafc 0%, #eef2ff 100%);
            --card-bg: #ffffff;
            --step-accent: #4f46e5;
            --tip-bg: #fff7ed;
            --tip-border: #fed7aa;
            --tip-text: #9a3412;
            --metric-bg: #f8fafc;
            --input-bg: #ffffff;
            --input-text: #0f172a;
            --input-border: #cbd5e1;
            --input-focus: #6366f1;
            --form-bg: #fafbfc;
            --divider: #e2e8f0;
            --link: #4f46e5;
            --shadow: 0 1px 2px rgba(15, 23, 42, 0.06);
        }

        @media (prefers-color-scheme: dark) {
            :root:not([data-theme="light"]) {
                --app-bg: #090b10;
                --main-bg: #0f131a;
                --sidebar-bg: #0c1017;
                --surface: #151b24;
                --surface-muted: #1a2230;
                --text-primary: #edf2f7;
                --text-secondary: #c4d0de;
                --text-muted: #8fa0b3;
                --border-color: #2a3545;
                --border-subtle: #1e2735;
                --hero-bg: linear-gradient(145deg, #121820 0%, #161f2b 50%, #1a2433 100%);
                --card-bg: #151b24;
                --step-accent: #7c89ff;
                --tip-bg: #172033;
                --tip-border: #334155;
                --tip-text: #cbd5e1;
                --metric-bg: #151b24;
                --input-bg: #1a2230;
                --input-text: #edf2f7;
                --input-border: #334155;
                --input-focus: #7c89ff;
                --form-bg: #121820;
                --divider: #2a3545;
                --link: #93a4ff;
                --shadow: 0 2px 8px rgba(0, 0, 0, 0.35);
            }
        }

        :root[data-theme="light"] {
            --app-bg: #f4f6f9;
            --main-bg: #ffffff;
            --sidebar-bg: #f8fafc;
            --surface: #ffffff;
            --surface-muted: #f1f5f9;
            --text-primary: #0f172a;
            --text-secondary: #475569;
            --text-muted: #64748b;
            --border-color: #e2e8f0;
            --border-subtle: #eef2f7;
            --hero-bg: linear-gradient(135deg, #f8fafc 0%, #eef2ff 100%);
            --card-bg: #ffffff;
            --step-accent: #4f46e5;
            --tip-bg: #fff7ed;
            --tip-border: #fed7aa;
            --tip-text: #9a3412;
            --metric-bg: #f8fafc;
            --input-bg: #ffffff;
            --input-text: #0f172a;
            --input-border: #cbd5e1;
            --input-focus: #6366f1;
            --form-bg: #fafbfc;
            --divider: #e2e8f0;
            --link: #4f46e5;
            --shadow: 0 1px 2px rgba(15, 23, 42, 0.06);
        }

        :root[data-theme="dark"] {
            --app-bg: #090b10;
            --main-bg: #0f131a;
            --sidebar-bg: #0c1017;
            --surface: #151b24;
            --surface-muted: #1a2230;
            --text-primary: #edf2f7;
            --text-secondary: #c4d0de;
            --text-muted: #8fa0b3;
            --border-color: #2a3545;
            --border-subtle: #1e2735;
            --hero-bg: linear-gradient(145deg, #121820 0%, #161f2b 50%, #1a2433 100%);
            --card-bg: #151b24;
            --step-accent: #7c89ff;
            --tip-bg: #172033;
            --tip-border: #334155;
            --tip-text: #cbd5e1;
            --metric-bg: #151b24;
            --input-bg: #1a2230;
            --input-text: #edf2f7;
            --input-border: #334155;
            --input-focus: #7c89ff;
            --form-bg: #121820;
            --divider: #2a3545;
            --link: #93a4ff;
            --shadow: 0 2px 8px rgba(0, 0, 0, 0.35);
        }

        .stApp {
            background-color: var(--app-bg);
            color: var(--text-primary);
        }

        section[data-testid="stSidebar"],
        div[data-testid="stSidebar"],
        [data-testid="stSidebarCollapsedControl"],
        [data-testid="collapsedControl"],
        button[data-testid="stSidebarCollapseButton"] {
            display: none !important;
        }

        [data-testid="stAppViewContainer"] .main {
            background-color: var(--main-bg);
        }

        [data-testid="stHeader"] {
            background: transparent;
        }

        div[data-testid="column"]:has(.settings-dock-marker) {
            position: fixed !important;
            left: 0;
            top: 0;
            width: min(320px, 30vw) !important;
            min-width: 280px !important;
            height: 100vh;
            overflow-y: auto;
            overflow-x: hidden;
            padding: 0.75rem 0.85rem 1.25rem !important;
            background: var(--sidebar-bg) !important;
            border-right: 1px solid var(--border-color) !important;
            box-shadow: var(--shadow);
            z-index: 1000;
            flex: none !important;
        }

        div[data-testid="column"]:has(.settings-dock-marker)::-webkit-scrollbar {
            width: 8px;
        }

        div[data-testid="column"]:has(.settings-dock-marker)::-webkit-scrollbar-thumb {
            background: var(--border-color);
            border-radius: 999px;
        }

        html[data-dock-open="true"] div[data-testid="column"]:has(.main-content-marker) {
            margin-left: min(320px, 30vw);
            width: calc(100% - min(320px, 30vw)) !important;
            max-width: none !important;
            flex: 1 1 auto !important;
        }

        html[data-dock-open="false"] div[data-testid="column"]:has(.main-content-marker) {
            margin-left: 58px;
            width: calc(100% - 58px) !important;
            max-width: none !important;
            flex: 1 1 auto !important;
        }

        div[data-testid="column"]:has(.dock-reopen-marker) {
            position: fixed !important;
            left: 0;
            top: 50%;
            transform: translateY(-50%);
            width: 52px !important;
            min-width: 52px !important;
            z-index: 1001;
            padding: 0 !important;
            background: transparent !important;
            border: none !important;
            flex: none !important;
        }

        div[data-testid="column"]:has(.dock-reopen-marker) button {
            min-height: 92px !important;
            padding: 0.35rem 0.2rem !important;
            line-height: 1.25 !important;
            white-space: pre-line !important;
            border-radius: 0 14px 14px 0 !important;
            background: var(--surface) !important;
            color: var(--text-primary) !important;
            border: 1px solid var(--border-color) !important;
            border-left: none !important;
            box-shadow: var(--shadow) !important;
            font-size: 0.82rem !important;
            font-weight: 600 !important;
        }

        div[data-testid="column"]:has(.settings-dock-marker) button[kind="secondary"] {
            min-width: 2rem;
            padding: 0.15rem 0.35rem !important;
            border-radius: 8px !important;
        }

        .dock-section-label {
            font-size: 0.72rem;
            font-weight: 700;
            letter-spacing: 0.06em;
            text-transform: uppercase;
            color: var(--text-muted);
            margin: 0.75rem 0 0.35rem;
        }

        .dock-section-label:first-child {
            margin-top: 0.15rem;
        }

        .dock-cost-box {
            background: var(--surface-muted);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 0.75rem 0.85rem;
            color: var(--text-secondary);
            font-size: 0.88rem;
            line-height: 1.55;
            margin: 0.35rem 0 0.75rem;
        }

        div[data-testid="column"]:has(.settings-dock-marker) div[data-testid="stRadio"] label,
        div[data-testid="column"]:has(.settings-dock-marker) div[data-testid="stToggle"] label,
        div[data-testid="column"]:has(.settings-dock-marker) div[data-testid="stSlider"] label {
            color: var(--text-secondary) !important;
            font-size: 0.9rem !important;
        }

        div[data-testid="column"]:has(.settings-dock-marker) div[data-testid="stTextInput"] input {
            background-color: var(--input-bg) !important;
            color: var(--input-text) !important;
            border: 1px solid var(--input-border) !important;
        }

        div[data-testid="column"]:has(.settings-dock-marker) [data-testid="stLinkButton"] a {
            background-color: var(--surface-muted) !important;
            color: var(--text-primary) !important;
            border: 1px solid var(--border-color) !important;
        }

        .block-container {
            padding-top: 1.5rem;
            padding-bottom: 2rem;
        }

        [data-testid="stMarkdownContainer"] p,
        [data-testid="stMarkdownContainer"] li,
        [data-testid="stMarkdownContainer"] h1,
        [data-testid="stMarkdownContainer"] h2,
        [data-testid="stMarkdownContainer"] h3,
        [data-testid="stMarkdownContainer"] h4,
        [data-testid="stMarkdownContainer"] h5,
        [data-testid="stMarkdownContainer"] h6 {
            color: var(--text-primary);
        }

        [data-testid="stCaptionContainer"] p,
        [data-testid="stCaptionContainer"] small {
            color: var(--text-muted) !important;
        }

        hr {
            border-color: var(--divider) !important;
            opacity: 0.9;
        }

        div[data-testid="stForm"] {
            background: var(--form-bg);
            border: 1px solid var(--border-color);
            border-radius: 14px;
            padding: 1rem 1.1rem 1.2rem;
            box-shadow: var(--shadow);
        }

        div[data-testid="stTextInput"] label,
        div[data-testid="stTextInput"] p,
        div[data-testid="stRadio"] label,
        div[data-testid="stSlider"] label,
        div[data-testid="stToggle"] label {
            color: var(--text-secondary) !important;
        }

        div[data-testid="stTextInput"] input,
        div[data-testid="stTextInput"] textarea {
            background-color: var(--input-bg) !important;
            color: var(--input-text) !important;
            border: 1px solid var(--input-border) !important;
            border-radius: 10px !important;
        }

        div[data-testid="stTextInput"] input:focus,
        div[data-testid="stTextInput"] textarea:focus {
            border-color: var(--input-focus) !important;
            box-shadow: 0 0 0 1px var(--input-focus) !important;
        }

        div[data-testid="stTextInput"] input::placeholder {
            color: var(--text-muted) !important;
            opacity: 0.85;
        }

        div[data-testid="stMetric"] {
            background: var(--metric-bg);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 0.65rem 0.75rem;
            box-shadow: var(--shadow);
        }

        div[data-testid="stMetric"] label {
            color: var(--text-muted) !important;
        }

        div[data-testid="stMetric"] [data-testid="stMetricValue"] {
            color: var(--text-primary) !important;
        }

        div[data-testid="stAlert"] {
            background-color: var(--surface-muted) !important;
            color: var(--text-secondary) !important;
            border: 1px solid var(--border-color) !important;
            border-radius: 12px !important;
        }

        [data-testid="stStatusWidget"] {
            background-color: var(--surface) !important;
            border: 1px solid var(--border-color) !important;
            border-radius: 12px !important;
        }

        [data-testid="stExpander"] details {
            background-color: var(--surface);
            border: 1px solid var(--border-color);
            border-radius: 12px;
        }

        [data-testid="stDataFrame"] {
            border: 1px solid var(--border-color);
            border-radius: 12px;
            overflow: hidden;
            box-shadow: var(--shadow);
        }

        [data-testid="stLinkButton"] a {
            border-radius: 10px !important;
        }

        .hero-box {
            background: var(--hero-bg);
            border: 1px solid var(--border-color);
            border-radius: 16px;
            padding: 1.25rem 1.5rem;
            margin-bottom: 1rem;
            box-shadow: var(--shadow);
        }

        .hero-title {
            font-size: 1.55rem;
            font-weight: 700;
            color: var(--text-primary);
            margin-bottom: 0.35rem;
        }

        .hero-subtitle {
            color: var(--text-secondary);
            font-size: 0.95rem;
            line-height: 1.6;
            margin: 0;
        }

        .step-card {
            background: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 14px;
            padding: 1rem 1.1rem;
            height: 100%;
            box-shadow: var(--shadow);
        }

        .step-number {
            display: inline-block;
            width: 1.6rem;
            height: 1.6rem;
            border-radius: 999px;
            background: var(--step-accent);
            color: #ffffff;
            text-align: center;
            line-height: 1.6rem;
            font-size: 0.85rem;
            font-weight: 700;
            margin-bottom: 0.45rem;
        }

        .step-title {
            font-weight: 600;
            color: var(--text-primary);
            margin-bottom: 0.25rem;
        }

        .step-desc {
            color: var(--text-muted);
            font-size: 0.88rem;
            line-height: 1.5;
            margin: 0;
        }

        .soft-tip {
            background: var(--tip-bg);
            border: 1px solid var(--tip-border);
            border-radius: 12px;
            padding: 0.85rem 1rem;
            color: var(--tip-text);
            font-size: 0.9rem;
            margin-top: 0.5rem;
            line-height: 1.55;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_hero() -> None:
    st.markdown(
        """
        <div class="hero-box">
            <div class="hero-title">Instagram 合作账号发现工具</div>
            <p class="hero-subtitle">
                输入一个 IG 主页链接，告诉工具这是品牌还是红人，它会帮你找出近期合作过的另一类账号，
                并整理成可导出的数据表。
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_getting_started() -> None:
    st.markdown("#### 怎么用？三步就好")
    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown(
            """
            <div class="step-card">
                <div class="step-number">1</div>
                <div class="step-title">粘贴主页链接</div>
                <p class="step-desc">例如 https://www.instagram.com/nike/<br>每次只支持一个账号。</p>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with c2:
        st.markdown(
            """
            <div class="step-card">
                <div class="step-number">2</div>
                <div class="step-title">判断账号类型</div>
                <p class="step-desc">由你手动选择「品牌」或「红人」。工具会反向查找合作对象。</p>
            </div>
            """,
            unsafe_allow_html=True,
        )
    with c3:
        st.markdown(
            """
            <div class="step-card">
                <div class="step-number">3</div>
                <div class="step-title">开始检索并导出</div>
                <p class="step-desc">填写 Apify Token，点击开始。完成后可一键导出 CSV。</p>
            </div>
            """,
            unsafe_allow_html=True,
        )
    st.markdown(
        '<div class="soft-tip">💡 首次使用？建议先用默认抓取规模试跑一次，确认结果符合预期后再调高数量。</div>',
        unsafe_allow_html=True,
    )


def render_run_metrics(run_summary: dict) -> None:
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("扫描贴文", run_summary["posts_scraped"])
    c2.metric("发现合作账号", run_summary["total_candidates"])
    c3.metric("抓取主页", run_summary["profiles_scraped"])
    c4.metric("最终保留", run_summary["final_rows"])
    c5.metric("预估消耗", f"${run_summary['estimated_cost_usd']:.2f}")


def apply_scale_preset(preset: str) -> tuple[int, int]:
    presets = {
        "save": (100, 40),
        "balanced": (DEFAULT_POSTS_LIMIT, DEFAULT_MAX_PROFILES),
        "deep": (STARTER_MAX_POSTS_LIMIT, STARTER_MAX_PROFILES),
    }
    return presets[preset]


def render_main_content(posts_limit: int, max_profiles: int, api_token: str) -> None:
    render_hero()

    st.markdown("#### 开始一次检索")
    with st.form("sourcing_form", clear_on_submit=False):
        instagram_url = st.text_input(
            "Instagram 主页链接",
            placeholder="https://www.instagram.com/nike/",
            help="请粘贴账号主页链接，不支持贴文链接或一次输入多个账号",
        )

        st.markdown("这个账号是？")
        account_type_choice = st.segmented_control(
            "账号类型",
            options=["品牌", "红人"],
            default="品牌",
            help="品牌 → 查找合作红人；红人 → 查找合作品牌",
            label_visibility="collapsed",
        ) or "品牌"
        type_hint_col1, type_hint_col2 = st.columns(2)
        with type_hint_col1:
            st.caption("🏢 **品牌**：官方店铺、公司账号、商业品牌主页")
        with type_hint_col2:
            st.caption("👤 **红人**：博主、创作者、KOL、个人 IP 账号")

        start_button = st.form_submit_button("开始检索", type="primary", use_container_width=True)

    if st.session_state.result_df is None and not start_button:
        render_getting_started()

    if start_button:
        if not instagram_url.strip():
            st.error("请先输入 Instagram 主页链接。")
            st.stop()
        if not api_token.strip():
            st.error("请先在左侧 ⚙️ 设置 面板中输入 Apify API Token。")
            st.stop()

        try:
            parsed_handle = parse_instagram_url(instagram_url)
        except ValueError as e:
            st.error(str(e))
            st.stop()

        input_account_type: AccountType = "brand" if account_type_choice == "品牌" else "influencer"
        sourcer = InfluencerSourcer(
            api_token=api_token.strip(),
            instagram_url=instagram_url,
            posts_limit=posts_limit,
            max_profiles=max_profiles,
        )

        with st.status("正在检索，请稍候…", expanded=True) as status:
            st.write(f"目标账号：@{parsed_handle}")
            st.write(f"账号类型：{account_type_choice}")
            st.write(f"抓取配置：{posts_limit} 条贴文 / 最多 {max_profiles} 个主页")

            try:
                st.write("① 正在抓取近期贴文…")
                posts = sourcer.scrape_brand_posts()
                st.write(f"   已获取 {len(posts)} 条贴文")

                st.write("② 正在识别合作账号…")
                influencer_dates = sourcer.extract_influencers(posts)
                if not influencer_dates:
                    status.update(label="未找到合作账号", state="error")
                    st.warning(
                        "最近的贴文里没有发现联合创作者或被标记用户。"
                        "这个账号可能较少使用 Collab 或 @ 标记功能，建议换一个账号试试。"
                    )
                    st.stop()

                total_candidates = len(influencer_dates)
                profiles_to_scrape = min(total_candidates, max_profiles)
                st.write(f"   发现 {total_candidates} 个合作账号，将抓取最近合作的 {profiles_to_scrape} 个主页")

                st.write("③ 正在补充主页数据并筛选…")

                def update_progress(ratio: float, text: str):
                    st.write(f"   {text}")

                records, filtered_out, _, run_stats = sourcer.enrich_influencers(
                    influencer_dates,
                    input_account_type=input_account_type,
                    progress_callback=update_progress,
                )

                if input_account_type == "brand":
                    input_label, target_label, filtered_label = "品牌", "红人", "品牌"
                else:
                    input_label, target_label, filtered_label = "红人", "品牌", "红人"

                if not records:
                    status.update(label="没有符合条件的结果", state="error")
                    st.warning(
                        f"已抓取数据，但没有找到符合条件的「{target_label}」。"
                        f"可以尝试提高抓取数量，或确认输入账号类型是否选对。"
                    )
                    st.stop()

                st.session_state.result_df = InfluencerSourcer.build_dataframe(records)
                st.session_state.input_type = input_account_type
                st.session_state.input_handle = parsed_handle
                st.session_state.target_label = target_label
                st.session_state.run_summary = {
                    "posts_scraped": len(posts),
                    "total_candidates": run_stats["total_candidates"],
                    "profiles_scraped": run_stats["profiles_scraped"],
                    "filtered_out": len(filtered_out),
                    "final_rows": len(records),
                    "estimated_cost_usd": estimate_run_cost_usd(posts_limit, profiles_to_scrape),
                    "input_label": input_label,
                    "target_label": target_label,
                    "filtered_label": filtered_label,
                    "filtered_out_list": filtered_out,
                }
                status.update(
                    label=f"完成！共找到 {len(records)} 个「{target_label}」",
                    state="complete",
                )

            except RuntimeError as e:
                status.update(label="检索失败", state="error")
                st.error(f"任务失败：{e}")
                st.stop()
            except Exception as e:
                status.update(label="发生未知错误", state="error")
                st.error(f"发生未知错误：{e}")
                st.stop()

    if st.session_state.result_df is not None:
        df = st.session_state.result_df
        run_summary = st.session_state.get("run_summary", {})
        target_label = st.session_state.get("target_label", "账号")
        input_handle = st.session_state.get("input_handle", "")

        st.divider()
        st.markdown(f"### 检索结果：@{input_handle}")
        st.caption(
            f"输入账号类型：**{run_summary.get('input_label', '未知')}** → "
            f"本次查找：**{target_label}**"
        )

        if run_summary:
            render_run_metrics(run_summary)
            if run_summary.get("filtered_out", 0) > 0:
                with st.expander(
                    f"已自动剔除 {run_summary['filtered_out']} 个同类账号（点击展开）",
                    expanded=False,
                ):
                    filtered_list = run_summary.get("filtered_out_list", [])
                    if filtered_list:
                        st.write(", ".join(f"@{u}" for u in filtered_list))

        st.markdown("#### 数据总表")
        st.dataframe(
            df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "账号主页链接": st.column_config.LinkColumn("主页链接"),
                "账号类型": st.column_config.TextColumn("类型"),
                "粉丝数": st.column_config.NumberColumn("粉丝数", format="%d"),
                "互动率评估": st.column_config.TextColumn("互动率"),
                "活跃国家 (Top 3)": st.column_config.TextColumn("活跃国家"),
                "最近合作日期": st.column_config.TextColumn("最近合作"),
            },
        )

        csv_bytes = df.to_csv(index=False).encode("utf-8-sig")
        col1, col2 = st.columns([1, 2])
        with col1:
            st.download_button(
                label="导出 CSV",
                data=csv_bytes,
                file_name=f"ig_sourcing_{input_handle}_{datetime.now().strftime('%Y%m%d_%H%M')}.csv",
                mime="text/csv",
                type="primary",
                use_container_width=True,
            )
        with col2:
            st.caption("导出文件使用 UTF-8 编码，Excel 可直接打开，中文不会乱码。")


def main():
    st.set_page_config(
        page_title="IG Sourcing",
        page_icon=".streamlit/favicon.png",
        layout="wide",
        initial_sidebar_state="collapsed",
    )

    if "result_df" not in st.session_state:
        st.session_state.result_df = None
    init_app_settings_state()
    render_settings_dock_shell(render_main_content)


if __name__ == "__main__":
    main()
