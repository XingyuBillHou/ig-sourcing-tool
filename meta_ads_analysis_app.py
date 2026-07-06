# -*- coding: utf-8 -*-
"""
Meta 广告账户 API 拉数 + AI 分析
================================
通过 Meta Marketing API 拉取多账户 Insights，复用投放分析工具的时间维度
（单日 / 区间 / Week 周汇总 / 周末三日），生成 Gemini AI 分析报告。
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from io import BytesIO

import pandas as pd
import streamlit as st

import ad_analysis_app as ad
from meta_ads_fetcher import (
    check_meta_connectivity,
    fetch_meta_sheets,
    format_accounts_for_textarea,
    get_access_token,
    get_default_fetch_range,
    parse_account_configs,
    parse_accounts_from_secrets,
)
from suite_shared import (
    SUITE_GEMINI_API_KEY,
    SUITE_SMTP_PASSWORD,
    SUITE_SMTP_USER,
    get_gemini_api_key,
    secret,
)

os.environ.setdefault("PYTHONUTF8", "1")

APP_TITLE = "Meta 广告投放 AI 分析"
META_ACCESS_TOKEN_KEY = "meta_access_token"
META_ACCOUNTS_TEXT_KEY = "meta_accounts_text"
PAGE_ICON = ad.APP_DIR / "page_icon.png"

META_REPORT_SECTIONS = [
    {
        "title": "## 一、账户总览（Executive Summary）",
        "instruction": (
            "请**只写第一章**「账户总览（Executive Summary）」。\n"
            "- 汇总所有 Meta 广告账户的**总消耗、总出单、整体 ROAS**（必须引用具体数字）\n"
            "- 指出本周期表现最好 / 最差的 1~2 个账户及原因\n"
            "- 3~5 条要点，评估 Meta 渠道整体效率\n"
            "**禁止**写第二、三、四章。"
        ),
        "min_chars": 180,
    },
    {
        "id": "comparison",
        "title": "## 二、环比对比（Period-over-Period）",
        "instruction": (
            "环比表格已由系统自动生成（见下方）。请**只写**「### 2.2 环比洞察与诊断」小节：\n"
            "- 指出消耗/ROAS/订单变化最大的 3 个账户，必须引用表格中的**具体数字与涨跌幅度**\n"
            "- 判断 Meta 整体环比变好、变差或账户间分化\n"
            "- 2~3 条可能原因假设（基于数据，勿编造）\n"
            "**禁止**重复输出表格；**禁止**写其他章节。"
        ),
        "min_chars": 160,
        "skip_title": True,
    },
    {
        "id": "channel",
        "title": "## 三、账户表现拆解",
        "instruction": (
            "各账户数据表格已由系统自动生成（见下方），你**不要重复输出表格**。\n"
            "请**只写**「### 3.3 账户洞察与诊断」小节，包含：\n"
            "- 3~5 条 bullet：**增长引擎账户**（账户名 + 具体 ROAS/消耗数字 + 原因）\n"
            "- 3~5 条 bullet：**拖后腿账户**（账户名 + 具体问题数字 + 风险）\n"
            "- 2~3 条跨账户对比结论（如 CPM/CPC/CTR 差异）\n"
            "**禁止**写第一、二、四章；**禁止**重新输出 Markdown 表格。"
        ),
        "min_chars": 200,
        "skip_title": True,
    },
    {
        "title": "## 四、下一步行动指令（Action Items）",
        "instruction": (
            "请**只写第四章**「下一步行动指令（Action Items）」。\n"
            "- 至少 **5 条** numbered list，每条格式：\n"
            "  **【账户名】** 动作描述（含量化幅度，如 +20% / -500 USD / 暂停低效广告组）\n"
            "- 覆盖：加预算、减预算、暂停、放量、素材/受众测试 等类型\n"
            "- 可结合第二章环比结论给出针对性动作\n"
            "**禁止**写第一、二、三章。"
        ),
        "min_chars": 280,
    },
]


def build_meta_system_prompt(report_type: str, date_mode: str) -> str:
    if date_mode == "single":
        time_note = (
            f"这是一份【{report_type}】，数据为**单日**快照，"
            f"请重点关注各 Meta 广告账户当日的消耗、ROAS、出单与效率指标。"
        )
    elif date_mode == "week":
        time_note = (
            f"这是一份【{report_type}】，数据来自 **Week 周汇总行**（整周合并，非逐日明细）。"
            f"请按整周口径解读各账户表现，并与相邻周对比。"
        )
    elif date_mode == "weekend":
        time_note = (
            f"这是一份【{report_type}】，数据为**周末三日汇总**（周五+周六+周日合并）。"
            f"请按整个周末区间评估各账户表现。"
        )
    elif report_type == "周报":
        time_note = (
            f"这是一份【周报】，数据为所选**日期范围**内的逐日表现。"
            f"请汇总该区间各账户趋势并给出下周建议。"
        )
    elif report_type == "月报":
        time_note = (
            f"这是一份【月报】，数据为所选**月份区间**内的表现。"
            f"请从月度维度分析各账户体量、趋势与结构性变化。"
        )
    else:
        time_note = (
            f"这是一份【{report_type}】，数据为**日期范围内的时间序列**。"
            f"请分析各账户消耗、ROAS、出单等核心指标的变化趋势。"
        )

    return f"""# 角色设定
你是一位精通 Meta（Facebook / Instagram）效果广告的资深投放总监，擅长多广告账户数据对比、预算分配与 ROAS 优化，风格数据驱动、结论明确。

# 当前任务
请基于我提供的【Meta Marketing API 原始 Insights 数据】，生成一份【{report_type}】。
{time_note}

# 数据说明
- 每个 Sheet 对应一个 Meta 广告账户（如 WearNuage、Nuage Bra 等）。
- 每条记录含：日期、消耗(Spend)、曝光、点击、CPM、CPC、CTR、出单量、Purchase Value、ROAS 等。
- 数据中可能包含 **Week N** 周汇总行与 **周末三日** 汇总行，请勿与逐日明细重复相加。
- 若某账户在该区间无数据，会文字注明，不要编造。

# 输出结构要求（必须严格按以下 Markdown 结构输出，使用简体中文）

## 一、账户总览（Executive Summary）
- 汇总所有账户消耗与 ROAS，评估 Meta 渠道整体效率。

## 二、环比对比（Period-over-Period）
- 与上一周期对比；环比表格由系统生成，你需撰写环比洞察。

## 三、账户表现拆解
- 跨账户对比消耗、ROAS、出单；表格由系统生成，你需补充 3.3 洞察。

## 四、下一步行动指令（Action Items）
- 给出具体、可执行的账户级预算与优化建议。

# 硬性要求
- **必须完整输出四个章节**；所有结论必须引用**具体数字**。
- 第四章至少 **5 条** 行动建议，每条含账户名 + 量化幅度。
- 报告总篇幅不少于 **800 字**；使用简体中文。
"""


def build_meta_followup_system_prompt() -> str:
    return """# 角色
你是 Meta 广告投放数据分析助手。用户已基于 Meta API 拉取的数据收到一份 AI 分析报告，现在追问具体问题。

# 要求
- 必须结合提供的**数据摘要**与**已生成报告**作答，引用具体数字（消耗、ROAS、出单、CPM 等）
- 回答简洁、可执行，使用简体中文
- 数据不足时明确说明；不要编造未提供的数据
"""


def build_meta_full_markdown(report: str, meta: dict) -> str:
    generated_at = meta.get("generated_at") or datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"""# Meta 广告投放分析报告

| 项目 | 内容 |
|------|------|
| 报告类型 | {meta.get('report_type', '')} |
| 数据区间 | {meta.get('date_info', '')} |
| 生成时间 | {generated_at} |

---

{report}
"""


def _init_session_state() -> None:
    if META_ACCESS_TOKEN_KEY not in st.session_state:
        st.session_state[META_ACCESS_TOKEN_KEY] = secret("meta", "access_token")
    if META_ACCOUNTS_TEXT_KEY not in st.session_state:
        secret_accounts = parse_accounts_from_secrets()
        st.session_state[META_ACCOUNTS_TEXT_KEY] = format_accounts_for_textarea(secret_accounts)
    if SUITE_GEMINI_API_KEY not in st.session_state:
        st.session_state[SUITE_GEMINI_API_KEY] = secret("gemini", "api_key")
    if SUITE_SMTP_USER not in st.session_state:
        st.session_state[SUITE_SMTP_USER] = secret("email", "smtp_user")
    if SUITE_SMTP_PASSWORD not in st.session_state:
        st.session_state[SUITE_SMTP_PASSWORD] = secret("email", "smtp_password")


def _render_sidebar() -> None:
    _init_session_state()
    with st.sidebar:
        st.header("🔑 Meta API")
        st.text_input(
            "Access Token",
            type="password",
            key=META_ACCESS_TOKEN_KEY,
            help="Meta Marketing API 长期 Token，需 ads_read 权限。",
        )
        if secret("meta", "access_token"):
            st.caption("✅ Secrets 中已配置 Token")
        elif (st.session_state.get(META_ACCESS_TOKEN_KEY) or "").strip():
            st.caption("✅ 使用侧边栏填写的 Token")

        st.text_area(
            "广告账户列表",
            key=META_ACCOUNTS_TEXT_KEY,
            height=120,
            placeholder="WearNuage:act_123456789\nNuage Bra:act_987654321",
            help="每行一个账户，格式：显示名:act_账户ID",
        )

        if st.button("🔌 测试 Meta 连接", use_container_width=True, key="test_meta_api"):
            ok, msg = check_meta_connectivity(get_access_token())
            if ok:
                st.success(msg)
            else:
                st.error(msg)

        st.markdown("---")
        st.header("⚙️ Gemini")
        st.text_input(
            "API Key",
            type="password",
            key=SUITE_GEMINI_API_KEY,
            help="从 https://aistudio.google.com/apikey 获取",
        )
        model_name = st.selectbox(
            "模型",
            options=ad.GEMINI_MODELS,
            index=ad.GEMINI_MODELS.index(ad.GEMINI_DEFAULT_MODEL),
        )
        st.session_state["meta_analysis_model"] = model_name
        st.session_state["meta_analysis_temperature"] = st.slider(
            "Temperature（创意度）", 0.0, 1.0, 0.5, 0.1,
        )

        st.markdown("---")
        st.header("🎨 显示主题")
        theme_label = st.radio(
            "界面模式",
            options=list(ad.THEME_MODE_LABELS.values()),
            index=0,
            horizontal=True,
        )
        st.session_state["meta_theme_mode"] = next(
            k for k, v in ad.THEME_MODE_LABELS.items() if v == theme_label
        )


def _render_fetch_section() -> dict | None:
    default_since, default_until = get_default_fetch_range(120)
    col1, col2, col3 = st.columns([1, 1, 1])
    with col1:
        since = st.date_input("拉数起始日", value=default_since, key="meta_fetch_since")
    with col2:
        until = st.date_input("拉数结束日", value=default_until, key="meta_fetch_until")
    with col3:
        st.write("")
        st.write("")
        fetch_btn = st.button("📡 拉取 Meta 数据", type="primary", use_container_width=True)

    if fetch_btn:
        token = get_access_token()
        accounts = parse_account_configs(st.session_state.get(META_ACCOUNTS_TEXT_KEY, ""))
        if not token:
            st.error("❌ 请填写 Meta Access Token。")
            return None
        if not accounts:
            st.error("❌ 请至少配置一个广告账户（格式：名称:act_123456789）。")
            return None
        if since > until:
            st.warning("⚠️ 起始日晚于结束日，已自动交换。")
            since, until = until, since

        with st.spinner(f"正在拉取 {len(accounts)} 个账户的 Insights（{since} ~ {until}）..."):
            sheets, errors = fetch_meta_sheets(token, accounts, since, until)

        fetch_key = (token[:8], tuple((a["id"], a["name"]) for a in accounts), since, until)
        st.session_state["meta_fetch_key"] = fetch_key
        st.session_state["meta_sheets"] = sheets
        st.session_state["meta_fetch_errors"] = errors
        st.session_state.pop("meta_report", None)
        st.session_state.pop("meta_report_extracted_data", None)
        st.session_state.pop("meta_followup_messages", None)

        if sheets:
            st.success(f"✅ 成功拉取 {len(sheets)} 个账户：{', '.join(sheets.keys())}")
        if errors:
            for err in errors:
                st.warning(f"⚠️ {err}")
        if not sheets:
            st.error("❌ 未获取到任何有效数据，请检查 Token 权限与账户 ID。")
            return None

    sheets = st.session_state.get("meta_sheets")
    if not sheets:
        st.info(
            "请在上方配置 Meta Access Token 与广告账户，选择日期范围后点击「拉取 Meta 数据」。\n\n"
            "拉取后将自动附带 **Week 周汇总** 与 **周末三日** 行，与分析工具 Excel 格式一致。"
        )
        return None
    return sheets


def _render_data_preview(sheets: dict) -> None:
    with st.expander("🔍 查看各账户数据预览（前 5 行）", expanded=False):
        for name, df in sheets.items():
            st.markdown(f"**{name}**（共 {len(df)} 行）")
            preview_cols = [c for c in df.columns if not str(c).startswith("_")]
            st.dataframe(df[preview_cols].head(5), use_container_width=True)


def _render_report_controls(sheets: dict) -> None:
    st.markdown("---")
    st.subheader("📅 报告参数设置")

    col_a, col_b = st.columns([1, 2])
    with col_a:
        report_type = st.selectbox("选择报告维度", ["日报", "周报", "月报"], key="meta_report_type")

    valid_dates = ad.get_valid_dates(sheets)
    weekend_buckets = ad.get_weekend_buckets(sheets)
    week_buckets = ad.resolve_week_buckets_for_ui(sheets, valid_dates)

    if not valid_dates and not weekend_buckets and not week_buckets:
        st.error("❌ 无法从数据中提取有效日期、Week 或周末汇总，请扩大拉数区间后重试。")
        return

    date_mode_options, default_date_mode_label, date_mode_help = (
        ad.get_date_mode_options_for_report(report_type, weekend_buckets, week_buckets)
    )

    if "meta_date_mode_label" not in st.session_state:
        st.session_state["meta_date_mode_label"] = default_date_mode_label
    if st.session_state.get("_meta_prev_report_type") != report_type:
        st.session_state["_meta_prev_report_type"] = report_type
        st.session_state["meta_date_mode_label"] = default_date_mode_label
    if st.session_state.get("meta_date_mode_label") not in date_mode_options:
        st.session_state["meta_date_mode_label"] = default_date_mode_label

    selected_date = None
    start_date = None
    end_date = None
    weekend_bucket = None
    week_bucket = None
    date_info = ""
    date_mode = "single"

    with col_b:
        st.caption(date_mode_help)
        date_mode_label = st.radio(
            "数据范围",
            date_mode_options,
            horizontal=True,
            key="meta_date_mode_label",
            help=date_mode_help,
        )
        date_mode_map = {
            "单一日期": "single",
            "日期范围": "range",
            "Week 周数据": "week",
            "周末三日": "weekend",
        }
        date_mode = date_mode_map[date_mode_label]

        if date_mode == "single":
            if valid_dates:
                selected_date = st.selectbox(
                    "选择日期",
                    options=valid_dates,
                    index=len(valid_dates) - 1,
                    format_func=lambda d: d.strftime("%Y-%m-%d (%a)"),
                    key="meta_selected_date",
                )
                start_date = end_date = selected_date
                date_info = selected_date.strftime("%Y-%m-%d")
            else:
                st.warning("⚠️ 未找到标准日期。")
        elif date_mode == "range":
            if valid_dates:
                min_d, max_d = min(valid_dates), max(valid_dates)
                def_start, def_end = ad.get_default_range_for_report(report_type, valid_dates)
                c1, c2 = st.columns(2)
                with c1:
                    start_date = st.date_input(
                        "开始日期", value=def_start, min_value=min_d, max_value=max_d,
                        key=f"meta_range_start_{report_type}",
                    )
                with c2:
                    end_date = st.date_input(
                        "结束日期", value=def_end, min_value=min_d, max_value=max_d,
                        key=f"meta_range_end_{report_type}",
                    )
                if start_date > end_date:
                    start_date, end_date = end_date, start_date
                date_info = (
                    start_date.strftime("%Y-%m-%d")
                    if start_date == end_date
                    else f"{start_date.strftime('%Y-%m-%d')} 至 {end_date.strftime('%Y-%m-%d')}"
                )
        elif date_mode == "week":
            week_bucket = st.selectbox(
                "选择 Week 周汇总",
                options=week_buckets,
                index=len(week_buckets) - 1,
                format_func=ad.format_week_bucket_label,
                key="meta_week_bucket",
            )
            date_info = ad.format_week_bucket_label(week_bucket)
        elif date_mode == "weekend":
            weekend_bucket = st.selectbox(
                "选择周末三日",
                options=weekend_buckets,
                index=len(weekend_buckets) - 1,
                format_func=lambda b: (
                    f"周末三日 ({b['start'].strftime('%Y-%m-%d')} ~ "
                    f"{b['end'].strftime('%Y-%m-%d')})"
                ),
                key="meta_weekend_bucket",
            )
            start_date = weekend_bucket["start"]
            end_date = weekend_bucket["end"]
            date_info = (
                f"周末三日 ({weekend_bucket['start'].strftime('%Y-%m-%d')} ~ "
                f"{weekend_bucket['end'].strftime('%Y-%m-%d')})"
            )

    if valid_dates:
        st.caption(
            f"📌 可用日期：{min(valid_dates)} ~ {max(valid_dates)}，共 {len(valid_dates)} 天"
        )
    if week_buckets:
        recent = ", ".join(ad.format_week_bucket_label(b) for b in week_buckets[-4:])
        st.caption(
            f"📌 可用 Week 周区间 {len(week_buckets)} 个（由逐日数据生成，含日期范围）；"
            f"最近：{recent}"
        )
    if weekend_buckets:
        st.caption(f"📌 检测到 {len(weekend_buckets)} 个周末三日汇总段")

    st.markdown("---")
    if st.button("🚀 生成 AI 分析报告", type="primary", key="meta_generate_report"):
        api_key = get_gemini_api_key()
        if not api_key:
            st.error("❌ 请先在侧边栏填写 Gemini API Key。")
            return

        try:
            with st.spinner("正在提取各账户数据..."):
                extracted_data = ad.extract_data_for_report(
                    sheets_dict=sheets,
                    date_mode=date_mode,
                    selected_date=selected_date,
                    start_date=start_date,
                    end_date=end_date,
                    weekend_bucket=weekend_bucket,
                    week_bucket=week_bucket,
                )
                previous_extracted_data = None
                previous_period_label = ""
                prev_resolved = ad.resolve_previous_period(
                    date_mode=date_mode,
                    valid_dates=valid_dates,
                    selected_date=selected_date,
                    start_date=start_date,
                    end_date=end_date,
                    weekend_bucket=weekend_bucket,
                    week_bucket=week_bucket,
                    week_buckets=week_buckets,
                    weekend_buckets=weekend_buckets,
                )
                if prev_resolved:
                    prev_kwargs, previous_period_label = prev_resolved
                    previous_extracted_data = ad.extract_data_for_report(
                        sheets_dict=sheets,
                        date_mode=date_mode,
                        **prev_kwargs,
                    )

            with st.expander("🧾 查看提交给 AI 的原始数据"):
                st.code(
                    json.dumps(extracted_data, ensure_ascii=False, indent=2, default=str),
                    language="json",
                )

            model_name = st.session_state.get("meta_analysis_model", ad.GEMINI_DEFAULT_MODEL)
            temperature = st.session_state.get("meta_analysis_temperature", 0.5)
            with st.spinner("AI 正在分四节生成完整报告（约 3~6 分钟）..."):
                report = ad.generate_report(
                    api_key=api_key,
                    base_url=ad.GEMINI_BASE_URL,
                    model_name=model_name,
                    system_prompt=build_meta_system_prompt(report_type, date_mode),
                    user_content=ad.build_user_content(extracted_data, date_info, date_mode),
                    temperature=temperature,
                    extracted_data=extracted_data,
                    previous_extracted_data=previous_extracted_data,
                    current_period_label=date_info,
                    previous_period_label=previous_period_label,
                    report_sections=META_REPORT_SECTIONS,
                )

            st.session_state["meta_report"] = report
            st.session_state["meta_report_meta"] = {
                "report_type": report_type,
                "date_info": date_info,
                "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            }
            st.session_state["meta_report_extracted_data"] = extracted_data
            st.session_state["meta_followup_messages"] = []
        except Exception as e:
            st.error(f"❌ 生成报告失败：{e}")

    _render_report_output()


def _render_report_output() -> None:
    if "meta_report" not in st.session_state:
        return

    meta = st.session_state.get("meta_report_meta", {})
    report_body = st.session_state["meta_report"]
    st.markdown("---")
    st.subheader(f"📄 {meta.get('report_type', '')}　|　{meta.get('date_info', '')}")
    st.markdown(report_body)

    md_doc = build_meta_full_markdown(report_body, meta)
    html_doc = ad.build_full_html(report_body, meta)
    base_name = ad._safe_report_filename(meta, "").rstrip(".")

    st.markdown("**下载完整文档**")
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.download_button(
            "📄 Word (.docx)",
            data=ad.build_docx_bytes(report_body, meta),
            file_name=f"{base_name}.docx",
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            use_container_width=True,
        )
    with c2:
        st.download_button(
            "📝 Markdown (.md)",
            data=md_doc,
            file_name=f"{base_name}.md",
            mime="text/markdown",
            use_container_width=True,
        )
    with c3:
        st.download_button(
            "🌐 HTML (.html)",
            data=html_doc,
            file_name=f"{base_name}.html",
            mime="text/html",
            use_container_width=True,
        )
    with c4:
        excel_buf = BytesIO()
        sheets = st.session_state.get("meta_sheets", {})
        with pd.ExcelWriter(excel_buf, engine="openpyxl") as writer:
            for name, df in sheets.items():
                export_df = df[[c for c in df.columns if not str(c).startswith("_")]]
                export_df.to_excel(writer, sheet_name=name[:31], index=False)
        st.download_button(
            "📊 Excel 原始数据",
            data=excel_buf.getvalue(),
            file_name=f"meta_insights_{meta.get('date_info', 'export').replace(' ', '_')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )

    gmail_user = st.session_state.get(SUITE_SMTP_USER, secret("email", "smtp_user"))
    gmail_password = st.session_state.get(SUITE_SMTP_PASSWORD, secret("email", "smtp_password"))
    smtp_override = {**ad.GMAIL_SMTP, "user": gmail_user, "password": gmail_password, "from_addr": gmail_user}
    smtp_ready = bool(ad._get_smtp_config(smtp_override))

    st.markdown("**发送报告到邮箱**")
    if not smtp_ready:
        st.caption("需在 secrets.toml 的 [email] 中配置 SMTP，或参考营销套件侧边栏。")

    col_mail1, col_mail2 = st.columns([2, 1])
    with col_mail1:
        recipient_text = st.text_input(
            "收件人邮箱",
            value=secret("email", "default_recipients"),
            key="meta_email_recipients",
        )
    with col_mail2:
        mail_format = st.selectbox(
            "附件格式",
            options=["docx", "html", "md"],
            format_func=lambda x: {"docx": "Word", "html": "HTML", "md": "Markdown"}[x],
            key="meta_mail_format",
        )

    if st.button("📧 发送邮件", disabled=not smtp_ready, key="meta_send_email"):
        recipients = ad._parse_email_list(recipient_text)
        if not recipients:
            st.error("请填写至少一个有效收件人邮箱。")
        else:
            with st.spinner("正在发送..."):
                ok, message = ad.send_report_email(
                    recipients, report_body, meta,
                    attachment_format=mail_format,
                    smtp_override=smtp_override,
                )
            st.success(message) if ok else st.error(message)

    st.markdown("---")
    st.subheader("💬 具体提问")
    followup_messages = st.session_state.setdefault("meta_followup_messages", [])
    extracted_for_qa = st.session_state.get("meta_report_extracted_data")
    api_key = get_gemini_api_key()
    model_name = st.session_state.get("meta_analysis_model", ad.GEMINI_DEFAULT_MODEL)
    temperature = st.session_state.get("meta_analysis_temperature", 0.5)

    if not extracted_for_qa:
        st.info("请先生成 AI 分析报告后再提问。")
        return

    for msg in followup_messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    if st.button("清空对话", key="meta_clear_followup"):
        st.session_state["meta_followup_messages"] = []
        st.rerun()

    question = st.chat_input("例如：WearNuage 账户 ROAS 下降的主要原因？", key="meta_followup_input")
    if question:
        if not api_key:
            st.error("❌ 请填写 Gemini API Key。")
        else:
            with st.spinner("AI 正在思考..."):
                reply = ad.answer_followup_question(
                    api_key=api_key,
                    base_url=ad.GEMINI_BASE_URL,
                    model_name=model_name,
                    temperature=temperature,
                    extracted_data=extracted_for_qa,
                    report_text=report_body,
                    date_info=meta.get("date_info", ""),
                    report_type=meta.get("report_type", ""),
                    question=question,
                    history=followup_messages,
                    system_prompt=build_meta_followup_system_prompt(),
                )
            followup_messages.append({"role": "user", "content": question})
            followup_messages.append({"role": "assistant", "content": reply})
            st.rerun()


def main() -> None:
    st.set_page_config(
        page_title=APP_TITLE,
        page_icon=str(PAGE_ICON) if PAGE_ICON.exists() else "📊",
        layout="wide",
    )
    _render_sidebar()
    ad.apply_adaptive_theme(st.session_state.get("meta_theme_mode", "system"))

    st.title(APP_TITLE)
    st.caption("Meta Marketing API 拉数 · 日报 / 周报 / 月报 · Week / 周末三日 · Gemini AI 分析")

    sheets = _render_fetch_section()
    if not sheets:
        return

    _render_data_preview(sheets)
    _render_report_controls(sheets)


if __name__ == "__main__":
    main()
