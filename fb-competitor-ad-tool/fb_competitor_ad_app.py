"""
FB 竞品广告全自动监控、筛选与 AI 拆解系统
====================================
工作流：Page Name -> Apify 抓取广告库 -> 按 startDate 升序筛选 Top3 最久投放视频
       -> 静默下载 mp4 -> Gemini 1.5 Pro 异步上传+轮询+深度拆解 -> 渲染结果 -> 清理临时文件
"""

import os
import time
import uuid
import traceback

import requests
import streamlit as st
from apify_client import ApifyClient
import google.generativeai as genai

# ------------------------------------------------------------------
# 全局配置
# ------------------------------------------------------------------
TEMP_DIR = "temp_videos"
FB_ADS_ACTOR_ID = "apify/facebook-ads-scraper"
TOP_N = 3
POLL_INTERVAL_SEC = 3

os.makedirs(TEMP_DIR, exist_ok=True)

st.set_page_config(page_title="FB 竞品爆款广告拆解系统", page_icon="🎬", layout="wide")


def _secret(section: str, key: str, default: str = "") -> str:
    """从 .streamlit/secrets.toml 读取可选配置。"""
    try:
        val = st.secrets[section][key]
        return str(val).strip() if val else default
    except Exception:
        return default


# ------------------------------------------------------------------
# 模块二：Apify 抓取与“持续投放时长”筛选算法
# ------------------------------------------------------------------
def fetch_fb_ads(apify_token: str, page_name: str) -> list:
    """调用 Apify FB Ads Scraper，返回原始广告 item 列表。"""
    client = ApifyClient(apify_token)

    run_input = {
        "startUrls": [
            {
                "url": (
                    "https://www.facebook.com/ads/library/?active_status=all"
                    "&ad_type=all&country=ALL&q="
                    f"{page_name}&search_type=keyword_unordered"
                )
            }
        ],
        "searchTerms": [page_name],
        "maxItems": 60,
        "isDetailsPerAd": True,
    }

    run = client.actor(FB_ADS_ACTOR_ID).call(run_input=run_input)
    dataset_id = run["defaultDatasetId"]

    items = list(client.dataset(dataset_id).iterate_items())
    return items


def _extract_video_url(item: dict):
    """尽量兼容不同 Actor 返回结构里的视频字段。"""
    candidates = [
        item.get("videoUrl"),
        item.get("video_url"),
        item.get("videoHdUrl"),
        item.get("videoSdUrl"),
    ]
    snapshot = item.get("snapshot") or {}
    if isinstance(snapshot, dict):
        candidates.append(snapshot.get("videoUrl"))
        videos = snapshot.get("videos")
        if isinstance(videos, list) and videos:
            first = videos[0]
            if isinstance(first, dict):
                candidates.append(first.get("videoHdUrl") or first.get("videoSdUrl"))

    cards = item.get("cards")
    if isinstance(cards, list):
        for card in cards:
            if isinstance(card, dict) and card.get("videoUrl"):
                candidates.append(card.get("videoUrl"))

    for c in candidates:
        if c:
            return c
    return None


def _extract_start_date(item: dict):
    """兼容不同字段名的开始投放日期。"""
    for key in ("startDate", "start_date", "adDeliveryStartTime", "startDateFormatted"):
        val = item.get(key)
        if val:
            return val
    snapshot = item.get("snapshot") or {}
    if isinstance(snapshot, dict):
        return snapshot.get("startDate") or snapshot.get("start_date")
    return None


def filter_top_longest_running_videos(items: list, top_n: int = TOP_N) -> list:
    """
    核心筛选逻辑（平替 Impression / Spend）：
    1. 过滤出含视频链接的广告
    2. 按 startDate 升序排列（越早开始投放 => 跑得越久 => 越是核心爆款）
    3. 截取最前面的 top_n 条
    """
    video_ads = []
    for item in items:
        video_url = _extract_video_url(item)
        start_date = _extract_start_date(item)
        if video_url and start_date:
            video_ads.append(
                {
                    "video_url": video_url,
                    "start_date": start_date,
                    "raw": item,
                }
            )

    if not video_ads:
        return []

    def _sort_key(ad):
        try:
            return str(ad["start_date"])
        except Exception:
            return ""

    video_ads.sort(key=_sort_key)
    return video_ads[:top_n]


# ------------------------------------------------------------------
# 模块三：静默下载视频
# ------------------------------------------------------------------
def download_video(video_url: str, index: int) -> str:
    local_path = os.path.join(TEMP_DIR, f"temp_video_{index}_{uuid.uuid4().hex[:6]}.mp4")
    with requests.get(video_url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(local_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
    return local_path


# ------------------------------------------------------------------
# 模块四：Gemini 批量深度拆解
# ------------------------------------------------------------------
ANALYSIS_PROMPT = """这是一条在 FB 上跑了很久的现象级爆款广告。请从顶级导演的视角，拆解出它的：

1.【黄金前3秒画面与台词（The Hook）】
2.【痛点引入与卖点逻辑】
3.【视觉与情绪节奏】
4. 最终输出一份【可供立刻拍摄的逐秒分镜脚本表格】，包含：秒数、画面要求、画外音台词三列。

请用中文输出，结构清晰，使用 Markdown 标题和表格。"""


def upload_and_wait_active(local_video_path: str):
    """上传视频到 Gemini 云端，并轮询状态直到 ACTIVE。"""
    video_file = genai.upload_file(path=local_video_path)

    while True:
        file_state = video_file.state.name
        if file_state == "ACTIVE":
            break
        elif file_state == "PROCESSING":
            time.sleep(POLL_INTERVAL_SEC)
            video_file = genai.get_file(video_file.name)
        elif file_state == "FAILED":
            raise RuntimeError(f"Gemini 视频处理失败：{video_file.name}")
        else:
            time.sleep(POLL_INTERVAL_SEC)
            video_file = genai.get_file(video_file.name)

    return video_file


def analyze_video_with_gemini(local_video_path: str) -> str:
    """完整流程：上传 -> 轮询 ACTIVE -> 生成拆解报告 -> 清理云端文件。"""
    model = genai.GenerativeModel("gemini-1.5-pro")
    video_file = None
    try:
        video_file = upload_and_wait_active(local_video_path)
        response = model.generate_content(
            [video_file, ANALYSIS_PROMPT],
            request_options={"timeout": 600},
        )
        return response.text
    finally:
        if video_file is not None:
            try:
                genai.delete_file(video_file.name)
            except Exception:
                pass


# ------------------------------------------------------------------
# 侧边栏 — 模块一
# ------------------------------------------------------------------
with st.sidebar:
    st.header("🔑 全局配置")
    apify_token = st.text_input(
        "Apify API Token",
        type="password",
        help="也可在 .streamlit/secrets.toml 中预填 [apify] token",
    )
    gemini_api_key = st.text_input(
        "Google Gemini API Key",
        type="password",
        help="也可在 .streamlit/secrets.toml 中预填 [gemini] api_key",
    )
    st.markdown("---")
    st.caption("提示：密钥仅在本次会话中使用；侧边栏留空时会尝试读取 secrets.toml。")

st.title("🎬 FB 竞品爆款广告监控 & AI 拆解系统")
st.caption("Page Name → Apify 抓取 → 投放最久 Top3 视频 → Gemini 1.5 Pro 深度拆解 → 分镜脚本")

page_name = st.text_input("Facebook Page Name", placeholder="例如：Nuage Wear")

run_button = st.button("🚀 一键获取并拆解 Top 3 爆款", type="primary", use_container_width=True)


# ------------------------------------------------------------------
# 主流程
# ------------------------------------------------------------------
if run_button:
    apify_token = apify_token.strip() or _secret("apify", "token")
    gemini_api_key = gemini_api_key.strip() or _secret("gemini", "api_key")

    if not apify_token or not gemini_api_key:
        st.error("请先在侧边栏填写 Apify API Token 和 Google Gemini API Key。")
        st.stop()
    if not page_name.strip():
        st.error("请输入要监控的竞品 Facebook Page Name。")
        st.stop()

    genai.configure(api_key=gemini_api_key)

    downloaded_paths = []

    try:
        with st.spinner("正在通过 Apify 抓取 FB 广告库数据..."):
            try:
                raw_items = fetch_fb_ads(apify_token, page_name.strip())
            except Exception as e:
                st.error(f"❌ Apify 抓取失败，请检查 Token 或网络：{e}")
                st.stop()

        if not raw_items:
            st.warning(f"⚠️ 未抓取到 “{page_name}” 的任何广告数据，请确认 Page Name 是否正确。")
            st.stop()

        top_ads = filter_top_longest_running_videos(raw_items, TOP_N)

        if not top_ads:
            st.warning("⚠️ 抓取到了广告数据，但其中没有包含视频素材的广告，无法进行拆解。")
            st.stop()

        st.success(f"✅ 成功筛选出 {len(top_ads)} 条投放最久的视频广告，开始处理...")

        results = []
        progress = st.progress(0.0, text="准备开始...")

        for idx, ad in enumerate(top_ads, start=1):
            video_url = ad["video_url"]
            start_date = ad["start_date"]

            try:
                progress.progress((idx - 1) / len(top_ads), text=f"正在下载第 {idx} 条视频...")
                local_path = download_video(video_url, idx)
                downloaded_paths.append(local_path)
            except Exception as e:
                results.append(
                    {
                        "index": idx,
                        "start_date": start_date,
                        "video_url": video_url,
                        "local_path": None,
                        "report": None,
                        "error": f"视频下载失败：{e}",
                    }
                )
                continue

            try:
                progress.progress(
                    (idx - 0.5) / len(top_ads), text=f"正在用 Gemini 深度拆解第 {idx} 条视频..."
                )
                report = analyze_video_with_gemini(local_path)
                results.append(
                    {
                        "index": idx,
                        "start_date": start_date,
                        "video_url": video_url,
                        "local_path": local_path,
                        "report": report,
                        "error": None,
                    }
                )
            except Exception as e:
                results.append(
                    {
                        "index": idx,
                        "start_date": start_date,
                        "video_url": video_url,
                        "local_path": local_path,
                        "report": None,
                        "error": f"Gemini 分析失败：{e}",
                    }
                )

        progress.progress(1.0, text="全部处理完成！")

        st.subheader("📊 拆解结果")
        tabs = st.tabs([f"爆款 #{r['index']}（{r['start_date']}）" for r in results])

        for tab, r in zip(tabs, results):
            with tab:
                if r["local_path"] and os.path.exists(r["local_path"]):
                    st.video(r["local_path"])
                st.caption(f"开始投放日期：{r['start_date']}　|　原始视频链接：{r['video_url']}")

                if r["error"]:
                    st.error(f"❌ {r['error']}")
                else:
                    with st.expander("📋 查看 AI 拆解报告 / 分镜脚本", expanded=True):
                        st.markdown(r["report"])

    except Exception as e:
        st.error(f"❌ 程序运行中发生未预期错误：{e}")
        st.code(traceback.format_exc())

    finally:
        for path in downloaded_paths:
            try:
                if path and os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass
