# -*- coding: utf-8 -*-
"""
跨境电商广告投放多维度（日/周/月）智能分析与生成系统
作者: AI数据架构师
说明: 单文件 Streamlit 应用
"""

import re
import json
import os
import smtplib
import ssl
import tempfile
import time
from datetime import datetime, date, timedelta
from typing import Optional
from email.header import Header
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from io import BytesIO
from pathlib import Path

import httpx
import numpy as np
import pandas as pd
import streamlit as st

from suite_shared import (
    GEMINI_KEY_VALIDATION_VERSION,
    SUITE_DEPLOY_VERSION,
    SUITE_GEMINI_API_KEY,
    SUITE_SMTP_PASSWORD,
    SUITE_SMTP_USER,
    _gemini_key_raw_candidates,
    apply_ad_analysis_gemini_patches,
    build_gemini_http_headers,
    describe_gemini_key_input,
    get_gemini_api_key,
    is_gemini_auth_key,
    sanitize_gemini_api_key,
    secret as suite_secret,
    sync_gemini_key_session_state,
)

# 避免 macOS 默认 locale 下 HTTP 请求体中文被按 ASCII 编码
os.environ.setdefault("PYTHONUTF8", "1")


# ============================================================
# 模块零：基础工具函数 - 日期识别与解析
# ============================================================

DATE_PATTERN = re.compile(r'^\d{4}[-/]\d{1,2}[-/]\d{1,2}')
WEEKEND_LABEL_RE = re.compile(r'周末|weekend', re.I)
WEEK_LABEL_RE = re.compile(r'^week(?:\s*\d+)?$', re.I)
EXCEL_SERIAL_MIN = 35000
EXCEL_SERIAL_MAX = 55000
SKIP_ROW_LABEL_RE = re.compile(
    r'^(february?|january|march|april|may|june|july|august|'
    r'september|october|november|december|日期|date)$',
    re.I,
)


def _try_excel_serial_date(val):
    """Excel 日期序列号（常见于 .xlsx 被读成 float，如 45474）。"""
    try:
        if isinstance(val, (int, float, np.integer, np.floating)) and not isinstance(val, bool):
            if isinstance(val, float) and np.isnan(val):
                return None
            n = float(val)
            if EXCEL_SERIAL_MIN <= n <= EXCEL_SERIAL_MAX:
                return pd.to_datetime(n, unit="D", origin="1899-12-30").date()
    except Exception:
        pass
    return None


def is_valid_date(val) -> bool:
    """
    判断一个单元格的值是否是"标准日期"。
    - pandas/python 的日期/时间类型 -> True
    - 形如 2026-06-09 / 2026/6/9 (可带时间后缀) 的字符串 -> True
    - 纯数字 (如周数 23) -> False
    - "Week" / "周末三日" 等纯文本 -> False
    """
    try:
        if val is None:
            return False
        if isinstance(val, float) and np.isnan(val):
            return False
        if pd.isna(val):
            return False
    except (TypeError, ValueError):
        pass

    if isinstance(val, (pd.Timestamp, datetime, date)):
        return True

    # 纯数字：优先识别 Excel 日期序列号，避免与周数混淆
    if isinstance(val, (int, float, np.integer, np.floating)):
        return _try_excel_serial_date(val) is not None

    s = str(val).strip()
    if not s:
        return False

    if DATE_PATTERN.match(s):
        try:
            pd.to_datetime(s)
            return True
        except Exception:
            return False
    return False


def to_date(val):
    """将一个值安全地转换为 datetime.date，转换失败返回 None"""
    try:
        if isinstance(val, (pd.Timestamp, datetime)):
            return val.date()
        if isinstance(val, date):
            return val
        serial_d = _try_excel_serial_date(val)
        if serial_d:
            return serial_d
        return pd.to_datetime(str(val).strip()).date()
    except Exception:
        return None


def _md_dot_parts(val):
    """解析 M.DD / MM.DD 格式（如 11.13、9.1），返回 (month, day) 或 None。"""
    if isinstance(val, str):
        s = val.strip()
    elif isinstance(val, (int, float, np.integer, np.floating)) and not isinstance(val, bool):
        try:
            if isinstance(val, float) and np.isnan(val):
                return None
        except (TypeError, ValueError):
            pass
        s = f"{val:g}"
    else:
        return None

    if "." not in s:
        return None
    left, right = s.split(".", 1)
    try:
        month, day = int(left), int(right)
    except ValueError:
        return None
    if 1 <= month <= 12 and 1 <= day <= 31:
        return month, day
    return None


def is_md_dot_date(val) -> bool:
    """判断是否为 M.DD 短日期（Shopify / Nuage Wear 常用）。"""
    return _md_dot_parts(val) is not None


def _infer_year_for_month_day(month: int, day: int, ref_dates: list) -> int:
    if not ref_dates:
        return datetime.now().year
    years = sorted({d.year for d in ref_dates})
    best_year = years[-1]
    best_dist = None
    for year in years:
        try:
            target = date(year, month, day)
        except ValueError:
            continue
        dist = min(abs((target - rd).days) for rd in ref_dates)
        if best_dist is None or dist < best_dist:
            best_dist = dist
            best_year = year
    return best_year


def md_dot_to_date(val, ref_dates: list = None):
    parts = _md_dot_parts(val)
    if not parts:
        return None
    month, day = parts
    year = _infer_year_for_month_day(month, day, ref_dates or [])
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_any_date(val, ref_dates: list = None):
    """统一解析标准日期、Timestamp、M.DD 短日期。"""
    if is_skip_row_label(val):
        return None
    if is_weekend_label(val):
        return None
    if is_week_label(val):
        return None
    if is_valid_date(val):
        return to_date(val)
    if is_md_dot_date(val):
        return md_dot_to_date(val, ref_dates)
    serial_d = _try_excel_serial_date(val)
    if serial_d:
        return serial_d
    return None


def is_skip_row_label(val) -> bool:
    """月份名、重复表头行等不参与日期匹配（Week 汇总行由 is_week_label 单独识别）。"""
    try:
        if val is None or pd.isna(val):
            return True
    except (TypeError, ValueError):
        pass
    s = str(val).strip()
    if not s or s.lower() == "nan":
        return True
    return bool(SKIP_ROW_LABEL_RE.match(s))


def is_data_row(row) -> bool:
    """判断一行是否为数据行（首列为日期/周末汇总，且其余列有值）。"""
    if row is None or len(row) == 0:
        return False
    col0 = row.iloc[0]
    if is_weekend_label(col0) or is_week_label(col0):
        return True
    if is_skip_row_label(col0):
        return False
    if is_valid_date(col0) or is_md_dot_date(col0):
        return int(row.iloc[1:].notna().sum()) >= 1
    return False


def is_header_row(row) -> bool:
    """判断一行是否为表头行。"""
    if row is None or len(row) == 0:
        return False
    col0 = row.iloc[0]
    if pd.notna(col0) and str(col0).strip() in ("日期", "date", "Date", "DATE"):
        return True
    if is_data_row(row) or is_weekend_label(col0) or is_week_label(col0):
        return False
    text_count = sum(
        1 for v in row
        if pd.notna(v) and isinstance(v, str) and str(v).strip()
    )
    return text_count >= 2


def find_data_start_row(raw: pd.DataFrame) -> int:
    for i in range(len(raw)):
        if is_data_row(raw.iloc[i]):
            return i
    return 1


def find_header_start(raw: pd.DataFrame, data_start: int) -> int:
    header_start = data_start
    for i in range(data_start - 1, -1, -1):
        if is_header_row(raw.iloc[i]):
            header_start = i
        else:
            break
    return 0 if header_start == data_start and data_start > 0 else header_start


def collect_reference_dates(df: pd.DataFrame, date_col) -> list:
    refs = []
    for val in df[date_col]:
        if is_valid_date(val):
            d = to_date(val)
            if d:
                refs.append(d)
            continue
        serial_d = _try_excel_serial_date(val)
        if serial_d:
            refs.append(serial_d)
    return sorted(set(refs))


def collect_all_parseable_dates(df: pd.DataFrame, date_col) -> list:
    """收集 Sheet 日期列中所有可解析日期（含 M.DD 短日期）。"""
    refs = collect_reference_dates(df, date_col)
    for val in df[date_col]:
        if is_skip_row_label(val) or is_week_label(val) or is_weekend_label(val):
            continue
        d = parse_any_date(val, refs)
        if d:
            refs.append(d)
    return sorted(set(refs))


def collect_dates_from_all_sheets(sheets_dict: dict) -> list:
    """从所有 Sheet 汇总可解析日期，供 Week 范围回退匹配。"""
    all_dates = []
    for df in (sheets_dict or {}).values():
        if df is None or df.empty:
            continue
        cleaned = clean_columns(df.copy())
        date_col = find_date_column(cleaned)
        if date_col is None:
            continue
        all_dates.extend(collect_all_parseable_dates(cleaned, date_col))
    return sorted(set(all_dates))


def _group_dates_by_iso_week(dates: list) -> list:
    groups: dict = {}
    for d in dates:
        key = (d.isocalendar().year, d.isocalendar().week)
        groups.setdefault(key, []).append(d)
    return [groups[k] for k in sorted(groups.keys())]


def iso_week_range_for_bucket(week_pos: int, num_buckets: int, all_dates: list):
    """按 Week 汇总行顺序，与全局 ISO 周分组对齐（从最近一周往前匹配）。"""
    groups = _group_dates_by_iso_week(all_dates)
    if not groups or num_buckets <= 0:
        return None, None
    if len(groups) >= num_buckets:
        aligned = groups[-num_buckets:]
        if week_pos < len(aligned):
            g = aligned[week_pos]
            return min(g), max(g)
    if week_pos < len(groups):
        g = groups[week_pos]
        return min(g), max(g)
    return None, None


def build_parsed_date_series(df: pd.DataFrame, date_col) -> pd.Series:
    refs = collect_reference_dates(df, date_col)
    return df[date_col].apply(lambda x: parse_any_date(x, refs))


def is_weekend_label(val) -> bool:
    """判断是否为「周末三日」等汇总行标签（非标准日期）。"""
    try:
        if val is None:
            return False
        if isinstance(val, float) and np.isnan(val):
            return False
        if pd.isna(val):
            return False
    except (TypeError, ValueError):
        pass

    if is_valid_date(val):
        return False

    s = str(val).strip()
    if not s:
        return False
    return bool(WEEKEND_LABEL_RE.search(s)) or "三日" in s


def is_week_label(val) -> bool:
    """判断是否为「Week / Week 23」等周汇总行标签。"""
    try:
        if val is None:
            return False
        if isinstance(val, float) and np.isnan(val):
            return False
        if pd.isna(val):
            return False
    except (TypeError, ValueError):
        pass

    if is_valid_date(val):
        return False

    s = str(val).strip()
    if not s:
        return False
    return bool(WEEK_LABEL_RE.match(s))


def parse_week_number(val):
    """从 Week 标签解析周序号，纯 Week 返回 None。"""
    m = re.match(r'^week\s*(\d+)$', str(val).strip(), re.I)
    return int(m.group(1)) if m else None


def infer_weekend_range(cleaned_df: pd.DataFrame, date_col, row_idx: int):
    """
    根据「周末三日」行在表中的位置，推断对应的周五~周日日期。
    规则：通常紧跟在周四行之后；或以下一行周一反推。
    """
    for i in range(row_idx - 1, max(-1, row_idx - 8), -1):
        val = cleaned_df.iloc[i][date_col]
        refs = collect_reference_dates(cleaned_df, date_col)
        prev = parse_any_date(val, refs)
        if prev and prev.weekday() == 3:  # 周四
            return prev + timedelta(days=1), prev + timedelta(days=3)
        if prev:
            break

    for i in range(row_idx + 1, min(len(cleaned_df), row_idx + 8)):
        val = cleaned_df.iloc[i][date_col]
        refs = collect_reference_dates(cleaned_df, date_col)
        nxt = parse_any_date(val, refs)
        if nxt and nxt.weekday() == 0:  # 周一
            sun = nxt - timedelta(days=1)
            return sun - timedelta(days=2), sun
        if nxt:
            break

    return None, None


def infer_week_range(
    cleaned_df: pd.DataFrame,
    date_col,
    row_idx: int,
    week_row_indices: list = None,
    all_dates: list = None,
):
    """
    根据 Week 汇总行在表中的位置，推断对应周的日期范围。
    1) 取「上一个 Week 行 ~ 当前 Week 行」之间所有可解析日期
    2) 若无则取当前 Week 行 ~ 下一个 Week 行之间
    3) 再按 Week N 的 ISO 周序号匹配
    4) 最后按 Week 行顺序与全局 ISO 周分组对齐（跨 Sheet 回退）
    """
    refs = collect_all_parseable_dates(cleaned_df, date_col)
    if all_dates:
        refs = sorted(set(refs) | set(all_dates))

    week_row_indices = week_row_indices or [
        i for i, val in enumerate(cleaned_df[date_col]) if is_week_label(val)
    ]
    if row_idx not in week_row_indices:
        return None, None

    pos = week_row_indices.index(row_idx)
    week_val = cleaned_df.iloc[row_idx][date_col]
    week_num = parse_week_number(week_val)

    def _dates_between(start_row: int, end_row: int) -> list:
        found = []
        for i in range(start_row, end_row):
            if i < 0 or i >= len(cleaned_df):
                continue
            d = parse_any_date(cleaned_df.iloc[i][date_col], refs)
            if d:
                found.append(d)
        return found

    prev_week = week_row_indices[pos - 1] if pos > 0 else -1
    before_dates = _dates_between(prev_week + 1, row_idx)
    if before_dates:
        iso_groups = _group_dates_by_iso_week(before_dates)
        num_weeks = len(week_row_indices)
        # 多个 Week 行堆在底部、上方是一大段逐日数据时，按 ISO 周切分
        if len(before_dates) > 8 and len(iso_groups) > 1:
            if len(iso_groups) >= num_weeks:
                aligned = iso_groups[-num_weeks:]
                if pos < len(aligned):
                    g = aligned[pos]
                    return min(g), max(g)
            if pos < len(iso_groups):
                g = iso_groups[pos]
                return min(g), max(g)
        return min(before_dates), max(before_dates)

    next_week = (
        week_row_indices[pos + 1]
        if pos + 1 < len(week_row_indices)
        else len(cleaned_df)
    )
    after_dates = _dates_between(row_idx + 1, next_week)
    if after_dates:
        return min(after_dates), max(after_dates)

    if week_num is not None and refs:
        iso_dates = [d for d in refs if d.isocalendar().week == week_num]
        if iso_dates:
            return min(iso_dates), max(iso_dates)

    if all_dates and week_row_indices:
        return iso_week_range_for_bucket(pos, len(week_row_indices), all_dates)

    return None, None


def format_week_bucket_label(bucket: dict) -> str:
    """格式化 Week 选项文案，含日期范围便于定位。"""
    raw = bucket.get("raw_label", "Week")
    start = bucket.get("start")
    end = bucket.get("end")
    if start and end:
        if start == end:
            return f"{raw}（{start.strftime('%Y-%m-%d')}）"
        return f"{raw}（{start.strftime('%Y-%m-%d')} ~ {end.strftime('%Y-%m-%d')}）"
    if bucket.get("week_number") is not None:
        return f"Week {bucket['week_number']}（周汇总）"
    return f"{raw}（周汇总）"


def build_week_buckets_from_valid_dates(valid_dates: list) -> list:
    """用逐日数据按 ISO 周生成 Week 选项（始终带日期范围，避免 Excel 重复 Week 行）。"""
    if not valid_dates:
        return []
    groups = _group_dates_by_iso_week(valid_dates)
    buckets = []
    for i, g in enumerate(groups):
        start, end = min(g), max(g)
        wn = start.isocalendar().week
        buckets.append({
            "bucket_index": i,
            "raw_label": f"Week {wn}",
            "week_number": wn,
            "iso_year": start.isocalendar().year,
            "start": start,
            "end": end,
            "from_valid_dates": True,
        })
    return buckets


def resolve_week_buckets_for_ui(sheets_dict: dict, valid_dates: list) -> list:
    """优先用逐日数据生成带日期范围的 Week 列表；无逐日数据时回退 Excel Week 行。"""
    if valid_dates:
        return build_week_buckets_from_valid_dates(valid_dates)
    return get_week_buckets(sheets_dict)


def enrich_week_buckets(week_buckets: list, sheets_dict: dict) -> list:
    """为缺少日期范围的 Week 选项，用全局可解析日期按 ISO 周补齐。"""
    if not week_buckets:
        return week_buckets

    valid_dates = get_valid_dates(sheets_dict)
    if not valid_dates:
        valid_dates = collect_dates_from_all_sheets(sheets_dict)
    if not valid_dates:
        return week_buckets

    num = len(week_buckets)
    enriched = []
    for bucket in week_buckets:
        nb = dict(bucket)
        if not (nb.get("start") and nb.get("end")):
            start, end = iso_week_range_for_bucket(nb["bucket_index"], num, valid_dates)
            if start and end:
                nb["start"] = start
                nb["end"] = end
        enriched.append(nb)
    return enriched


def get_weekend_buckets(sheets_dict: dict) -> list:
    """从 Shopify Sheet 扫描所有「周末三日」汇总段，返回可选 bucket 列表。"""
    try:
        shopify_df = get_shopify_df(sheets_dict)
        if shopify_df is None or shopify_df.empty:
            return []

        cleaned_df = clean_columns(shopify_df)
        date_col = find_date_column(cleaned_df)
        if date_col is None:
            return []

        buckets = []
        bucket_index = 0
        for idx in range(len(cleaned_df)):
            val = cleaned_df.iloc[idx][date_col]
            if not is_weekend_label(val):
                continue

            fri, sun = infer_weekend_range(cleaned_df, date_col, idx)
            if fri is None or sun is None:
                continue

            buckets.append({
                "bucket_index": bucket_index,
                "raw_label": str(val).strip(),
                "start": fri,
                "end": sun,
            })
            bucket_index += 1

        return buckets
    except Exception as e:
        st.warning(f"⚠️ 扫描周末三日数据时出错：{e}")
        return []


def get_week_buckets(sheets_dict: dict) -> list:
    """扫描 Sheet 日期列中所有「Week / Week N」周汇总行。"""
    try:
        source_df = get_shopify_df(sheets_dict)
        source_name = "Shopify"
        if source_df is None:
            for name, df in sheets_dict.items():
                if df is not None and not df.empty:
                    source_df = df
                    source_name = name
                    break
        if source_df is None or source_df.empty:
            return []

        cleaned_df = clean_columns(source_df)
        date_col = find_date_column(cleaned_df)
        if date_col is None:
            return []

        buckets = []
        week_row_indices = [
            i for i, val in enumerate(cleaned_df[date_col]) if is_week_label(val)
        ]
        all_dates = collect_dates_from_all_sheets(sheets_dict)

        for idx in week_row_indices:
            label = str(cleaned_df.iloc[idx][date_col]).strip()
            start, end = infer_week_range(
                cleaned_df,
                date_col,
                idx,
                week_row_indices=week_row_indices,
                all_dates=all_dates,
            )
            bucket = {
                "bucket_index": len(buckets),
                "raw_label": label,
                "week_number": parse_week_number(label),
                "row_index": idx,
                "source_sheet": source_name,
            }
            if start and end:
                bucket["start"] = start
                bucket["end"] = end
            buckets.append(bucket)
        return enrich_week_buckets(buckets, sheets_dict)
    except Exception as e:
        st.warning(f"⚠️ 扫描 Week 周汇总行时出错：{e}")
        return []


def _filter_weekend_rows(cleaned_df: pd.DataFrame, date_col, bucket: dict) -> pd.DataFrame:
    """在各 Sheet 中定位与 bucket 对应的「周末三日」汇总行。"""
    raw = cleaned_df[date_col]
    weekend_indices = [i for i, val in enumerate(raw) if is_weekend_label(val)]

    bucket_idx = bucket["bucket_index"]
    if bucket_idx < len(weekend_indices):
        return cleaned_df.iloc[[weekend_indices[bucket_idx]]]

    label = bucket["raw_label"]
    label_matches = cleaned_df[raw.astype(str).str.strip() == label]
    if bucket_idx < len(label_matches):
        return label_matches.iloc[[bucket_idx]]

    return pd.DataFrame()


def _filter_week_rows(cleaned_df: pd.DataFrame, date_col, bucket: dict) -> pd.DataFrame:
    """在各 Sheet 中定位与 bucket 对应的「Week」周汇总行。"""
    raw = cleaned_df[date_col]
    week_indices = [i for i, val in enumerate(raw) if is_week_label(val)]
    if not week_indices:
        return pd.DataFrame()

    b_start = bucket.get("start")
    b_end = bucket.get("end")
    b_week_num = bucket.get("week_number")
    all_dates = collect_all_parseable_dates(cleaned_df, date_col)

    best_idx = None
    best_score = -1
    for i in week_indices:
        val = raw.iloc[i]
        wn = parse_week_number(val)
        if b_week_num is not None and wn is not None and wn != b_week_num:
            continue

        if b_start and b_end:
            row_start, row_end = infer_week_range(
                cleaned_df,
                date_col,
                i,
                week_row_indices=week_indices,
                all_dates=all_dates,
            )
            if row_start and row_end:
                overlap_start = max(row_start, b_start)
                overlap_end = min(row_end, b_end)
                if overlap_start <= overlap_end:
                    score = (overlap_end - overlap_start).days
                    if score > best_score:
                        best_score = score
                        best_idx = i
            elif b_week_num is not None and wn == b_week_num and best_idx is None:
                best_idx = i
        elif b_week_num is not None and wn == b_week_num:
            return cleaned_df.iloc[[i]]

    if best_idx is not None:
        return cleaned_df.iloc[[best_idx]]

    bucket_idx = bucket.get("bucket_index", 0)
    if bucket_idx < len(week_indices):
        return cleaned_df.iloc[[week_indices[bucket_idx]]]

    label = bucket.get("raw_label", "")
    label_matches = cleaned_df[raw.astype(str).str.strip().str.lower() == label.lower()]
    if not label_matches.empty:
        return label_matches.iloc[[0]]

    if b_week_num is not None:
        for i, val in enumerate(raw):
            if is_week_label(val) and parse_week_number(val) == b_week_num:
                return cleaned_df.iloc[[i]]

    return pd.DataFrame()


# ============================================================
# 模块一(数据层)：复杂合并表头的"暴力拍平"读取引擎
# ============================================================

def flatten_merged_header(raw: pd.DataFrame) -> pd.DataFrame:
    """
    将已读出的原始表格（header=None）拍平 2-4 行合并表头，返回标准 DataFrame。
    适配 Shopify 等多层表头、M.DD 短日期等格式。
    """
    if raw.empty or raw.shape[0] < 1:
        return pd.DataFrame()

    data_start_row = find_data_start_row(raw)
    if data_start_row == 0:
        data_start_row = 1

    header_start = find_header_start(raw, data_start_row)
    header_block = raw.iloc[header_start:data_start_row]
    header_block = header_block.ffill(axis=1).ffill(axis=0)

    data_block = raw.iloc[data_start_row:].reset_index(drop=True)

    new_columns = []
    for col_idx in range(raw.shape[1]):
        parts = []
        for row_idx in range(len(header_block)):
            val = header_block.iloc[row_idx, col_idx]
            if pd.notna(val):
                s = str(val).strip()
                if s and s.lower() != "nan" and s not in parts:
                    parts.append(s)
        col_name = "_".join(parts) if parts else f"列{col_idx}"
        new_columns.append(col_name)

    seen = {}
    final_columns = []
    for c in new_columns:
        if c in seen:
            seen[c] += 1
            final_columns.append(f"{c}_{seen[c]}")
        else:
            seen[c] = 0
            final_columns.append(c)

    data_block.columns = final_columns
    return data_block


def smart_read_sheet(xls: pd.ExcelFile, sheet_name: str) -> pd.DataFrame:
    """通过 pandas ExcelFile 读取 Sheet 并拍平合并表头。"""
    raw = pd.read_excel(xls, sheet_name=sheet_name, header=None)
    return flatten_merged_header(raw)


def _read_uploaded_bytes(uploaded_file) -> bytes:
    uploaded_file.seek(0)
    return uploaded_file.read()


def _read_sheet_with_calamine(file_bytes: bytes, sheet_name: str) -> pd.DataFrame:
    """用 Rust 引擎 calamine 读取，可绕过 openpyxl 对损坏 sharedStrings 的报错。"""
    from python_calamine import CalamineWorkbook

    try:
        wb = CalamineWorkbook.from_filelike(BytesIO(file_bytes))
    except Exception:
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
            tmp.write(file_bytes)
            tmp_path = tmp.name
        try:
            wb = CalamineWorkbook.from_path(tmp_path)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    sheet = wb.get_sheet_by_name(sheet_name)
    if sheet is None:
        raise ValueError(f"Sheet「{sheet_name}」不存在")
    return pd.DataFrame(sheet.to_python())


def _open_excel_workbook(uploaded_file):
    """
    依次尝试 calamine / openpyxl 打开工作簿。
    返回 (sheet_names, read_raw_fn)，read_raw_fn(sheet_name) -> DataFrame(header=None)。
    """
    file_bytes = _read_uploaded_bytes(uploaded_file)
    errors = []

    for engine in ("calamine", "openpyxl"):
        try:
            xls = pd.ExcelFile(BytesIO(file_bytes), engine=engine)

            def read_raw(sheet_name, _xls=xls):
                return pd.read_excel(_xls, sheet_name=sheet_name, header=None)

            return xls.sheet_names, read_raw, engine
        except Exception as e:
            errors.append(f"{engine}: {e}")

    try:
        from python_calamine import CalamineWorkbook

        def read_raw(sheet_name, _bytes=file_bytes):
            return _read_sheet_with_calamine(_bytes, sheet_name)

        wb = CalamineWorkbook.from_filelike(BytesIO(file_bytes))
        return wb.sheet_names, read_raw, "calamine_direct"
    except Exception as e:
        errors.append(f"calamine_direct: {e}")

    detail = "\n".join(f"  - {msg}" for msg in errors)
    raise RuntimeError(
        "无法读取该 Excel 文件（常见于 WPS/在线表格导出的损坏 XML）。\n"
        f"{detail}\n"
        "建议：用 Microsoft Excel 打开后「另存为」新的 .xlsx 再上传。"
    )


# 未在投放的账号 Sheet，读取 Excel 时自动跳过
SKIPPED_SHEET_NAMES = frozenset({"nuage2026", "nuage wear"})


def _is_skipped_sheet(sheet_name: str) -> bool:
    return sheet_name.strip().lower() in SKIPPED_SHEET_NAMES


def load_all_sheets(uploaded_file) -> dict:
    """读取上传文件中的所有 Sheet（或 CSV 的单表），返回 {sheet名: DataFrame}"""
    sheets = {}
    try:
        file_name = uploaded_file.name.lower()

        if file_name.endswith(".csv"):
            try:
                df = pd.read_csv(uploaded_file)
            except UnicodeDecodeError:
                uploaded_file.seek(0)
                df = pd.read_csv(uploaded_file, encoding="gbk")
            sheets["数据表"] = df
            return sheets

        # xlsx 文件（多引擎容错：calamine -> openpyxl -> calamine 直读）
        sheet_names, read_raw, engine_used = _open_excel_workbook(uploaded_file)
        if engine_used != "openpyxl":
            st.info(f"ℹ️ 已使用 {engine_used} 引擎读取（原 openpyxl 无法解析该文件）。")

        skipped_sheets = []
        for sheet_name in sheet_names:
            if _is_skipped_sheet(sheet_name):
                skipped_sheets.append(sheet_name)
                continue
            try:
                raw = read_raw(sheet_name)
                df = flatten_merged_header(raw)
                if df is not None and not df.empty:
                    sheets[sheet_name] = df
                else:
                    st.warning(f"⚠️ Sheet「{sheet_name}」为空或无法解析，已跳过。")
            except Exception as e:
                st.warning(f"⚠️ 读取 Sheet「{sheet_name}」时发生错误，已跳过：{e}")

        if skipped_sheets:
            st.caption(
                "ℹ️ 已自动跳过未投放账号表："
                + "、".join(skipped_sheets)
            )

    except Exception as e:
        st.error(f"❌ 文件解析失败：{e}")

    return sheets


# ============================================================
# 模块二：纯净数据抓取引擎
# ============================================================

DROP_COL_KEYWORDS = [
    "总结", "分析", "理解", "复盘", "心得", "解读",
    "summary", "analysis", "insight", "comment", "note",
]


def clean_columns(df: pd.DataFrame) -> pd.DataFrame:
    """删除所有列名中包含'总结/分析/理解'等人工洞察字样的列"""
    if df is None or df.empty:
        return df

    cols_to_drop = []
    for col in df.columns:
        col_str = str(col).lower()
        for kw in DROP_COL_KEYWORDS:
            if kw.lower() in col_str:
                cols_to_drop.append(col)
                break

    if cols_to_drop:
        return df.drop(columns=cols_to_drop, errors="ignore")
    return df


def find_date_column(df: pd.DataFrame):
    """
    找到日期列：
    1. 优先匹配列名中含"日期"/"时间"/"date"的列；
    2. 否则在前几列中，选择"看起来像日期的值"最多的列；
    3. 实在找不到则返回第一列。
    """
    if df is None or df.empty or len(df.columns) == 0:
        return None

    keywords = ["日期", "时间", "date", "Date", "DATE"]
    for col in df.columns:
        col_str = str(col)
        for kw in keywords:
            if kw in col_str:
                return col

    best_col = df.columns[0]
    best_count = -1
    for col in df.columns[: min(5, len(df.columns))]:
        try:
            refs = collect_reference_dates(df, col)
            count = sum(
                1 for v in df[col]
                if parse_any_date(v, refs) is not None
            )
            if count > best_count:
                best_count = count
                best_col = col
        except Exception:
            continue

    return best_col


def extract_row_dict(row: pd.Series) -> dict:
    """把一行数据转成 {列名: 值} 的字典，剔除 NaN，并把 numpy/Timestamp 转成原生类型"""
    d = {}
    for col, val in row.items():
        try:
            if val is None:
                continue
            if isinstance(val, float) and np.isnan(val):
                continue
            if pd.isna(val):
                continue
        except (TypeError, ValueError):
            pass

        if isinstance(val, (pd.Timestamp, datetime, date)):
            val = str(val)[:10]
        elif isinstance(val, np.integer):
            val = int(val)
        elif isinstance(val, np.floating):
            fval = float(val)
            val = int(fval) if fval == int(fval) else round(fval, 4)
        elif isinstance(val, float):
            val = int(val) if val == int(val) else round(val, 4)

        d[str(col)] = val
    return d


def extract_data_for_report(
    sheets_dict: dict,
    date_mode: str,
    selected_date: date = None,
    start_date: date = None,
    end_date: date = None,
    weekend_bucket: dict = None,
    week_bucket: dict = None,
) -> dict:
    """
    核心数据抓取：
    - single：各 Sheet 中日期 == selected_date 的行
    - range：日期落在 [start_date, end_date] 的行（时间序列）
    - week：匹配日期列为「Week / Week N」的周汇总行
    - weekend：匹配「周末三日」汇总行（周五~周日合并数据）
    """
    result = {}

    for sheet_name, df in sheets_dict.items():
        try:
            if df is None or df.empty:
                result[sheet_name] = f"【{sheet_name}】该 Sheet 数据为空，跳过。"
                continue

            cleaned_df = clean_columns(df.copy())
            date_col = find_date_column(cleaned_df)

            if date_col is None or date_col not in cleaned_df.columns:
                result[sheet_name] = f"【{sheet_name}】未识别到日期列，跳过该渠道数据提取。"
                continue

            if date_mode == "week":
                if not week_bucket:
                    result[sheet_name] = f"【{sheet_name}】未指定 Week 周汇总，跳过。"
                    continue
                filtered = _filter_week_rows(cleaned_df, date_col, week_bucket)
            elif date_mode == "weekend":
                if not weekend_bucket:
                    result[sheet_name] = f"【{sheet_name}】未指定周末三日区间，跳过。"
                    continue
                filtered = _filter_weekend_rows(cleaned_df, date_col, weekend_bucket)
                # 同时附上该周末 Fri~Sun 的逐日明细（若有），供 AI 做趋势分析
                parsed_dates = pd.to_datetime(
                    build_parsed_date_series(cleaned_df, date_col)
                )
                fri, sun = weekend_bucket["start"], weekend_bucket["end"]
                daily_mask = (parsed_dates >= pd.Timestamp(fri)) & (
                    parsed_dates <= pd.Timestamp(sun)
                )
                daily_rows = cleaned_df[daily_mask]
                if not daily_rows.empty:
                    filtered = pd.concat([filtered, daily_rows]).drop_duplicates()
            else:
                parsed_dates = pd.to_datetime(
                    build_parsed_date_series(cleaned_df, date_col)
                )

                if date_mode == "single":
                    if selected_date is None:
                        result[sheet_name] = f"【{sheet_name}】未指定日期，跳过。"
                        continue
                    mask = parsed_dates == pd.Timestamp(selected_date)
                else:
                    if start_date is None or end_date is None:
                        result[sheet_name] = f"【{sheet_name}】未指定时间区间，跳过。"
                        continue
                    mask = (parsed_dates >= pd.Timestamp(start_date)) & (
                        parsed_dates <= pd.Timestamp(end_date)
                    )
                filtered = cleaned_df[mask]

            if filtered.empty:
                if date_mode == "week":
                    result[sheet_name] = (
                        f"【{sheet_name}】未找到「{week_bucket['raw_label']}」周汇总行。"
                    )
                elif date_mode == "weekend":
                    fri = weekend_bucket["start"].strftime("%Y-%m-%d")
                    sun = weekend_bucket["end"].strftime("%Y-%m-%d")
                    result[sheet_name] = (
                        f"【{sheet_name}】未找到 {fri} ~ {sun} 的「周末三日」汇总行。"
                    )
                else:
                    result[sheet_name] = f"【{sheet_name}】在此时间范围内无数据记录。"
                continue

            records = [extract_row_dict(row) for _, row in filtered.iterrows()]
            result[sheet_name] = records

        except Exception as e:
            result[sheet_name] = (
                f"处理 Sheet「{sheet_name}」时发生异常：{e}，已跳过该渠道数据，"
                f"不影响其他渠道分析。"
            )

    return result


# ============================================================
# 模块三：动态日期范围 - 基于 Shopify Sheet
# ============================================================

def get_shopify_df(sheets_dict: dict):
    """模糊匹配名字中含 shopify 的 Sheet；找不到则退而求其次用第一个 Sheet（兼容CSV单表场景）"""
    for name, df in sheets_dict.items():
        if "shopify" in str(name).lower():
            return df
    if sheets_dict:
        return list(sheets_dict.values())[0]
    return None


def get_valid_dates(sheets_dict: dict):
    """从 Shopify Sheet 的日期列中，提取所有符合 YYYY-MM-DD 的真实日期，排除纯文本汇总行"""
    try:
        shopify_df = get_shopify_df(sheets_dict)
        if shopify_df is None or shopify_df.empty:
            return []

        cleaned_df = clean_columns(shopify_df)
        date_col = find_date_column(cleaned_df)
        if date_col is None:
            return []

        valid_dates = set()
        parsed = build_parsed_date_series(cleaned_df, date_col)
        for d in parsed:
            if d:
                valid_dates.add(d)

        return sorted(valid_dates)
    except Exception as e:
        st.warning(f"⚠️ 提取日期列表时出错：{e}")
        return []


def get_date_mode_options_for_report(
    report_type: str,
    weekend_buckets: list,
    week_buckets: list,
) -> tuple:
    """根据报告维度返回可选的数据范围、默认选项与说明。"""
    if report_type == "日报":
        return (
            ["单一日期"],
            "单一日期",
            "日报：选择单个分析日期。",
        )
    if report_type == "周报":
        if week_buckets:
            options = ["Week 周数据"]
            if weekend_buckets:
                options.append("周末三日")
            options.append("日期范围")
            return (
                options,
                "Week 周数据",
                "周报：默认识别表格中日期列为 **Week** 的周汇总行。",
            )
        options = ["日期范围"]
        default = "日期范围"
        if weekend_buckets:
            options.append("周末三日")
            default = "周末三日"
        return (
            options,
            default,
            "周报：未检测到 Week 行，可改选日期范围或周末三日。",
        )
    # 月报
    return (
        ["日期范围"],
        "日期范围",
        "月报：选择整月或自定义月份区间（默认当月已有数据）。",
    )


def get_default_range_for_report(report_type: str, valid_dates: list) -> tuple:
    """按报告维度计算日期范围的默认起止日。"""
    if not valid_dates:
        return None, None
    min_d, max_d = min(valid_dates), max(valid_dates)
    if report_type == "周报":
        start = max(min_d, max_d - timedelta(days=6))
        return start, max_d
    if report_type == "月报":
        month_start = max_d.replace(day=1)
        start = max(min_d, month_start)
        return start, max_d
    return max_d, max_d


# ============================================================
# 模块四：AI 分析报告生成引擎
# ============================================================

def build_system_prompt(report_type: str, date_mode: str) -> str:
    if date_mode == "single":
        time_note = (
            f"这是一份【{report_type}】，数据为**单日**快照，"
            f"请重点关注该日各渠道的即时表现、异常波动及与近期均值的对比。"
        )
    elif date_mode == "week":
        time_note = (
            f"这是一份【{report_type}】，数据来自表格中日期列为 **Week** 的**周汇总行**（非逐日明细）。"
            f"请按整周口径解读各渠道消耗、ROAS、转化，并与相邻周对比。"
        )
    elif date_mode == "weekend":
        time_note = (
            f"这是一份【{report_type}】，数据为**周末三日汇总**（周五+周六+周日合并），"
            f"请按整个周末区间评估表现，并与相邻工作日/周末对比。"
        )
    elif report_type == "周报":
        time_note = (
            f"这是一份【周报】，数据为所选**日期范围**内的逐日表现。"
            f"请汇总该区间各渠道趋势并给出下周建议。"
        )
    elif report_type == "月报":
        time_note = (
            f"这是一份【月报】，数据为所选**月份区间**内的表现。"
            f"请从月度维度分析体量、趋势、渠道贡献与结构性变化，并给出下月策略。"
        )
    else:
        time_note = (
            f"这是一份【{report_type}】，数据为**日期范围内的时间序列**。"
            f"请务必从时间维度分析消耗、销售额、ROAS 等核心指标的变化趋势"
            f"（上升/下降/平稳/剧烈波动），并明确指出关键的转折点或异常日期。"
        )

    return f"""# 角色设定
你是一位拥有十年以上经验的顶尖跨境电商品牌 CMO（首席营销官），尤其精通 Meta、Google、AppLovin 等多渠道效果广告的数据分析与预算决策，风格数据驱动、直击要害、敢于给出明确结论和行动建议。

# 当前任务
请基于我提供的【原始投放数据】，生成一份【{report_type}】。
{time_note}

# 数据说明
- 数据来源可能包括：Shopify（电商大盘销售数据）、Google（Google Ads 投放数据）、Axon(AppLovin)（AppLovin 投放数据），以及多个 Meta 广告账号（如 WearNuage、Nuage Bra 等）。
- 每个渠道的数据是一份「记录列表」，每条记录对应一天（或一行）的原始字段，字段名以实际数据为准（可能包含消耗/Spend/Cost、曝光、点击、转化、ROAS、CPA、销售额/Revenue/Sales、订单数等）。
- 若某渠道在该区间无数据，会用文字注明"该渠道此区间无数据"，请正常处理，不要编造数据，也不要因此中断对其他渠道的分析。

# 输出结构要求（必须严格按以下 Markdown 结构输出，使用简体中文）

## 一、大盘纵览（Executive Summary）
- 结合 Shopify 的销售额（大盘数据）与各广告渠道的总消耗，评估整体投入产出比与业务体量。

## 二、环比对比（Period-over-Period）
- 与**上一周期**（日报=上一日，周报=上一 Week/区间，周末=上一周末）对比消耗、ROAS、订单等核心指标变化。
- 环比表格由系统自动生成；你需撰写环比洞察，引用表格中的涨跌数字。

## 三、渠道表现拆解
- 跨 Sheet 对比 Google、各个 Meta 账号、AppLovin（Axon）的消耗与 ROAS / 转化表现。

## 四、下一步行动指令（Action Items）
- 给出具体、可执行的预算与关停/放量建议。

# 注意事项
- 全文使用简体中文，语言专业、简洁、有决策力。
- 若某渠道数据缺失，简要说明即可，不影响其他部分的结论。
- 不要输出与上述结构无关的内容，不要重复粘贴原始数据。

# 硬性要求（必须遵守，否则视为失败）
- **必须完整输出四个章节**，不得在中途停止；第三章表格由系统生成，你需补充 3.3 洞察。
- **所有结论必须引用原始数据中的具体数字**（消耗、ROAS、ROI、销售额、订单数等），禁止空泛描述。
- 第三章表格至少包含：Shopify 大盘、Google、Axon(AppLovin)、WearNuage、Nuage Bra 等所有有数据的渠道。
- 第四章至少给出 **3 条以上** 具体行动建议，每条含渠道名 + 量化调整幅度。
- 报告总篇幅不少于 **800 字**。
"""


def build_data_digest(extracted_data: dict) -> str:
    """生成关键指标 plaintext 摘要，确保模型能直接引用数字。"""
    lines = ["## 关键数据摘要（报告中必须引用以下数字）\n"]
    for sheet_name, data in extracted_data.items():
        if not isinstance(data, list) or not data:
            continue
        for idx, row in enumerate(data):
            label = f"记录{idx + 1}" if len(data) > 1 else "汇总"
            parts = []
            for k, v in row.items():
                if k == "日期" or v in (None, "", "/", "nan"):
                    continue
                try:
                    if isinstance(v, float) and np.isnan(v):
                        continue
                except (TypeError, ValueError):
                    pass
                parts.append(f"{k}={v}")
            if parts:
                lines.append(f"- **{sheet_name}** ({label}): " + "; ".join(parts))
    return "\n".join(lines) + "\n"


def _pick_summary_row(rows: list) -> dict:
    """优先取 Week / 周末三日 汇总行，否则取首行。"""
    for row in rows:
        if is_week_label(row.get("日期")):
            return row
    for row in rows:
        if is_weekend_label(row.get("日期")):
            return row
    return rows[0] if rows else {}


def _is_numeric(val) -> bool:
    if val is None:
        return False
    if isinstance(val, (int, float, np.integer, np.floating)):
        try:
            return not (isinstance(val, float) and np.isnan(val))
        except (TypeError, ValueError):
            return True
    return False


def _fmt_metric(val) -> str:
    if not _is_numeric(val):
        return str(val)
    f = float(val)
    if f == int(f):
        return f"{int(f):,}"
    return f"{f:,.2f}"


def _find_first_numeric(row: dict, keywords: list, prefer: str = None):
    prefer_match = None
    first_match = None
    for key, val in row.items():
        if not _is_numeric(val):
            continue
        key_str = str(key)
        for kw in keywords:
            if kw.lower() in key_str.lower():
                if prefer and prefer in key_str:
                    prefer_match = float(val)
                elif first_match is None:
                    first_match = float(val)
                break
    return prefer_match if prefer_match is not None else first_match


def _get_shopify_row(extracted_data: dict) -> dict:
    for name, data in extracted_data.items():
        if "shopify" in str(name).lower() and isinstance(data, list) and data:
            return _pick_summary_row(data)
    return {}


def _shopify_spend_for_channel(shopify_row: dict, channel_name: str):
    if not shopify_row:
        return None
    name_lower = channel_name.lower().replace(" ", "")
    mapping = [
        ("google", "Ad Spent_Google"),
        ("axon", "Ad Spent_Axon(AppLovin)"),
        ("applovin", "Ad Spent_Axon(AppLovin)"),
        ("wearnuage", "Ad Spent_WearNuage"),
        ("nuagebra", "Ad Spent_NuageBra"),
        ("nuage2026", "Ad Spent_Nuage2026"),
        ("nuagewear", "Ad Spent_NuageWear"),
    ]
    for pattern, col in mapping:
        if pattern in name_lower:
            val = shopify_row.get(col)
            if _is_numeric(val):
                return float(val)
    return None


def _extract_channel_summary(sheet_name: str, row: dict, shopify_row: dict) -> dict:
    shopify_spend = _shopify_spend_for_channel(shopify_row, sheet_name)
    local_spend = _find_first_numeric(row, [
        "Ad Spent_Total", "总消耗", "Spent", "消耗", "Cost", "Spend",
    ])
    if shopify_spend is not None and "shopify" not in sheet_name.lower():
        spend = shopify_spend
    else:
        spend = local_spend

    roas = _find_first_numeric(row, ["ROAS", "ROI", "MER"], prefer="总体")
    if roas is None:
        roas = _find_first_numeric(row, ["总ROAS", "ROI_总"])
    orders = _find_first_numeric(row, ["出单量", "Orders", "订单", "Oders", "转化"])

    extras = []
    for key, val in row.items():
        if key == "日期" or not _is_numeric(val):
            continue
        kl = str(key).lower()
        if any(x in kl for x in ["消耗", "spent", "roas", "roi", "出单", "order", "订单"]):
            continue
        if ("cpm" in kl or "cpc" in kl) and "日期" not in str(key):
            extras.append(f"{key.split('_')[-1]}={_fmt_metric(val)}")

    return {
        "channel": sheet_name,
        "spend": spend,
        "roas": roas,
        "orders": orders,
        "extra": "; ".join(extras[:3]) if extras else "-",
    }


def build_channel_tables_markdown(extracted_data: dict) -> str:
    """用代码从原始数据生成完整渠道表格，不依赖 LLM。"""
    shopify_row = _get_shopify_row(extracted_data)
    lines = [
        "## 三、渠道表现拆解",
        "",
        "### 3.1 渠道核心指标汇总",
        "",
        "| 渠道/账号 | 消耗 (USD) | ROAS/ROI | 出单/订单 | 其他 |",
        "|----------|-----------|----------|----------|------|",
    ]

    summaries = []
    for sheet_name, data in extracted_data.items():
        if not isinstance(data, list) or not data:
            continue
        row = _pick_summary_row(data)
        if "shopify" in str(sheet_name).lower():
            s = _extract_channel_summary(sheet_name, row, shopify_row)
            s["spend"] = _find_first_numeric(row, ["Ad Spent_Total"])
            s["roas"] = _find_first_numeric(row, ["MER", "ROI", "ROAS"])
            s["orders"] = _find_first_numeric(row, ["订单", "Oders", "Orders"])
            sales = _find_first_numeric(row, ["Total Sales", "销售额"])
            s["extra"] = f"销售额={_fmt_metric(sales)}" if sales else "-"
            summaries.append(s)
        else:
            summaries.append(_extract_channel_summary(sheet_name, row, shopify_row))

    for s in summaries:
        spend_s = _fmt_metric(s["spend"]) if s["spend"] is not None else "-"
        roas_s = _fmt_metric(s["roas"]) if s["roas"] is not None else "-"
        orders_s = _fmt_metric(s["orders"]) if s["orders"] is not None else "-"
        lines.append(
            f"| {s['channel']} | {spend_s} | {roas_s} | {orders_s} | {s['extra']} |"
        )

    lines.extend(["", "### 3.2 各渠道完整指标明细", ""])
    for sheet_name, data in extracted_data.items():
        if not isinstance(data, list) or not data:
            lines.append(f"**{sheet_name}**：{data}")
            lines.append("")
            continue

        lines.append(f"#### {sheet_name}")
        lines.append("")
        lines.append("| 指标字段 | 数值 |")
        lines.append("|---------|------|")
        for row_idx, row in enumerate(data):
            prefix = ""
            if len(data) > 1:
                prefix = f"[{row.get('日期', f'记录{row_idx + 1}')}] "
            for key, val in row.items():
                if key == "日期":
                    continue
                if val in (None, "", "nan"):
                    continue
                try:
                    if isinstance(val, float) and np.isnan(val):
                        continue
                except (TypeError, ValueError):
                    pass
                display_val = "无数据" if val == "/" else val
                lines.append(f"| {prefix}{key} | {display_val} |")
        lines.append("")

    return "\n".join(lines)


def resolve_previous_period(
    date_mode: str,
    valid_dates: list,
    selected_date=None,
    start_date=None,
    end_date=None,
    weekend_bucket=None,
    week_bucket=None,
    week_buckets=None,
    weekend_buckets=None,
) -> Optional[tuple]:
    """
    按当前时间维度解析「上一周期」提取参数。
    返回 (extract_data_for_report 的 kwargs 片段, 对比周期说明文案)，无上一周期则 None。
    """
    week_buckets = week_buckets or []
    weekend_buckets = weekend_buckets or []

    if date_mode == "single":
        if selected_date is None:
            return None
        prev_date = selected_date - timedelta(days=1)
        if valid_dates and prev_date not in valid_dates:
            earlier = [d for d in valid_dates if d < selected_date]
            if not earlier:
                return None
            prev_date = max(earlier)
        label = f"{prev_date.strftime('%Y-%m-%d')}（上一日）"
        return ({"selected_date": prev_date}, label)

    if date_mode == "week":
        if not week_bucket or not week_buckets:
            return None
        idx = week_bucket.get("bucket_index", 0)
        if idx <= 0:
            return None
        prev_bucket = week_buckets[idx - 1]
        wn = prev_bucket.get("week_number")
        if prev_bucket.get("start") and prev_bucket.get("end"):
            label = (
                f"{prev_bucket.get('raw_label', 'Week')}（"
                f"{prev_bucket['start'].strftime('%Y-%m-%d')} ~ "
                f"{prev_bucket['end'].strftime('%Y-%m-%d')}）（上一周）"
            )
        elif wn is not None:
            label = f"Week {wn}（上一周）"
        else:
            label = f"{prev_bucket.get('raw_label', '上一周')}（上一周）"
        return ({"week_bucket": prev_bucket}, label)

    if date_mode == "weekend":
        if not weekend_bucket or not weekend_buckets:
            return None
        idx = weekend_bucket.get("bucket_index", 0)
        if idx <= 0:
            return None
        prev_bucket = weekend_buckets[idx - 1]
        label = (
            f"{prev_bucket['start'].strftime('%Y-%m-%d')} ~ "
            f"{prev_bucket['end'].strftime('%Y-%m-%d')}（上一周末）"
        )
        return ({"week_bucket": prev_bucket}, label)

    if start_date is None or end_date is None:
        return None
    period_days = (end_date - start_date).days + 1
    prev_end = start_date - timedelta(days=1)
    prev_start = prev_end - timedelta(days=period_days - 1)
    if period_days == 1:
        label = f"{prev_start.strftime('%Y-%m-%d')}（上一日）"
    elif period_days == 7:
        label = f"{prev_start.strftime('%Y-%m-%d')} 至 {prev_end.strftime('%Y-%m-%d')}（上一周）"
    else:
        label = f"{prev_start.strftime('%Y-%m-%d')} 至 {prev_end.strftime('%Y-%m-%d')}（上一区间）"
    return ({"start_date": prev_start, "end_date": prev_end}, label)


def _summarize_channels_for_comparison(extracted_data: dict) -> dict:
    shopify_row = _get_shopify_row(extracted_data)
    summaries = {}
    for sheet_name, data in extracted_data.items():
        if not isinstance(data, list) or not data:
            continue
        row = _pick_summary_row(data)
        summary = _extract_channel_summary(sheet_name, row, shopify_row)
        if "shopify" in str(sheet_name).lower():
            summary["spend"] = _find_first_numeric(row, ["Ad Spent_Total"])
            summary["roas"] = _find_first_numeric(row, ["MER", "ROI", "ROAS"])
            summary["orders"] = _find_first_numeric(row, ["订单", "Oders", "Orders"])
            summary["sales"] = _find_first_numeric(row, ["Total Sales", "销售额"])
        summaries[sheet_name] = summary
    return summaries


def _format_metric_delta(current, previous) -> str:
    if current is None or previous is None:
        return "-"
    delta = float(current) - float(previous)
    if previous == 0:
        pct_text = "N/A"
    else:
        pct = delta / abs(float(previous)) * 100
        pct_text = f"{pct:+.1f}%"
    sign = "+" if delta >= 0 else ""
    if delta == int(delta):
        return f"{sign}{int(delta):,} ({pct_text})"
    return f"{sign}{delta:,.2f} ({pct_text})"


def build_period_comparison_markdown(
    current_data: dict,
    previous_data: Optional[dict],
    current_label: str,
    previous_label: str,
) -> str:
    """生成环比对比表格（本周期 vs 上一周期）。"""
    lines = [
        "## 二、环比对比（Period-over-Period）",
        "",
        f"**本周期**：{current_label}  ·  **对比周期**：{previous_label or '—'}",
        "",
    ]
    if not previous_data:
        lines.append("⚠️ 未找到上一周期数据，本次跳过环比分析。")
        return "\n".join(lines)

    current_summaries = _summarize_channels_for_comparison(current_data)
    previous_summaries = _summarize_channels_for_comparison(previous_data)
    if not current_summaries:
        lines.append("⚠️ 本周期无有效渠道数据，无法生成环比表。")
        return "\n".join(lines)

    lines.extend([
        "### 2.1 核心指标环比",
        "",
        "| 渠道/账号 | 本周期消耗 | 上周期消耗 | 消耗变化 | 本周期 ROAS | 上周期 ROAS | ROAS 变化 | 订单变化 |",
        "|----------|-----------|-----------|---------|------------|------------|----------|---------|",
    ])

    for channel, cur in current_summaries.items():
        prev = previous_summaries.get(channel, {})
        lines.append(
            "| {channel} | {c_spend} | {p_spend} | {d_spend} | {c_roas} | {p_roas} | {d_roas} | {d_orders} |".format(
                channel=channel,
                c_spend=_fmt_metric(cur["spend"]) if cur.get("spend") is not None else "-",
                p_spend=_fmt_metric(prev.get("spend")) if prev.get("spend") is not None else "-",
                d_spend=_format_metric_delta(cur.get("spend"), prev.get("spend")),
                c_roas=_fmt_metric(cur["roas"]) if cur.get("roas") is not None else "-",
                p_roas=_fmt_metric(prev.get("roas")) if prev.get("roas") is not None else "-",
                d_roas=_format_metric_delta(cur.get("roas"), prev.get("roas")),
                d_orders=_format_metric_delta(cur.get("orders"), prev.get("orders")),
            )
        )

    shopify_key = next((k for k in current_summaries if "shopify" in k.lower()), None)
    if shopify_key and shopify_key in previous_summaries:
        shopify_cur = current_summaries[shopify_key]
        shopify_prev = previous_summaries[shopify_key]
        lines.extend([
            "",
            "### 2.1.1 Shopify 大盘环比",
            "",
            f"- 销售额：{_fmt_metric(shopify_cur.get('sales')) if shopify_cur.get('sales') is not None else '-'} "
            f"→ 上周期 {_fmt_metric(shopify_prev.get('sales')) if shopify_prev.get('sales') is not None else '-'} "
            f"（{_format_metric_delta(shopify_cur.get('sales'), shopify_prev.get('sales'))}）",
            f"- 总广告消耗：{_format_metric_delta(shopify_cur.get('spend'), shopify_prev.get('spend'))}",
            f"- MER/ROAS：{_format_metric_delta(shopify_cur.get('roas'), shopify_prev.get('roas'))}",
        ])

    missing_prev = [
        ch for ch in current_summaries
        if ch not in previous_summaries or not previous_summaries.get(ch)
    ]
    if missing_prev:
        lines.extend([
            "",
            f"*注：以下渠道上周期无对应数据：{', '.join(missing_prev)}*",
        ])

    return "\n".join(lines)


def build_user_content(extracted_data: dict, date_info: str, date_mode: str) -> str:
    lines = [f"以下是【{date_info}】期间，各渠道/Sheet 的原始投放数据（已剔除人工总结/分析列）：\n"]
    lines.append(build_data_digest(extracted_data))

    for sheet_name, data in extracted_data.items():
        lines.append(f"\n## 渠道/Sheet：{sheet_name}\n")
        if isinstance(data, str):
            lines.append(data)
        else:
            try:
                json_str = json.dumps(data, ensure_ascii=False, indent=2, default=str)
                lines.append(f"```json\n{json_str}\n```")
            except Exception as e:
                lines.append(f"（该渠道数据序列化失败：{e}）")

    if date_mode == "range":
        lines.append(
            "\n\n请注意：以上每个渠道的数据均为按日期排列的列表（时间序列），"
            "请结合日期变化分析趋势。"
        )
    elif date_mode == "week":
        lines.append(
            "\n\n请注意：以上数据来自各 Sheet 中日期列为 **Week** 的周汇总行，"
            "已是整周合并指标，请勿与逐日数据重复相加。"
        )
    elif date_mode == "weekend":
        lines.append(
            "\n\n请注意：以上数据包含「周末三日」汇总行，以及该周末 Fri~Sun 的逐日明细（如有）。"
            "汇总行已含三天合并指标，请勿与逐日数据重复相加。"
        )

    return "\n".join(lines)


def _safe_report_filename(meta: dict, ext: str) -> str:
    date_part = meta.get("date_info", "").replace(" ", "_").replace("至", "to")
    return f"{meta.get('report_type', 'report')}_{date_part}.{ext}"


def build_full_markdown(report: str, meta: dict) -> str:
    """将 AI 输出整合为带封面信息的完整 Markdown 文档。"""
    generated_at = meta.get("generated_at") or datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"""# 跨境电商广告投放分析报告

| 项目 | 内容 |
|------|------|
| 报告类型 | {meta.get('report_type', '')} |
| 数据区间 | {meta.get('date_info', '')} |
| 生成时间 | {generated_at} |

---

{report}
"""


def build_full_html(report: str, meta: dict) -> str:
    """将报告转为可打印的 HTML 文档。"""
    md_doc = build_full_markdown(report, meta)
    body_lines = []
    for line in md_doc.split("\n"):
        stripped = line.strip()
        if stripped.startswith("# "):
            body_lines.append(f"<h1>{stripped[2:]}</h1>")
        elif stripped.startswith("## "):
            body_lines.append(f"<h2>{stripped[3:]}</h2>")
        elif stripped.startswith("### "):
            body_lines.append(f"<h3>{stripped[4:]}</h3>")
        elif stripped.startswith("|") and "---" not in stripped:
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            body_lines.append(
                "<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"
            )
        elif stripped == "---":
            body_lines.append("<hr>")
        elif stripped.startswith("- "):
            body_lines.append(f"<li>{stripped[2:]}</li>")
        elif stripped:
            body_lines.append(f"<p>{stripped}</p>")

    body_html = "\n".join(body_lines)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{meta.get('report_type', '报告')} - {meta.get('date_info', '')}</title>
<style>
  body {{ font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         max-width: 900px; margin: 40px auto; padding: 0 24px; line-height: 1.7; color: #222; }}
  h1 {{ border-bottom: 2px solid #1f77b4; padding-bottom: 8px; }}
  h2 {{ color: #1f77b4; margin-top: 28px; }}
  h3 {{ color: #444; }}
  table {{ border-collapse: collapse; width: 100%; margin: 16px 0; }}
  td, th {{ border: 1px solid #ddd; padding: 8px 12px; text-align: left; }}
  hr {{ border: none; border-top: 1px solid #eee; margin: 24px 0; }}
  li {{ margin: 4px 0; }}
</style>
</head>
<body>
{body_html}
</body>
</html>"""


def build_docx_bytes(report: str, meta: dict) -> bytes:
    """将报告转为 Word (.docx) 字节流。"""
    from docx import Document

    doc = Document()
    doc.add_heading("跨境电商广告投放分析报告", 0)
    doc.add_paragraph(f"报告类型：{meta.get('report_type', '')}")
    doc.add_paragraph(f"数据区间：{meta.get('date_info', '')}")
    doc.add_paragraph(
        f"生成时间：{meta.get('generated_at') or datetime.now().strftime('%Y-%m-%d %H:%M')}"
    )
    doc.add_paragraph("")

    for line in report.split("\n"):
        text = line.rstrip()
        if not text:
            continue
        if text.startswith("## "):
            doc.add_heading(text[3:], level=1)
        elif text.startswith("### "):
            doc.add_heading(text[4:], level=2)
        elif text.startswith("- "):
            doc.add_paragraph(text[2:], style="List Bullet")
        elif re.match(r"^\d+\.\s", text):
            doc.add_paragraph(re.sub(r"^\d+\.\s", "", text), style="List Number")
        else:
            clean = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
            doc.add_paragraph(clean)

    buf = BytesIO()
    doc.save(buf)
    return buf.getvalue()


EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")

GMAIL_SMTP = {
    "host": "smtp.gmail.com",
    "port": 587,
    "use_tls": True,
    "use_ssl": False,
}


def _sanitize_smtp_password(password: str) -> str:
    """去除授权码中误粘贴的空格（QQ/Gmail 授权码常见）。"""
    return re.sub(r"\s+", "", (password or "").strip())


def _smtp_auth_error_hint(cfg: dict, exc: Exception) -> str:
    host = cfg.get("host", "").lower()
    server_msg = ""
    if getattr(exc, "args", None):
        server_msg = " ".join(str(a) for a in exc.args if a)
    base = "❌ SMTP 登录失败。"
    if server_msg:
        base += f"\n\n服务器回复：`{server_msg}`"
    if "gmail.com" in host:
        base += (
            "\n\n**Gmail 请确认：**\n"
            "1. 已开启两步验证，并使用 **应用专用密码**（16 位，不是 Google 登录密码）\n"
            "2. 发件邮箱与登录账号一致\n"
            "3. 应用专用密码获取：[Google 账号 → 安全 → 应用专用密码](https://myaccount.google.com/apppasswords)"
        )
    else:
        base += "\n\n请检查 Gmail 邮箱与应用专用密码是否正确。"
    return base


def _is_valid_email(addr: str) -> bool:
    return bool(EMAIL_RE.match((addr or "").strip()))


def _parse_email_list(text: str) -> list:
    addrs = []
    for part in re.split(r"[,;\s\n]+", text or ""):
        addr = part.strip()
        if addr and _is_valid_email(addr):
            addrs.append(addr)
    return addrs


def _get_smtp_config(override: dict = None) -> dict:
    """读取 SMTP 配置：优先使用界面传入，其次 Streamlit Secrets。"""
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

    use_ssl = override.get("use_ssl")
    if use_ssl is None:
        use_ssl = _secret("email", "use_ssl", default="").lower() == "true"
    use_tls = override.get("use_tls")
    if use_tls is None:
        use_tls = _secret("email", "use_tls", default="true").lower() != "false"

    if port == 465 and not use_ssl:
        use_ssl = True
        use_tls = False

    if not host or not user or not password or not from_addr:
        return {}
    return {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "from_addr": from_addr,
        "from_name": override.get("from_name") or _secret("email", "from_name") or "跨境电商广告分析报告",
        "use_tls": use_tls and not use_ssl,
        "use_ssl": use_ssl,
    }


def _smtp_connect_and_send(host: str, port: int, use_ssl: bool, use_tls: bool,
                           user: str, password: str, msg: MIMEMultipart) -> None:
    context = ssl.create_default_context()
    if use_ssl:
        with smtplib.SMTP_SSL(host, port, timeout=30, context=context) as smtp:
            smtp.ehlo()
            smtp.login(user, password)
            smtp.send_message(msg)
        return
    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.ehlo()
        if use_tls:
            smtp.starttls(context=context)
            smtp.ehlo()
        smtp.login(user, password)
        smtp.send_message(msg)


def _smtp_login_and_send(cfg: dict, msg: MIMEMultipart) -> None:
    user = cfg["user"].strip()
    password = _sanitize_smtp_password(cfg["password"])
    attempts = [(cfg["host"], cfg["port"], cfg["use_ssl"], cfg["use_tls"])]

    host_lower = cfg["host"].lower()
    if "gmail.com" in host_lower and not cfg["use_ssl"]:
        attempts.append((cfg["host"], 465, True, False))

    last_auth_err = None
    for host, port, use_ssl, use_tls in attempts:
        try:
            _smtp_connect_and_send(host, port, use_ssl, use_tls, user, password, msg)
            return
        except smtplib.SMTPAuthenticationError as e:
            last_auth_err = e
            continue
    if last_auth_err:
        raise last_auth_err
    _smtp_connect_and_send(
        cfg["host"], cfg["port"], cfg["use_ssl"], cfg["use_tls"], user, password, msg
    )


def _build_email_attachment(report: str, meta: dict, attachment_format: str):
    base_name = _safe_report_filename(meta, "").rstrip(".")
    if attachment_format == "html":
        return (
            f"{base_name}.html",
            build_full_html(report, meta).encode("utf-8"),
            "text/html",
        )
    if attachment_format == "md":
        return (
            f"{base_name}.md",
            build_full_markdown(report, meta).encode("utf-8"),
            "text/markdown",
        )
    return (
        f"{base_name}.docx",
        build_docx_bytes(report, meta),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


def send_report_email(
    to_addrs: list,
    report: str,
    meta: dict,
    attachment_format: str = "docx",
    smtp_override: dict = None,
) -> tuple:
    """通过 Gmail SMTP 发送报告附件。返回 (成功与否, 提示信息)。"""
    cfg = _get_smtp_config(smtp_override)
    if not cfg:
        return False, (
            "未配置 Gmail。请在左侧侧边栏「Gmail 发信配置」中填写发件邮箱与应用专用密码。"
        )
    if not to_addrs:
        return False, "请填写至少一个有效的收件人邮箱。"

    filename, payload, mime_type = _build_email_attachment(report, meta, attachment_format)
    subject = f"{meta.get('report_type', '分析报告')} - {meta.get('date_info', '')}"
    generated_at = meta.get("generated_at") or datetime.now().strftime("%Y-%m-%d %H:%M")

    msg = MIMEMultipart()
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((cfg["from_name"], cfg["from_addr"]))
    msg["To"] = ", ".join(to_addrs)

    body = f"""您好，

附件为自动生成的跨境电商广告投放分析报告。

报告类型：{meta.get('report_type', '')}
数据区间：{meta.get('date_info', '')}
生成时间：{generated_at}

此邮件由分析系统自动发送，请勿直接回复。
"""
    msg.attach(MIMEText(body, "plain", "utf-8"))

    attachment = MIMEApplication(payload, Name=filename)
    attachment.add_header("Content-Disposition", "attachment", filename=("utf-8", "", filename))
    attachment.add_header("Content-Type", mime_type)
    msg.attach(attachment)

    try:
        _smtp_login_and_send(cfg, msg)
        return True, f"✅ 报告已发送至：{', '.join(to_addrs)}"
    except smtplib.SMTPAuthenticationError as e:
        return False, _smtp_auth_error_hint(cfg, e)
    except smtplib.SMTPException as e:
        return False, f"❌ 邮件发送失败（SMTP）：{e}"
    except OSError as e:
        return False, f"❌ 无法连接邮件服务器 {cfg['host']}:{cfg['port']}：{e}"
    except Exception as e:
        return False, f"❌ 邮件发送失败：{e}"


def _require_ascii(value: str, field_name: str) -> str:
    """API Key / Base URL / 模型名必须为 ASCII，防止误粘贴中文说明。"""
    value = (value or "").strip()
    if not value:
        return value
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        raise ValueError(
            f"「{field_name}」包含非英文字符，请只填写 Key / URL / 模型名，"
            f"不要粘贴中文说明文字。"
        )
    return value


def _validate_llm_credentials(api_key: str, base_url: str) -> None:
    """在发起请求前检测常见 Key / URL 错配。"""
    key = api_key.strip().lower()

    if key.startswith("apify_api") or key.startswith("apify_ap"):
        raise ValueError(
            "检测到 **Apify API Key**，不能用于 AI 报告生成。"
            "请检查 Gemini API Key 配置。"
        )


def _sanitize_api_key(api_key: str) -> str:
    clean = sanitize_gemini_api_key(api_key)
    if clean:
        return clean
    compact = re.sub(r"\s+", "", (api_key or "").strip())
    if re.match(r"(?i)^aq\.", compact) and len(compact) >= 10:
        return f"AQ.{compact[3:]}" if not compact.upper().startswith("AQ.") else compact
    return ""


def _resolve_gemini_key_for_report() -> str:
    """生成报告前解析 Key：侧边栏 / Secrets / AQ. 兜底。"""
    clean = get_gemini_api_key()
    if clean:
        return clean
    for raw in _gemini_key_raw_candidates():
        fallback = _sanitize_api_key(raw)
        if fallback:
            return fallback
    return ""


def _raw_gemini_key_candidates() -> list[str]:
    return _gemini_key_raw_candidates()


def _gemini_key_error_hint(raw_key: str = "") -> str:
    """当 Key 无法解析时，生成可操作的中文提示。"""
    sidebar_raw = str(st.session_state.get(SUITE_GEMINI_API_KEY, "")).strip()
    secret_raw = str(
        suite_secret("gemini", "api_key") or suite_secret("google", "api_key")
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
    if secret_raw and not sanitize_gemini_api_key(secret_raw):
        return (
            "Secrets 中的 [gemini] api_key 格式无效（需 AIza 或 AQ. 开头）。"
            "请检查 Streamlit Cloud → Settings → Secrets，或改在侧边栏直接填写。"
        )
    if sidebar_raw and not sanitize_gemini_api_key(sidebar_raw):
        return (
            "侧边栏 Gemini API Key 格式无效。"
            f"当前输入：{describe_gemini_key_input(sidebar_raw)}。"
            "请从 [Google AI Studio](https://aistudio.google.com/apikey) 复制完整 Key"
            "（新版以 **AQ.** 开头，旧版以 **AIzaSy** 开头）。"
        )
    return (
        "Gemini API Key 格式无效。"
        f"侧栏：{describe_gemini_key_input(sidebar_raw)}；"
        f"Secrets：{describe_gemini_key_input(secret_raw)}。"
        f"（当前部署版本应为 {GEMINI_KEY_VALIDATION_VERSION}；若你仍看到「以 AIza 开头」旧提示，"
        "说明 Cloud 未更新，请到 Streamlit → Manage app → Reboot。）"
        "请从 [Google AI Studio](https://aistudio.google.com/apikey) 复制完整 Key"
        "（新版以 **AQ.** 开头，旧版以 **AIzaSy** 开头）。"
    )


def _format_api_error(status: int, detail: str, api_key: str, base_url: str) -> str:
    """将 HTTP 错误转为更易读的中文提示。"""
    detail_lower = detail.lower()
    base_lower = base_url.strip().lower()
    key_hint = ""

    if "apify" in detail_lower or api_key.strip().lower().startswith("apify"):
        key_hint = (
            "\n\n**原因**：API Key 配置有误，请检查 Gemini Key。"
        )
    elif status == 429:
        if "generativelanguage.googleapis.com" in base_lower or "quota" in detail_lower:
            key_hint = (
                "\n\n**原因**：Google Gemini 免费额度已用完或未开通（limit: 0 表示当前无可用配额）。\n\n"
                "**可选方案**：\n"
                "1. 在 [Google AI Studio](https://aistudio.google.com/) 关联账单\n"
                "2. 将模型改为 `gemini-2.5-flash-lite` 后重试\n"
                "3. 查看用量：[ai.dev/rate-limit](https://ai.dev/rate-limit)"
            )
        else:
            key_hint = "\n\n**原因**：请求频率或配额超限，请稍后重试或更换模型。"
    elif status == 401:
        key_hint = "\n\n**原因**：Gemini API Key 无效，请核对侧边栏配置。"
    elif status == 404:
        key_hint = "\n\n**原因**：模型名称可能不正确。"
    elif status in (502, 503, 529):
        key_hint = (
            "\n\n**原因**：Gemini 服务暂时不可用（高峰期常见，通常几分钟内恢复）。\n\n"
            "**建议**：\n"
            "1. 等待 1~2 分钟后点击「生成 AI 分析报告」重试\n"
            "2. 改用 `gemini-2.5-flash-lite` 等轻量模型"
        )

    return f"❌ API 请求失败 (HTTP {status})：{detail}{key_hint}"


def _request_chat_completion(
    client,
    url: str,
    headers: dict,
    payload: dict,
    retry_delays: tuple,
) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    for attempt in range(len(retry_delays) + 1):
        try:
            resp = client.post(url, content=body, headers=headers)
            if resp.status_code in (429, 502, 503, 529) and attempt < len(retry_delays):
                time.sleep(retry_delays[attempt])
                continue
            resp.raise_for_status()
            return resp.json()
        except httpx.TimeoutException:
            if attempt < len(retry_delays):
                time.sleep(retry_delays[attempt])
                continue
            raise
    raise RuntimeError("API 多次重试后仍失败")


def _messages_to_gemini_native(messages: list) -> tuple[str, list]:
    """OpenAI messages → Gemini native systemInstruction + contents。"""
    system_parts: list[str] = []
    contents: list[dict] = []
    for msg in messages:
        role = msg.get("role")
        text = str(msg.get("content", "") or "")
        if role == "system":
            if text:
                system_parts.append(text)
        elif role == "assistant":
            contents.append({"role": "model", "parts": [{"text": text}]})
        else:
            contents.append({"role": "user", "parts": [{"text": text}]})
    return "\n\n".join(system_parts), contents


def _gemini_native_response_to_openai_shape(data: dict) -> dict:
    """将 generateContent 响应转为与 chat/completions 兼容的结构。"""
    if "error" in data:
        err = data["error"]
        message = err.get("message", str(err))
        code = err.get("code", 400)
        raise httpx.HTTPStatusError(
            message,
            request=httpx.Request("POST", GEMINI_NATIVE_BASE),
            response=httpx.Response(code, text=json.dumps(data, ensure_ascii=False)),
        )

    candidates = data.get("candidates") or []
    if not candidates:
        raise RuntimeError("Gemini 未返回 candidates，请稍后重试。")

    candidate = candidates[0]
    parts = candidate.get("content", {}).get("parts") or []
    text = "".join(str(part.get("text", "")) for part in parts)
    finish_reason = str(candidate.get("finishReason", "") or "").lower()
    return {
        "choices": [{
            "message": {"content": text},
            "finish_reason": "length" if finish_reason == "max_tokens" else finish_reason,
        }],
    }


def _request_gemini_generate_content(
    client,
    api_key: str,
    model_name: str,
    messages: list,
    temperature: float,
    max_output_tokens: int,
    retry_delays: tuple,
) -> dict:
    """调用 Gemini 原生 generateContent（兼容 AQ. Auth Key）。"""
    clean_key = _sanitize_api_key(api_key)
    system_prompt, contents = _messages_to_gemini_native(messages)
    if not contents:
        contents = [{"role": "user", "parts": [{"text": "请根据上述数据生成分析报告。"}]}]

    url = f"{GEMINI_NATIVE_BASE}/models/{model_name}:generateContent"
    headers = build_gemini_http_headers(clean_key)
    payload: dict = {
        "contents": contents,
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_output_tokens,
        },
    }
    if system_prompt:
        payload["systemInstruction"] = {"parts": [{"text": system_prompt}]}

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    for attempt in range(len(retry_delays) + 1):
        try:
            resp = client.post(url, content=body, headers=headers)
            if resp.status_code in (429, 502, 503, 529) and attempt < len(retry_delays):
                time.sleep(retry_delays[attempt])
                continue
            if resp.status_code >= 400:
                detail = resp.text[:500]
                raise httpx.HTTPStatusError(
                    detail,
                    request=resp.request,
                    response=resp,
                )
            return _gemini_native_response_to_openai_shape(resp.json())
        except httpx.TimeoutException:
            if attempt < len(retry_delays):
                time.sleep(retry_delays[attempt])
                continue
            raise
    raise RuntimeError("API 多次重试后仍失败")


def _request_llm_completion(
    client,
    api_key: str,
    base_url: str,
    model_name: str,
    messages: list,
    temperature: float,
    max_output_tokens: int,
    retry_delays: tuple,
) -> dict:
    """统一走 Gemini 原生 generateContent，兼容 AQ. 与 AIza 两类 Key。"""
    return _request_gemini_generate_content(
        client,
        api_key,
        model_name,
        messages,
        temperature,
        max_output_tokens,
        retry_delays,
    )


REPORT_SECTIONS = [
    {
        "title": "## 一、大盘纵览（Executive Summary）",
        "instruction": (
            "请**只写第一章**「大盘纵览（Executive Summary）」。\n"
            "- 引用 Shopify 总销售额、总广告消耗、订单数、MER/ROI 等**具体数字**\n"
            "- 计算并写出大盘 ROAS（销售额÷广告消耗）\n"
            "- 3~5 条要点，评估本周期整体盈亏与体量\n"
            "**禁止**写第二、三、四章。"
        ),
        "min_chars": 180,
    },
    {
        "id": "comparison",
        "title": "## 二、环比对比（Period-over-Period）",
        "instruction": (
            "环比表格已由系统自动生成（见下方）。请**只写**「### 2.2 环比洞察与诊断」小节：\n"
            "- 指出消耗/ROAS/订单变化最大的 3 个渠道，必须引用表格中的**具体数字与涨跌幅度**\n"
            "- 判断整体环比变好、变差或结构性分化\n"
            "- 2~3 条可能原因假设（基于数据，勿编造）\n"
            "**禁止**重复输出表格；**禁止**写其他章节。"
        ),
        "min_chars": 160,
        "skip_title": True,
    },
    {
        "id": "channel",
        "title": "## 三、渠道表现拆解",
        "instruction": (
            "渠道数据表格已由系统自动生成（见下方），你**不要重复输出表格**。\n"
            "请**只写**「### 3.3 渠道洞察与诊断」小节，包含：\n"
            "- 3~5 条 bullet：**增长引擎**（渠道名 + 具体 ROAS/消耗数字 + 原因）\n"
            "- 3~5 条 bullet：**拖后腿渠道**（渠道名 + 具体问题数字 + 风险）\n"
            "- 2~3 条 cross-channel 对比结论（如 Meta vs Google vs AppLovin）\n"
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
            "  **【渠道/账号】** 动作描述（含量化幅度，如 +20% / -500 USD / 暂停 XX 广告组）\n"
            "- 覆盖：加预算、减预算、暂停、放量 等类型\n"
            "- 可结合第二章环比结论给出针对性动作\n"
            "**禁止**写第一、二、三章。"
        ),
        "min_chars": 280,
    },
]


def _normalize_section_output(title: str, content: str) -> str:
    text = (content or "").strip()
    if not text:
        return f"{title}\n\n（该章节生成失败，请重试。）"
    if not text.startswith("##"):
        text = f"{title}\n\n{text}"
    elif title.split("（")[0] not in text.split("\n")[0]:
        text = f"{title}\n\n{text}"
    return text


def generate_report(
    api_key: str,
    base_url: str,
    model_name: str,
    system_prompt: str,
    user_content: str,
    temperature: float = 0.5,
    extracted_data: dict = None,
    previous_extracted_data: dict = None,
    current_period_label: str = "",
    previous_period_label: str = "",
    report_sections: Optional[list] = None,
) -> str:
    """
    分四节依次调用大模型，再合并为完整报告，避免单次输出被截断。
    """
    read_timeout = 300.0
    connect_timeout = 60.0
    retry_delays = (5, 15, 30)
    max_output_tokens = 4096

    if not api_key:
        return "❌ 错误：请先在左侧侧边栏填写 Gemini API Key。"

    api_key = _require_ascii(_sanitize_api_key(api_key), "API Key")
    base_url = _require_ascii(base_url.rstrip("/"), "Base URL")
    model_name = _require_ascii(model_name, "模型名称")
    _validate_llm_credentials(api_key, base_url)

    timeout = httpx.Timeout(read_timeout, connect=connect_timeout)
    report_temperature = min(temperature, 0.4)
    sections_plan = report_sections or REPORT_SECTIONS

    sections_out = []
    comparison_tables = build_period_comparison_markdown(
        extracted_data or {},
        previous_extracted_data,
        current_period_label,
        previous_period_label,
    )
    try:
        with httpx.Client(timeout=timeout) as client:
            for section in sections_plan:
                if section.get("id") == "comparison":
                    if "跳过环比" in comparison_tables or "未找到上一周期" in comparison_tables:
                        sections_out.append(comparison_tables)
                        continue
                    section_user = (
                        f"{user_content}\n\n---\n已生成的环比表格：\n{comparison_tables}\n\n"
                        f"{section['instruction']}"
                    )
                elif section.get("id") == "channel" and extracted_data:
                    channel_tables = build_channel_tables_markdown(extracted_data)
                    section_user = (
                        f"{user_content}\n\n---\n已生成的渠道表格：\n{channel_tables}\n\n"
                        f"{section['instruction']}"
                    )
                else:
                    section_user = f"{user_content}\n\n---\n{section['instruction']}"

                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": section_user},
                ]

                content = ""
                for attempt in range(2):
                    data = _request_llm_completion(
                        client,
                        api_key,
                        base_url,
                        model_name,
                        messages,
                        report_temperature,
                        max_output_tokens,
                        retry_delays,
                    )
                    choice = data.get("choices", [{}])[0]
                    content = choice.get("message", {}).get("content") or ""
                    finish_reason = choice.get("finish_reason", "")

                    if (
                        content
                        and len(content.strip()) >= section["min_chars"]
                        and finish_reason != "length"
                    ):
                        break

                    if attempt == 0:
                        messages.append({
                            "role": "assistant",
                            "content": content or "（空）",
                        })
                        messages.append({
                            "role": "user",
                            "content": (
                                "内容太短或不完整。请重新撰写本节，"
                                "必须引用上方数据中的**全部关键数字**，写得更详细、更完整。"
                            ),
                        })

                if section.get("id") == "comparison" and previous_extracted_data:
                    analysis = content.strip()
                    if analysis.startswith("##"):
                        analysis = analysis.split("\n", 1)[-1].strip()
                    sections_out.append(
                        comparison_tables + "\n\n" + (
                            analysis if analysis.startswith("###")
                            else "### 2.2 环比洞察与诊断\n\n" + analysis
                        )
                    )
                elif section.get("id") == "channel" and extracted_data:
                    analysis = content.strip()
                    if analysis.startswith("##"):
                        analysis = analysis.split("\n", 1)[-1].strip()
                    sections_out.append(
                        channel_tables + "\n\n" + (
                            analysis if analysis.startswith("###")
                            else "### 3.3 渠道洞察与诊断\n\n" + analysis
                        )
                    )
                else:
                    sections_out.append(
                        _normalize_section_output(section["title"], content)
                    )

        return "\n\n".join(sections_out)

    except ValueError as e:
        return f"❌ {e}"
    except httpx.TimeoutException:
        partial = "\n\n".join(sections_out)
        if partial:
            return partial + (
                "\n\n---\n⚠️ *后续章节因超时未完成，请缩小日期范围或换用 deepseek-chat 后重试。*"
            )
        return (
            "❌ 模型响应超时。\n\n"
            "**建议**：换用 deepseek-chat / gpt-4o-mini，或缩小数据范围后重试。"
        )
    except httpx.HTTPStatusError as e:
        detail = e.response.text[:500] if e.response is not None else str(e)
        status = e.response.status_code if e.response is not None else 0
        return _format_api_error(status, detail, api_key, base_url)
    except Exception as e:
        return f"❌ 调用大模型 API 时发生错误：{e}\n\n请检查 API Key、Base URL、模型名称是否正确。"


def build_followup_system_prompt() -> str:
    return """# 角色
你是跨境电商 Meta / Google / AppLovin 投放数据分析助手。用户已基于同一批 Excel 数据收到一份 AI 分析报告，现在追问具体问题。

# 要求
- 必须结合提供的**数据摘要**与**已生成报告**作答，引用具体数字（消耗、ROAS、ROI、订单等）
- 回答简洁、可执行，使用简体中文
- 数据不足以回答时，明确说明缺少哪些字段或渠道
- **不要**整段复述报告，不要编造未提供的数据
- 若用户要求与报告矛盾，以原始数据为准并说明差异
"""


def _build_followup_context(
    extracted_data: dict,
    report_text: str,
    date_info: str,
    report_type: str,
) -> str:
    digest = build_data_digest(extracted_data)
    report_excerpt = (report_text or "").strip()
    if len(report_excerpt) > 12000:
        report_excerpt = report_excerpt[:12000] + "\n\n…（报告后续内容已截断，必要时请用户缩小问题范围）"
    return (
        f"【报告类型】{report_type}\n"
        f"【数据区间】{date_info}\n\n"
        f"【数据摘要】\n{digest}\n\n"
        f"【已生成的分析报告（供参考，勿整段复述）】\n{report_excerpt}"
    )


def answer_followup_question(
    api_key: str,
    base_url: str,
    model_name: str,
    temperature: float,
    extracted_data: dict,
    report_text: str,
    date_info: str,
    report_type: str,
    question: str,
    history: Optional[list] = None,
    system_prompt: Optional[str] = None,
) -> str:
    """基于同一批数据与已生成报告，回答用户的追问（单次 API 调用）。"""
    if not api_key:
        return "❌ 请先在侧边栏填写 Gemini API Key。"
    if not (question or "").strip():
        return "❌ 请输入问题。"

    api_key = _require_ascii(_sanitize_api_key(api_key), "API Key")
    base_url = _require_ascii(base_url.rstrip("/"), "Base URL")
    model_name = _require_ascii(model_name, "模型名称")
    _validate_llm_credentials(api_key, base_url)

    context_block = _build_followup_context(
        extracted_data, report_text, date_info, report_type
    )
    messages = [{"role": "system", "content": system_prompt or build_followup_system_prompt()}]
    history = history or []

    if not history:
        messages.append({
            "role": "user",
            "content": f"{context_block}\n\n【我的问题】\n{question.strip()}",
        })
    else:
        first = True
        for turn in history:
            role = turn.get("role")
            content = turn.get("content", "")
            if role not in ("user", "assistant"):
                continue
            if role == "user" and first:
                messages.append({
                    "role": "user",
                    "content": f"{context_block}\n\n【我的问题】\n{content}",
                })
                first = False
            else:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": question.strip()})

    timeout = httpx.Timeout(180.0, connect=60.0)

    try:
        with httpx.Client(timeout=timeout) as client:
            data = _request_llm_completion(
                client,
                api_key,
                base_url,
                model_name,
                messages,
                min(temperature, 0.6),
                2048,
                (5, 15),
            )
        content = data.get("choices", [{}])[0].get("message", {}).get("content") or ""
        return content.strip() or "（模型未返回内容，请重试。）"
    except ValueError as e:
        return f"❌ {e}"
    except httpx.TimeoutException:
        return "❌ 回答超时，请缩短问题或稍后重试。"
    except httpx.HTTPStatusError as e:
        detail = e.response.text[:500] if e.response is not None else str(e)
        status = e.response.status_code if e.response is not None else 0
        return _format_api_error(status, detail, api_key, base_url)
    except Exception as e:
        return f"❌ 调用大模型时发生错误：{e}"


def call_llm(api_key: str, base_url: str, model_name: str,
              system_prompt: str, user_content: str, temperature: float = 0.5,
              extracted_data: dict = None) -> str:
    """兼容入口：委托给分节生成。"""
    return generate_report(
        api_key, base_url, model_name, system_prompt, user_content, temperature,
        extracted_data=extracted_data,
    )


# ============================================================
# Streamlit Cloud Secrets（可选）
# ============================================================

def _secret(*keys, default=""):
    """从 Streamlit Cloud「Settings → Secrets」读取配置，本地无 secrets 时返回 default。"""
    try:
        node = st.secrets
        for key in keys:
            node = node[key]
        if node is None:
            return default
        return str(node).strip()
    except (KeyError, TypeError, AttributeError, FileNotFoundError):
        return default


# ============================================================
# Google Gemini 配置（唯一大模型）
# ============================================================

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
GEMINI_NATIVE_BASE = "https://generativelanguage.googleapis.com/v1beta"
GEMINI_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.5-pro",
    "gemini-2.0-flash",
]
GEMINI_DEFAULT_MODEL = GEMINI_MODELS[0]


PROVIDER_MODELS = {
    "Google Gemini": GEMINI_MODELS,
}


APP_DIR = Path(__file__).resolve().parent
PAGE_ICON_PATH = APP_DIR / "page_icon.png"

THEME_MODE_LABELS = {
    "system": "跟随系统",
    "light": "浅色",
    "dark": "深色",
}


def _theme_css(mode: str) -> str:
    """生成随系统/手动切换的 UI 主题 CSS。"""
    light_vars = """
        --ad-bg: #ffffff;
        --ad-bg-secondary: #f6f8fb;
        --ad-text: #1a1a1a;
        --ad-text-muted: #5f6368;
        --ad-border: #e2e8f0;
        --ad-input-bg: #ffffff;
        --ad-code-bg: #f1f5f9;
        --ad-link: #1f77b4;
        --ad-alert-info-bg: #e8f4fd;
        --ad-alert-info-text: #0c4a6e;
        --ad-alert-warn-bg: #fff8e6;
        --ad-alert-warn-text: #7c4a03;
        --ad-alert-error-bg: #fdecec;
        --ad-alert-error-text: #7f1d1d;
        --ad-alert-success-bg: #ecfdf3;
        --ad-alert-success-text: #14532d;
    """
    dark_vars = """
        --ad-bg: #0d1117;
        --ad-bg-secondary: #161b22;
        --ad-text: #f3f4f6;
        --ad-text-muted: #b6bec8;
        --ad-border: #3d444d;
        --ad-input-bg: #21262d;
        --ad-code-bg: #2d333b;
        --ad-link: #58a6ff;
        --ad-alert-info-bg: #132f4c;
        --ad-alert-info-text: #cce4ff;
        --ad-alert-warn-bg: #3d2e00;
        --ad-alert-warn-text: #ffe8a3;
        --ad-alert-error-bg: #3f1414;
        --ad-alert-error-text: #ffc9c9;
        --ad-alert-success-bg: #0f2f1f;
        --ad-alert-success-text: #b7f5cb;
    """
    shared_rules = """
        .stApp,
        [data-testid="stAppViewContainer"],
        [data-testid="stMain"],
        [data-testid="stMainBlockContainer"],
        .main .block-container {
            background-color: var(--ad-bg) !important;
            color: var(--ad-text) !important;
        }
        [data-testid="stHeader"] {
            background-color: var(--ad-bg) !important;
        }
        [data-testid="stSidebar"],
        [data-testid="stSidebarContent"],
        section[data-testid="stSidebar"] > div {
            background-color: var(--ad-bg-secondary) !important;
            color: var(--ad-text) !important;
        }
        [data-testid="stSidebar"] h1,
        [data-testid="stSidebar"] h2,
        [data-testid="stSidebar"] h3,
        [data-testid="stSidebar"] h4,
        [data-testid="stSidebar"] label,
        [data-testid="stSidebar"] p,
        [data-testid="stSidebar"] span,
        [data-testid="stSidebar"] div,
        .main h1, .main h2, .main h3, .main h4,
        .main label, .main p, .main span, .main div,
        [data-testid="stMarkdownContainer"],
        [data-testid="stMarkdownContainer"] p,
        [data-testid="stMarkdownContainer"] li,
        [data-testid="stMarkdownContainer"] ol,
        [data-testid="stMarkdownContainer"] ul,
        [data-testid="stMarkdownContainer"] h1,
        [data-testid="stMarkdownContainer"] h2,
        [data-testid="stMarkdownContainer"] h3,
        [data-testid="stMarkdownContainer"] h4,
        [data-testid="stMarkdownContainer"] strong,
        [data-testid="stMarkdownContainer"] em,
        [data-testid="stWidgetLabel"] p,
        [data-testid="stRadio"] label,
        [data-testid="stSelectbox"] label,
        [data-testid="stSlider"] label,
        [data-testid="stTextInput"] label,
        [data-testid="stFileUploader"] label {
            color: var(--ad-text) !important;
        }
        .main .stCaption,
        [data-testid="stCaptionContainer"],
        [data-testid="stCaptionContainer"] p {
            color: var(--ad-text-muted) !important;
        }
        a, [data-testid="stMarkdownContainer"] a {
            color: var(--ad-link) !important;
        }
        [data-testid="stTextInput"] input,
        [data-testid="stNumberInput"] input,
        textarea,
        [data-baseweb="select"] > div,
        [data-baseweb="input"] > div,
        [data-baseweb="textarea"] textarea {
            background-color: var(--ad-input-bg) !important;
            color: var(--ad-text) !important;
            border-color: var(--ad-border) !important;
            caret-color: var(--ad-text) !important;
        }
        input::placeholder,
        textarea::placeholder {
            color: var(--ad-text-muted) !important;
            opacity: 1 !important;
        }
        [data-baseweb="popover"],
        [data-baseweb="menu"],
        ul[role="listbox"] {
            background-color: var(--ad-input-bg) !important;
            border-color: var(--ad-border) !important;
        }
        [data-baseweb="menu"] li,
        ul[role="listbox"] li,
        [role="option"] {
            color: var(--ad-text) !important;
            background-color: var(--ad-input-bg) !important;
        }
        [data-baseweb="menu"] li:hover,
        ul[role="listbox"] li:hover,
        [role="option"]:hover {
            background-color: var(--ad-bg-secondary) !important;
        }
        [data-testid="stExpander"] details {
            background-color: var(--ad-bg-secondary) !important;
            border: 1px solid var(--ad-border) !important;
            border-radius: 8px;
        }
        [data-testid="stExpander"] summary,
        [data-testid="stExpander"] summary span,
        [data-testid="stExpander"] summary p {
            color: var(--ad-text) !important;
        }
        [data-testid="stExpander"] [data-testid="stMarkdownContainer"] {
            color: var(--ad-text) !important;
        }
        [data-testid="stButton"] button[kind="secondary"],
        [data-testid="stDownloadButton"] button {
            background-color: var(--ad-bg-secondary) !important;
            color: var(--ad-text) !important;
            border: 1px solid var(--ad-border) !important;
        }
        [data-testid="stButton"] button[kind="primary"] {
            background-color: #1f77b4 !important;
            color: #ffffff !important;
            border: 1px solid #1a6aa3 !important;
        }
        [data-testid="stDataFrame"],
        [data-testid="stDataFrame"] > div,
        [data-testid="stDataFrame"] iframe,
        [data-testid="stDataFrame"] table,
        [data-testid="stDataFrame"] th,
        [data-testid="stDataFrame"] td,
        .stDataFrame,
        .stDataFrame table,
        .stDataFrame th,
        .stDataFrame td {
            background-color: var(--ad-bg-secondary) !important;
            color: var(--ad-text) !important;
            border-color: var(--ad-border) !important;
        }
        [data-testid="stMarkdownContainer"] table,
        [data-testid="stMarkdownContainer"] th,
        [data-testid="stMarkdownContainer"] td {
            background-color: var(--ad-bg-secondary) !important;
            color: var(--ad-text) !important;
            border: 1px solid var(--ad-border) !important;
        }
        hr {
            border-color: var(--ad-border) !important;
        }
        code, pre, [data-testid="stCode"] code {
            background-color: var(--ad-code-bg) !important;
            color: var(--ad-text) !important;
            border: 1px solid var(--ad-border) !important;
        }
        [data-testid="stCodeBlock"] {
            background-color: var(--ad-code-bg) !important;
            border: 1px solid var(--ad-border) !important;
        }
        [data-testid="stFileUploader"] section,
        [data-testid="stFileUploader"] [data-testid="stFileUploaderDropzone"] {
            background-color: var(--ad-bg-secondary) !important;
            border-color: var(--ad-border) !important;
        }
        [data-testid="stFileUploader"] span,
        [data-testid="stFileUploader"] small,
        [data-testid="stFileUploader"] p {
            color: var(--ad-text-muted) !important;
        }
        [data-testid="stAlert"] {
            border: 1px solid var(--ad-border) !important;
        }
        [data-testid="stAlert"] [data-testid="stMarkdownContainer"],
        [data-testid="stAlert"] [data-testid="stMarkdownContainer"] p,
        [data-testid="stAlert"] p,
        [data-testid="stAlert"] span,
        [data-testid="stAlert"] div {
            color: inherit !important;
        }
        [data-testid="stAlert"]:has([data-testid="stAlertContentInfo"]),
        div[data-testid="stNotificationContentInfo"] {
            background-color: var(--ad-alert-info-bg) !important;
            color: var(--ad-alert-info-text) !important;
        }
        [data-testid="stAlert"]:has([data-testid="stAlertContentWarning"]),
        div[data-testid="stNotificationContentWarning"] {
            background-color: var(--ad-alert-warn-bg) !important;
            color: var(--ad-alert-warn-text) !important;
        }
        [data-testid="stAlert"]:has([data-testid="stAlertContentError"]),
        div[data-testid="stNotificationContentError"] {
            background-color: var(--ad-alert-error-bg) !important;
            color: var(--ad-alert-error-text) !important;
        }
        [data-testid="stAlert"]:has([data-testid="stAlertContentSuccess"]),
        div[data-testid="stNotificationContentSuccess"] {
            background-color: var(--ad-alert-success-bg) !important;
            color: var(--ad-alert-success-text) !important;
        }
        .stAlert,
        [data-baseweb="notification"] {
            background-color: var(--ad-alert-info-bg) !important;
            color: var(--ad-alert-info-text) !important;
        }
        [data-testid="stSpinner"] label,
        [data-testid="stSpinner"] span {
            color: var(--ad-text) !important;
        }
        [data-testid="stTooltipIcon"] {
            color: var(--ad-text-muted) !important;
        }
    """

    if mode == "light":
        return f":root {{ {light_vars} }}{shared_rules}"
    if mode == "dark":
        return f":root {{ {dark_vars} }}{shared_rules}"
    return f"""
        :root {{ {light_vars} }}
        @media (prefers-color-scheme: dark) {{
            :root {{ {dark_vars} }}
        }}
        {shared_rules}
    """


def apply_adaptive_theme(mode: str = "system") -> None:
    """注入自适应深/浅色 UI 样式。mode: system | light | dark"""
    st.markdown(f"<style>{_theme_css(mode)}</style>", unsafe_allow_html=True)


# ============================================================
# 主程序：Streamlit UI
# ============================================================

def main(*, embedded: bool = False) -> None:
    try:
        sync_gemini_key_session_state()
        apply_ad_analysis_gemini_patches()
    except Exception:
        pass

    if not embedded:
        st.set_page_config(
            page_title="投放数据AI分析",
            page_icon=str(PAGE_ICON_PATH) if PAGE_ICON_PATH.exists() else "📊",
            layout="wide",
        )

    with st.sidebar:
        if not embedded:
            st.header("🎨 显示主题")
            theme_label = st.radio(
                "界面模式",
                options=list(THEME_MODE_LABELS.values()),
                index=0,
                horizontal=True,
                help="「跟随系统」将根据电脑/浏览器的深色/浅色设置自动切换。",
            )
            theme_mode = next(k for k, v in THEME_MODE_LABELS.items() if v == theme_label)
        else:
            theme_mode = st.session_state.get("suite_theme_mode", "system")

    apply_adaptive_theme(theme_mode)

    if not embedded:
        st.title("投放数据AI分析")
        st.caption("支持日报 / 周报 / 月报，自动跨 Shopify、Google、Meta、AppLovin 多渠道数据生成 AI 分析报告")

    # ---------------- 侧边栏：Gemini 配置 ----------------
    model_name = GEMINI_DEFAULT_MODEL
    temperature = 0.5
    gmail_user = ""
    gmail_password = ""

    with st.sidebar:
        if not embedded:
            st.markdown("---")
            st.header("⚙️ Gemini 配置")

            st.text_input(
                "API Key",
                type="password",
                placeholder="Gemini API Key",
                key=SUITE_GEMINI_API_KEY,
                help="从 https://aistudio.google.com/apikey 获取",
            )
            base_url = GEMINI_BASE_URL

            default_model = GEMINI_DEFAULT_MODEL
            model_name = st.selectbox(
                "模型",
                options=GEMINI_MODELS,
                index=GEMINI_MODELS.index(default_model),
                help="推荐 gemini-2.5-flash；配额紧张时可改用 flash-lite。",
            )

            st.caption(
                "💡 Gemini 免费额度有限；若报 429，请在 "
                "[AI Studio](https://aistudio.google.com/) 激活账单，或改用 flash-lite 模型。"
            )

            temperature = st.slider("Temperature（创意度）", 0.0, 1.0, 0.5, 0.1)

            st.markdown("---")
            with st.expander("📧 Gmail 发信配置", expanded=False):
                st.caption("SMTP：`smtp.gmail.com`　端口：`587`")
                if SUITE_SMTP_USER not in st.session_state:
                    st.session_state[SUITE_SMTP_USER] = _secret("email", "smtp_user")
                if SUITE_SMTP_PASSWORD not in st.session_state:
                    st.session_state[SUITE_SMTP_PASSWORD] = _secret("email", "smtp_password")
                st.text_input(
                    "Gmail 发件邮箱",
                    key=SUITE_SMTP_USER,
                    placeholder="your@gmail.com",
                )
                st.text_input(
                    "Gmail 应用专用密码",
                    type="password",
                    key=SUITE_SMTP_PASSWORD,
                    help="在 Google 账号 → 安全 → 两步验证 → 应用专用密码 中生成（16 位，不是登录密码）。",
                )
        else:
            base_url = GEMINI_BASE_URL
            model_name = st.session_state.get(
                "suite_ad_analysis_model",
                GEMINI_DEFAULT_MODEL,
            )
            temperature = st.session_state.get("suite_ad_analysis_temperature", 0.5)

    api_key = get_gemini_api_key()
    gmail_user = st.session_state.get(SUITE_SMTP_USER, _secret("email", "smtp_user"))
    gmail_password = st.session_state.get(SUITE_SMTP_PASSWORD, _secret("email", "smtp_password"))
    base_url = GEMINI_BASE_URL

    smtp_override = {
        **GMAIL_SMTP,
        "user": gmail_user,
        "password": gmail_password,
        "from_addr": gmail_user,
    }
    smtp_ready = bool(_get_smtp_config(smtp_override))

    # ---------------- 主区：文件上传 ----------------
    uploaded_file = st.file_uploader(
        "📁 上传广告投放数据文件（单个 .xlsx / .csv）",
        type=["xlsx", "csv"],
        accept_multiple_files=False,
        help="每次仅上传一个文件。若使用 Excel，可在同一文件内包含多个 Sheet（Shopify、Google、Meta 等）。",
    )

    if uploaded_file is None:
        st.info(
            "请上传**一个** Excel 或 CSV 文件。"
            "Excel 可在同一工作簿内包含多个 Sheet（如 Shopify / Google / Meta / AppLovin），无需分多个文件上传。"
        )
        return

    # 文件变更时重置缓存
    if (
        "file_id" not in st.session_state
        or st.session_state["file_id"] != (uploaded_file.name, uploaded_file.size)
    ):
        with st.spinner("正在解析文件，自动处理合并表头..."):
            sheets = load_all_sheets(uploaded_file)
        st.session_state["sheets"] = sheets
        st.session_state["file_id"] = (uploaded_file.name, uploaded_file.size)
        st.session_state.pop("report", None)  # 新文件，清空旧报告
        st.session_state.pop("report_extracted_data", None)
        st.session_state.pop("followup_messages", None)

    sheets = st.session_state.get("sheets", {})

    if not sheets:
        st.error("❌ 未能从文件中解析出任何有效数据，请检查文件格式。")
        return

    st.success(
        f"✅ 已加载 1 个文件，内含 {len(sheets)} 个工作表（Sheet）：{', '.join(sheets.keys())}"
    )

    with st.expander("🔍 查看各 Sheet 数据预览（前5行，已剔除总结/分析列）"):
        for name, df in sheets.items():
            st.markdown(f"**{name}**  （共 {len(df)} 行，{len(df.columns)} 列）")
            try:
                st.dataframe(clean_columns(df).head(5), use_container_width=True)
            except Exception as e:
                st.warning(f"预览失败：{e}")

    # ---------------- 报告维度 + 动态日期选择 ----------------
    st.markdown("---")
    st.subheader("📅 报告参数设置")

    col_a, col_b = st.columns([1, 2])
    with col_a:
        report_type = st.selectbox("选择报告维度", ["日报", "周报", "月报"])

    valid_dates = get_valid_dates(sheets)
    weekend_buckets = get_weekend_buckets(sheets)
    week_buckets = resolve_week_buckets_for_ui(sheets, valid_dates)

    if not valid_dates and not weekend_buckets and not week_buckets:
        st.error("❌ 无法从数据中提取到有效日期、Week 周汇总或周末汇总行，请检查 Excel。")
        return

    date_mode_options, default_date_mode_label, date_mode_help = (
        get_date_mode_options_for_report(report_type, weekend_buckets, week_buckets)
    )

    if "date_mode_label" not in st.session_state:
        st.session_state["date_mode_label"] = default_date_mode_label

    if st.session_state.get("_prev_report_type") != report_type:
        st.session_state["_prev_report_type"] = report_type
        st.session_state["date_mode_label"] = default_date_mode_label

    if st.session_state.get("date_mode_label") not in date_mode_options:
        st.session_state["date_mode_label"] = default_date_mode_label

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
            key="date_mode_label",
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
            if not valid_dates:
                st.warning("⚠️ 未找到标准日期，请检查数据。")
            else:
                selected_date = st.selectbox(
                    "选择日期",
                    options=valid_dates,
                    index=len(valid_dates) - 1,
                    format_func=lambda d: d.strftime("%Y-%m-%d (%a)"),
                )
                start_date = end_date = selected_date
                date_info = selected_date.strftime("%Y-%m-%d")

        elif date_mode == "range":
            if not valid_dates:
                st.warning("⚠️ 未找到标准日期，无法设定范围。")
            else:
                min_d, max_d = min(valid_dates), max(valid_dates)
                def_start, def_end = get_default_range_for_report(report_type, valid_dates)
                range_label = "选择周区间" if report_type == "周报" else (
                    "选择月区间" if report_type == "月报" else "选择日期范围"
                )
                st.markdown(f"**{range_label}**")
                c1, c2 = st.columns(2)
                with c1:
                    start_date = st.date_input(
                        "开始日期",
                        value=def_start,
                        min_value=min_d,
                        max_value=max_d,
                        key=f"range_start_{report_type}",
                    )
                with c2:
                    end_date = st.date_input(
                        "结束日期",
                        value=def_end,
                        min_value=min_d,
                        max_value=max_d,
                        key=f"range_end_{report_type}",
                    )
                if start_date > end_date:
                    st.warning("⚠️ 开始日期晚于结束日期，已自动交换。")
                    start_date, end_date = end_date, start_date
                if start_date == end_date:
                    date_info = start_date.strftime("%Y-%m-%d")
                else:
                    date_info = f"{start_date.strftime('%Y-%m-%d')} 至 {end_date.strftime('%Y-%m-%d')}"

        elif date_mode == "week":
            week_bucket = st.selectbox(
                "选择 Week 周汇总",
                options=week_buckets,
                index=len(week_buckets) - 1,
                format_func=format_week_bucket_label,
            )
            date_info = format_week_bucket_label(week_bucket)

        elif date_mode == "weekend":
            def _format_weekend_bucket(b):
                return (
                    f"周末三日 ({b['start'].strftime('%Y-%m-%d')} ~ "
                    f"{b['end'].strftime('%Y-%m-%d')})"
                )

            weekend_bucket = st.selectbox(
                "选择周末三日",
                options=weekend_buckets,
                index=len(weekend_buckets) - 1,
                format_func=_format_weekend_bucket,
            )
            start_date = weekend_bucket["start"]
            end_date = weekend_bucket["end"]
            date_info = _format_weekend_bucket(weekend_bucket)

    if valid_dates:
        st.caption(
            f"📌 标准日期：{min(valid_dates)} ~ {max(valid_dates)}，共 {len(valid_dates)} 天"
        )
    if week_buckets:
        recent = ", ".join(format_week_bucket_label(b) for b in week_buckets[-4:])
        st.caption(
            f"📌 可用 Week 周区间 {len(week_buckets)} 个（由逐日数据生成，含日期范围）；"
            f"最近：{recent}"
        )
    if weekend_buckets:
        st.caption(f"📌 检测到 {len(weekend_buckets)} 个「周末三日」汇总段")

    # ---------------- 生成报告 ----------------
    st.markdown("---")
    generate_btn = st.button("🚀 生成 AI 分析报告", type="primary", use_container_width=False)

    if generate_btn:
        clean_api_key = _resolve_gemini_key_for_report()
        if not clean_api_key:
            st.error(f"❌ [{GEMINI_KEY_VALIDATION_VERSION}] {_gemini_key_error_hint()}")
        else:
            try:
                with st.spinner("正在提取多渠道数据..."):
                    extracted_data = extract_data_for_report(
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
                    prev_resolved = resolve_previous_period(
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
                        previous_extracted_data = extract_data_for_report(
                            sheets_dict=sheets,
                            date_mode=date_mode,
                            **prev_kwargs,
                        )

                with st.expander("🧾 查看提交给 AI 的原始数据（调试用）"):
                    for sheet_name, data in extracted_data.items():
                        st.markdown(f"**{sheet_name}**")
                        if isinstance(data, str):
                            st.text(data)
                        else:
                            try:
                                st.code(
                                    json.dumps(data, ensure_ascii=False, indent=2, default=str),
                                    language="json",
                                )
                            except Exception as e:
                                st.text(f"（展示失败：{e}）")

                with st.spinner("AI 正在分四节生成完整报告（约 3~6 分钟）..."):
                    system_prompt = build_system_prompt(report_type, date_mode)
                    user_content = build_user_content(extracted_data, date_info, date_mode)
                    report = generate_report(
                        api_key=clean_api_key,
                        base_url=base_url,
                        model_name=model_name,
                        system_prompt=system_prompt,
                        user_content=user_content,
                        temperature=temperature,
                        extracted_data=extracted_data,
                        previous_extracted_data=previous_extracted_data,
                        current_period_label=date_info,
                        previous_period_label=previous_period_label,
                    )

                st.session_state["report"] = report
                st.session_state["report_meta"] = {
                    "report_type": report_type,
                    "date_info": date_info,
                    "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                }
                st.session_state["report_extracted_data"] = extracted_data
                st.session_state["followup_messages"] = []

            except Exception as e:
                st.error(f"❌ 生成报告过程中发生未预期错误：{e}")

    # ---------------- 报告展示与导出 ----------------
    if "report" in st.session_state:
        meta = st.session_state.get("report_meta", {})
        st.markdown("---")
        st.subheader(f"📄 {meta.get('report_type', '')}　|　{meta.get('date_info', '')}")

        st.markdown(st.session_state["report"])

        report_body = st.session_state["report"]
        md_doc = build_full_markdown(report_body, meta)
        html_doc = build_full_html(report_body, meta)

        st.markdown("**下载完整文档**")
        col_dl1, col_dl2, col_dl3 = st.columns(3)
        base_name = _safe_report_filename(meta, "").rstrip(".")

        with col_dl1:
            st.download_button(
                label="📄 Word 文档 (.docx)",
                data=build_docx_bytes(report_body, meta),
                file_name=f"{base_name}.docx",
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                use_container_width=True,
            )
        with col_dl2:
            st.download_button(
                label="📝 Markdown (.md)",
                data=md_doc,
                file_name=f"{base_name}.md",
                mime="text/markdown",
                use_container_width=True,
            )
        with col_dl3:
            st.download_button(
                label="🌐 HTML 网页 (.html)",
                data=html_doc,
                file_name=f"{base_name}.html",
                mime="text/html",
                use_container_width=True,
            )

        st.markdown("**发送报告到邮箱**")
        default_recipients = _secret("email", "default_recipients")
        if not smtp_ready:
            st.warning("请先在左侧「📧 Gmail 发信配置」中填写发件邮箱和应用专用密码。")

        col_mail1, col_mail2 = st.columns([2, 1])
        with col_mail1:
            recipient_text = st.text_input(
                "收件人邮箱",
                value=default_recipients,
                placeholder="a@company.com, b@company.com",
                help="多个邮箱可用逗号、分号或换行分隔。",
            )
        with col_mail2:
            mail_format = st.selectbox(
                "附件格式",
                options=["docx", "html", "md"],
                format_func=lambda x: {"docx": "Word (.docx)", "html": "HTML (.html)", "md": "Markdown (.md)"}[x],
            )

        if st.button("📧 发送邮件", disabled=not smtp_ready, use_container_width=False):
            recipients = _parse_email_list(recipient_text)
            if not recipients:
                st.error("请填写至少一个有效的收件人邮箱。")
            else:
                with st.spinner("正在发送邮件..."):
                    ok, message = send_report_email(
                        recipients,
                        report_body,
                        meta,
                        attachment_format=mail_format,
                        smtp_override=smtp_override,
                    )
                if ok:
                    st.success(message)
                else:
                    st.error(message)

        # ---------------- 具体提问（不纳入邮件） ----------------
        st.markdown("---")
        st.subheader("💬 具体提问")
        st.caption(
            "基于当前 Excel 数据与上方 AI 报告继续追问；"
            "**本模块对话不会写入邮件或下载附件。**"
        )

        followup_messages = st.session_state.setdefault("followup_messages", [])
        extracted_for_qa = st.session_state.get("report_extracted_data")

        if not extracted_for_qa:
            st.info("请先生成 AI 分析报告后再提问。")
        else:
            for msg in followup_messages:
                with st.chat_message(msg["role"]):
                    st.markdown(msg["content"])

            col_clear, _ = st.columns([1, 5])
            with col_clear:
                if st.button("清空对话", use_container_width=True):
                    st.session_state["followup_messages"] = []
                    st.rerun()

            question = st.chat_input(
                "输入问题，例如：WearNuage 账号 ROAS 下降的主要原因？Google 是否值得加预算？",
                key="followup_chat_input",
            )
            if question:
                followup_key = _resolve_gemini_key_for_report()
                if not followup_key:
                    st.error(f"❌ [{GEMINI_KEY_VALIDATION_VERSION}] {_gemini_key_error_hint()}")
                else:
                    with st.spinner("AI 正在思考..."):
                        reply = answer_followup_question(
                            api_key=followup_key,
                            base_url=base_url,
                            model_name=model_name,
                            temperature=temperature,
                            extracted_data=extracted_for_qa,
                            report_text=report_body,
                            date_info=meta.get("date_info", ""),
                            report_type=meta.get("report_type", ""),
                            question=question,
                            history=followup_messages,
                        )
                    st.session_state["followup_messages"].append(
                        {"role": "user", "content": question}
                    )
                    st.session_state["followup_messages"].append(
                        {"role": "assistant", "content": reply}
                    )
                    st.rerun()


if __name__ == "__main__":
    main()
