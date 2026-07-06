# -*- coding: utf-8 -*-
"""Meta Marketing API — 按广告账户拉取 Insights，转为投放分析工具可用的 Sheet 结构。"""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta
from typing import Any, Optional

import httpx
import pandas as pd

GRAPH_API_VERSION = "v21.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

PURCHASE_ACTION_TYPES = {
    "purchase",
    "omni_purchase",
    "offsite_conversion.fb_pixel_purchase",
    "web_in_store_purchase",
}

DEFAULT_INSIGHT_FIELDS = (
    "spend,impressions,clicks,cpm,cpc,ctr,actions,action_values,purchase_roas"
)


def _secret(section: str, key: str, default: str = "") -> str:
    try:
        import streamlit as st

        val = st.secrets[section][key]
        return str(val).strip() if val else default
    except Exception:
        pass
    section_upper = section.upper()
    key_upper = re.sub(r"[^A-Za-z0-9]", "_", key).upper()
    return (
        os.environ.get(f"{section_upper}_{key_upper}", "")
        or os.environ.get("META_ACCESS_TOKEN" if key == "access_token" else "", "")
        or default
    ).strip()


def normalize_ad_account_id(account_id: str) -> str:
    raw = (account_id or "").strip().replace(" ", "")
    if not raw:
        return ""
    if raw.startswith("act_"):
        return raw
    digits = re.sub(r"\D", "", raw)
    return f"act_{digits}" if digits else raw


def parse_account_configs(text: str) -> list[dict]:
    """
    解析账户配置，每行一种格式：
      WearNuage:act_123456789
      act_123456789,WearNuage
      act_123456789
    """
    configs = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            name, acc = line.split(":", 1)
            name, acc = name.strip(), acc.strip()
        elif "," in line:
            acc, name = [p.strip() for p in line.split(",", 1)]
        else:
            acc, name = line, line
        acc = normalize_ad_account_id(acc)
        if not acc:
            continue
        display = name if name and name != acc else acc.replace("act_", "Account ")
        configs.append({"id": acc, "name": display})
    return configs


def parse_accounts_from_secrets() -> list[dict]:
    """从 secrets [meta.accounts] 或 [meta] accounts_json 读取。"""
    configs: list[dict] = []
    try:
        import streamlit as st

        accounts = st.secrets.get("meta", {}).get("accounts")
        if accounts:
            for item in accounts:
                if isinstance(item, dict):
                    acc_id = normalize_ad_account_id(str(item.get("id", "")))
                    name = str(item.get("name") or acc_id).strip()
                    if acc_id:
                        configs.append({"id": acc_id, "name": name})
            if configs:
                return configs
        raw_json = st.secrets.get("meta", {}).get("accounts_json", "")
        if raw_json:
            data = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
            for item in data:
                acc_id = normalize_ad_account_id(str(item.get("id", "")))
                name = str(item.get("name") or acc_id).strip()
                if acc_id:
                    configs.append({"id": acc_id, "name": name})
    except Exception:
        pass
    return configs


def _sum_actions(entries: Optional[list], action_types: set) -> float:
    total = 0.0
    for item in entries or []:
        if item.get("action_type") in action_types:
            try:
                total += float(item.get("value") or 0)
            except (TypeError, ValueError):
                continue
    return total


def _parse_purchase_roas_field(purchase_roas: Any) -> Optional[float]:
    if not purchase_roas:
        return None
    if isinstance(purchase_roas, list) and purchase_roas:
        try:
            return float(purchase_roas[0].get("value"))
        except (TypeError, ValueError, KeyError):
            return None
    try:
        return float(purchase_roas)
    except (TypeError, ValueError):
        return None


def insight_row_to_record(row: dict) -> dict:
    spend = float(row.get("spend") or 0)
    purchases = _sum_actions(row.get("actions"), PURCHASE_ACTION_TYPES)
    purchase_value = _sum_actions(row.get("action_values"), PURCHASE_ACTION_TYPES)
    roas = _parse_purchase_roas_field(row.get("purchase_roas"))
    if roas is None and spend > 0 and purchase_value > 0:
        roas = purchase_value / spend

    date_start = row.get("date_start") or row.get("date_stop") or ""
    return {
        "日期": date_start,
        "Ad Spent_Total": round(spend, 2),
        "Spend": round(spend, 2),
        "消耗": round(spend, 2),
        "Impressions": int(float(row.get("impressions") or 0)),
        "Clicks": int(float(row.get("clicks") or 0)),
        "CPM": round(float(row.get("cpm") or 0), 4),
        "CPC": round(float(row.get("cpc") or 0), 4),
        "CTR": round(float(row.get("ctr") or 0), 4),
        "出单量": int(purchases),
        "Orders": int(purchases),
        "Purchase_Value": round(purchase_value, 2),
        "ROAS": round(roas, 4) if roas is not None else None,
        "ROI": round(roas, 4) if roas is not None else None,
    }


def _fetch_insights_page(
    client: httpx.Client,
    ad_account_id: str,
    access_token: str,
    since: date,
    until: date,
    after: Optional[str] = None,
) -> dict:
    params = {
        "access_token": access_token,
        "fields": DEFAULT_INSIGHT_FIELDS,
        "time_range": json.dumps({
            "since": since.isoformat(),
            "until": until.isoformat(),
        }),
        "time_increment": 1,
        "level": "account",
        "limit": 500,
    }
    if after:
        params["after"] = after
    url = f"{GRAPH_BASE}/{ad_account_id}/insights"
    resp = client.get(url, params=params, timeout=60.0)
    resp.raise_for_status()
    return resp.json()


def fetch_account_daily_insights(
    access_token: str,
    ad_account_id: str,
    since: date,
    until: date,
) -> list[dict]:
    ad_account_id = normalize_ad_account_id(ad_account_id)
    rows: list[dict] = []
    after = None
    with httpx.Client(timeout=httpx.Timeout(90.0, connect=30.0)) as client:
        while True:
            payload = _fetch_insights_page(
                client, ad_account_id, access_token, since, until, after=after
            )
            data = payload.get("data") or []
            rows.extend(data)
            paging = payload.get("paging") or {}
            cursors = paging.get("cursors") or {}
            after = cursors.get("after")
            if not after or not data:
                break
    return rows


def _aggregate_records(records: list[dict]) -> dict:
    if not records:
        return {}
    spend = sum(float(r.get("Ad Spent_Total") or 0) for r in records)
    orders = sum(int(r.get("出单量") or 0) for r in records)
    purchase_value = sum(float(r.get("Purchase_Value") or 0) for r in records)
    roas = purchase_value / spend if spend > 0 else None
    return {
        "Ad Spent_Total": round(spend, 2),
        "Spend": round(spend, 2),
        "消耗": round(spend, 2),
        "Impressions": sum(int(r.get("Impressions") or 0) for r in records),
        "Clicks": sum(int(r.get("Clicks") or 0) for r in records),
        "出单量": orders,
        "Orders": orders,
        "Purchase_Value": round(purchase_value, 2),
        "ROAS": round(roas, 4) if roas is not None else None,
        "ROI": round(roas, 4) if roas is not None else None,
    }


def enrich_with_week_and_weekend_rows(df: pd.DataFrame) -> pd.DataFrame:
    """追加 Week / 周末三日 汇总行，与 Excel 分析工具的时间维度一致。"""
    if df is None or df.empty or "日期" not in df.columns:
        return df

    daily = df.copy()
    daily["_parsed"] = pd.to_datetime(daily["日期"], errors="coerce")
    daily = daily[daily["_parsed"].notna()].sort_values("_parsed")
    if daily.empty:
        return df.drop(columns=["_parsed"], errors="ignore")

    extra_rows = []

    # ISO 周汇总 → 日期列写 Week N
    for week_key, group in daily.groupby(daily["_parsed"].dt.isocalendar().week):
        agg = _aggregate_records(group.drop(columns=["_parsed"]).to_dict("records"))
        agg["日期"] = f"Week {int(week_key)}"
        extra_rows.append(agg)

    # 周末三日：周五~周日聚合
    daily["_wd"] = daily["_parsed"].dt.weekday
    daily["_weekend_key"] = daily["_parsed"].apply(
        lambda d: (d - timedelta(days=(d.weekday() - 4) % 7)).date()
        if d.weekday() >= 4
        else pd.NaT
    )
    weekend_groups = daily[daily["_weekend_key"].notna()].groupby("_weekend_key")
    for _, group in weekend_groups:
        fri = group["_parsed"].min().date()
        sun = group["_parsed"].max().date()
        if (sun - fri).days > 2:
            continue
        agg = _aggregate_records(group.drop(columns=["_parsed", "_wd", "_weekend_key"]).to_dict("records"))
        agg["日期"] = "周末三日"
        agg["_weekend_start"] = fri.isoformat()
        agg["_weekend_end"] = sun.isoformat()
        extra_rows.append(agg)

    if extra_rows:
        extra_df = pd.DataFrame(extra_rows)
        out = pd.concat([df, extra_df], ignore_index=True)
        return out
    return df


def insights_to_dataframe(rows: list[dict]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    records = [insight_row_to_record(r) for r in rows]
    df = pd.DataFrame(records)
    return enrich_with_week_and_weekend_rows(df)


def fetch_meta_sheets(
    access_token: str,
    account_configs: list[dict],
    since: date,
    until: date,
) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """
    拉取多个广告账户日粒度数据，返回 {Sheet名: DataFrame} 与错误列表。
    """
    sheets: dict[str, pd.DataFrame] = {}
    errors: list[str] = []
    token = (access_token or "").strip()
    if not token:
        return sheets, ["未配置 Meta Access Token"]

    if since > until:
        since, until = until, since

    for cfg in account_configs:
        acc_id = cfg["id"]
        name = cfg["name"]
        try:
            rows = fetch_account_daily_insights(token, acc_id, since, until)
            df = insights_to_dataframe(rows)
            if df.empty:
                errors.append(f"{name}（{acc_id}）：该区间无 Insights 数据")
                continue
            sheets[name] = df
        except httpx.HTTPStatusError as e:
            detail = ""
            try:
                detail = e.response.json().get("error", {}).get("message", "")
            except Exception:
                detail = e.response.text[:200] if e.response else str(e)
            errors.append(f"{name}（{acc_id}）：HTTP {e.response.status_code} {detail}")
        except Exception as e:
            errors.append(f"{name}（{acc_id}）：{e}")

    return sheets, errors


def get_default_fetch_range(days: int = 120) -> tuple[date, date]:
    until = date.today()
    since = until - timedelta(days=max(days - 1, 1))
    return since, until


def merge_sheet_dicts(base: dict, override: dict) -> dict:
    merged = dict(base or {})
    merged.update(override or {})
    return merged


def get_access_token(default: str = "") -> str:
    """从 session_state / secrets / 环境变量读取 Meta Access Token。"""
    try:
        import streamlit as st

        key = "meta_access_token"
        if key in st.session_state and (st.session_state[key] or "").strip():
            return str(st.session_state[key]).strip()
    except Exception:
        pass
    return _secret("meta", "access_token", default)


def format_accounts_for_textarea(configs: list[dict]) -> str:
    if not configs:
        return ""
    return "\n".join(f"{cfg['name']}:{cfg['id']}" for cfg in configs)


def check_meta_connectivity(access_token: str) -> tuple[bool, str]:
    token = (access_token or "").strip()
    if not token:
        return False, "未填写 Meta Access Token"
    try:
        with httpx.Client(timeout=httpx.Timeout(30.0, connect=15.0)) as client:
            resp = client.get(
                f"{GRAPH_BASE}/me",
                params={"access_token": token, "fields": "id,name"},
            )
            resp.raise_for_status()
            data = resp.json()
            name = data.get("name") or data.get("id") or "OK"
            return True, f"已连接 Meta 用户：{name}"
    except httpx.HTTPStatusError as e:
        detail = ""
        try:
            detail = e.response.json().get("error", {}).get("message", "")
        except Exception:
            detail = e.response.text[:200] if e.response else str(e)
        return False, f"HTTP {e.response.status_code}：{detail or e}"
    except Exception as e:
        return False, str(e)
