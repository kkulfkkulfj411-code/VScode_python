# ==============================================================================
# AI株式分析データジェネレーター - data_fetcher.py (完全版 v2026.11 Rev.9.2 高密度スリム版)
# 実装済仕様：東証年末年始の振替休日誤判定修正 / 日本株ファンダ5段階フォールバック /
#            取得失敗・市場区分N/A時の非キャッシュ保護 / EDINET API稼働日フィルタ＆1日目Fail-Fast /
#            EDINETキー種別自動判別(金融庁公式v2 ＆ 民間EDINET DB v1両対応・ROE自動補完) /
#            起動時API自動監視＆ワンクリック接続診断(check_edinet_api_status) /
#            マクロ13指標化 ＋ CBOE公式CDN CSVフォールバック(VIX3M等の取得失敗完全防止) /
#            世界情勢・日米金融政策・日本の国策RSS 4カテゴリ独立取得＆出典明記・ゴシップ＆ETF/定型ページノイズ除外 /
#            上場市場抽出 ＆ TOPIXパッシブ需給区分5段階自動定量判定(区分不明の根絶) /
#            [Rev.9.2] マクロ指標(1306.T/N225等)の最新営業日欠落に対する短期足(5d/1h)自動合成パッチ
#                      ＆ 国策RSSへのETF決算短信・株価定型ページ混入防止フィルタ追加済
# ==============================================================================
import calendar
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime
from datetime import date, time as dt_time, timedelta, timezone
import difflib
import io
import re
import time
import urllib.parse
from bs4 import BeautifulSoup
import fear_greed
import feedparser
import numpy as np
import pandas as pd
import requests
import streamlit as st
import yfinance as yf

try:
  from curl_cffi import requests as cffi_requests
  HAS_CURL_CFFI = True
except ImportError:
  HAS_CURL_CFFI = False

JST = timezone(timedelta(hours=9))
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _http_get_html(url: str, timeout: int = 8) -> str:
  if HAS_CURL_CFFI:
    try:
      res = cffi_requests.get(
          url, headers={"Accept-Language": "ja,en-US;q=0.9,en;q=0.8"},
          impersonate="chrome", timeout=timeout,
      )
      if res.status_code == 200 and len(res.content) > 200:
        return res.content.decode("utf-8", errors="ignore")
    except Exception:
      pass
  headers = {
      "User-Agent": USER_AGENT,
      "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
      "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
      "Cache-Control": "no-cache",
  }
  try:
    res = requests.get(url, headers=headers, timeout=timeout)
    if res.status_code == 200 and len(res.content) > 200:
      return res.content.decode("utf-8", errors="ignore")
  except Exception:
    pass
  return ""


def get_dynamic_jp_holidays(year: int) -> set:
  fixed = [(1, 1), (2, 11), (2, 23), (4, 29), (5, 3), (5, 4), (5, 5), (8, 11), (11, 3), (11, 23)]
  holidays = {date(year, m, d) for m, d in fixed}

  def nth_monday(y, m, n):
    first_day = date(y, m, 1)
    return date(y, m, 1 + (7 - first_day.weekday()) % 7 + (n - 1) * 7)

  for m, n in [(1, 2), (7, 3), (9, 3), (10, 2)]:
    holidays.add(nth_monday(year, m, n))

  vernal_day = int(20.8431 + 0.242194 * (year - 1980) - int((year - 1980) / 4))
  autumnal_day = int(23.2488 + 0.242194 * (year - 1980) - int((year - 1980) / 4))
  holidays.add(date(year, 3, vernal_day))
  holidays.add(date(year, 9, autumnal_day))

  keiro, shubun = nth_monday(year, 9, 3), date(year, 9, autumnal_day)
  if (shubun - keiro).days == 2:
    holidays.add(keiro + timedelta(days=1))

  # 国民の祝日が日曜日の場合の振替休日算出
  for h in sorted(list(holidays)):
    if h.weekday() == 6:
      sub = h + timedelta(days=1)
      while sub in holidays:
        sub += timedelta(days=1)
      holidays.add(sub)

  # 東証の年末年始休業日（振替休日の対象外）を最後に追加
  for m, d in [(1, 2), (1, 3), (12, 31)]:
    holidays.add(date(year, m, d))
  return holidays


def get_sbi_order_limit_dates() -> str:
  weekdays_jp = ["月", "火", "水", "木", "金", "土", "日"]
  now = datetime.datetime.now(JST)
  curr_date = now.date()
  jp_holidays = get_dynamic_jp_holidays(curr_date.year) | get_dynamic_jp_holidays(curr_date.year + 1)

  def is_tse_open(d):
    return d.weekday() < 5 and d not in jp_holidays

  def fmt_d(d):
    return f"{d.strftime('%m/%d')}({weekdays_jp[d.weekday()]})"

  start_date = curr_date if (is_tse_open(curr_date) and now.time() < dt_time(15, 30)) else curr_date + timedelta(days=1)
  while not is_tse_open(start_date):
    start_date += timedelta(days=1)

  biz_days, d_cursor = [], start_date
  while len(biz_days) < 15:
    if is_tse_open(d_cursor):
      biz_days.append(d_cursor)
    d_cursor += timedelta(days=1)

  def get_month_end_kenri_date(year, month):
    d = date(year, month, calendar.monthrange(year, month)[1])
    open_days = []
    while len(open_days) < 3:
      if is_tse_open(d):
        open_days.append(d)
      d -= timedelta(days=1)
    return open_days[2]

  y1, m1 = start_date.year, start_date.month
  y2, m2 = (y1 + 1, 1) if m1 == 12 else (y1, m1 + 1)
  kenri_curr, kenri_next = get_month_end_kenri_date(y1, m1), get_month_end_kenri_date(y2, m2)

  return (
      f"[HYPER SBI 2 期間指定カレンダー（JST {now.strftime('%m/%d %H:%M')} 時点・東証祝日自動算出）]\n"
      f"・初回有効営業日(1営業日目): {fmt_d(biz_days[0])}\n"
      f"・第1注文(新規買い)推奨期限目安: 3営業日目={fmt_d(biz_days[2])} | 5営業日目={fmt_d(biz_days[4])} | 10営業日目={fmt_d(biz_days[9])}\n"
      f"・第2注文(決済OCO)システム最長上限(15営業日目): {fmt_d(biz_days[14])}\n"
      f"・月末銘柄の権利付き最終日目安: {m1}月末基準={fmt_d(kenri_curr)} / {m2}月末基準={fmt_d(kenri_next)}"
      " （※当該銘柄の権利確定月である場合、権利付き最終日をまたぐ期間指定は不可のためその日が上限となる）\n"
  )


@st.cache_data(ttl=86400, show_spinner=False)
def _load_edinet_csv():
  try:
    df_code = pd.read_csv("EdinetcodeDlInfo.csv", encoding="cp932", skiprows=1)
  except UnicodeDecodeError:
    df_code = pd.read_csv("EdinetcodeDlInfo.csv", encoding="utf-8", skiprows=1)
  df_code["証券コード_数値"] = pd.to_numeric(df_code["証券コード"], errors="coerce")
  return df_code


def _match_edinet_row(stock_code_4digit: str) -> pd.DataFrame:
  try:
    df_code = _load_edinet_csv()
    code_str = str(stock_code_4digit).strip().upper()
    if code_str.isdigit():
      return df_code[df_code["証券コード_数値"] == float(code_str) * 10]
    sec_s = df_code["証券コード"].astype(str).str.strip().str.upper()
    return df_code[sec_s.str.startswith(code_str)]
  except Exception:
    return pd.DataFrame()


def _resolve_edinet_code(stock_code_4digit: str, edb_api_key: str = "") -> str:
  match_row = _match_edinet_row(stock_code_4digit)
  if not match_row.empty:
    return str(match_row["ＥＤＩＮＥＴコード"].values[0]).strip()
  code_str = str(stock_code_4digit).strip().upper()
  if edb_api_key:
    try:
      res = requests.get(
          "https://edinetdb.jp/v1/search", params={"q": code_str},
          headers={"X-API-Key": edb_api_key}, timeout=8,
      )
      if res.status_code == 200:
        data = res.json().get("data", [])
        if isinstance(data, list) and data:
          for item in data:
            if str(item.get("sec_code") or "").strip().upper().startswith(code_str) and item.get("edinet_code"):
              return str(item["edinet_code"]).strip()
          if data[0].get("edinet_code"):
            return str(data[0]["edinet_code"]).strip()
    except Exception:
      pass
  return ""


MACRO_HARD_CAPS = {"^VIX": 30.0, "^VIX3M": 28.0, "^VVIX": 130.0, "^SKEW": 162.0, "^MOVE": 135.0}
CBOE_CSV_URLS = {
    "^VIX": "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv",
    "^VIX3M": "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX3M_History.csv",
    "^VVIX": "https://cdn.cboe.com/api/global/us_indices/daily_prices/VVIX_History.csv",
    "^SKEW": "https://cdn.cboe.com/api/global/us_indices/daily_prices/SKEW_History.csv",
}


def _fetch_cboe_csv_fallback(sym: str) -> pd.DataFrame:
  url = CBOE_CSV_URLS.get(sym)
  if not url:
    return pd.DataFrame()
  try:
    csv_text = _http_get_html(url, timeout=8)
    if not csv_text or "DATE" not in csv_text.upper():
      return pd.DataFrame()
    raw_df = pd.read_csv(io.StringIO(csv_text))
    if raw_df.empty:
      return pd.DataFrame()
    col_map = {str(c).strip().upper(): c for c in raw_df.columns}
    date_col = col_map.get("DATE")
    if not date_col:
      return pd.DataFrame()
    raw_df["Date"] = pd.to_datetime(raw_df[date_col], errors="coerce")
    raw_df = raw_df.dropna(subset=["Date"]).set_index("Date").sort_index()
    raw_df.index = raw_df.index.normalize()
    raw_df = raw_df[~raw_df.index.duplicated(keep="last")]
    clean_sym = sym.lstrip("^").upper()
    close_col = col_map.get("CLOSE") or col_map.get(clean_sym) or (raw_df.columns[-1] if len(raw_df.columns) >= 1 else None)
    if not close_col:
      return pd.DataFrame()
    out = pd.DataFrame(index=raw_df.index)
    out["Close"] = pd.to_numeric(raw_df[close_col], errors="coerce")
    for target_c, cand_k in [("Open", "OPEN"), ("High", "HIGH"), ("Low", "LOW")]:
      out[target_c] = pd.to_numeric(raw_df[col_map[cand_k]], errors="coerce").fillna(out["Close"]) if cand_k in col_map else out["Close"]
    return out.dropna(subset=["Close"]).tail(265)
  except Exception:
    return pd.DataFrame()


@st.cache_data(ttl=1800, show_spinner=False)
def get_macro_data_and_history():
  macro_specs = [
      ("^VIX", "VIX (S&P500恐怖指数)", 2, ""),
      ("^VIX3M", "VIX3M (3ヶ月中期VIX)", 2, ""),
      ("^VVIX", "VVIX (VIXボラティリティ)", 1, ""),
      ("^SKEW", "SKEW (ブラックスワン指数)", 1, ""),
      ("^MOVE", "MOVE (米国債ボラティリティ)", 1, ""),
      ("HYG", "HYG (ハイイールド債ETF)", 2, "$"),
      ("^SOX", "SOX (半導体株指数)", 1, ""),
      ("^TNX", "US10Y (米10年債利回り)", 2, "%"),
      ("CL=F", "WTI原油先物 (エネルギー/地政学)", 2, "$"),
      ("GC=F", "金先物 Gold (有事逃避/実質金利)", 1, "$"),
      ("USDJPY=X", "USD/JPY (ドル円)", 2, "円"),
      ("^N225", "Nikkei225 (日経平均)", 0, "円"),
      ("1306.T", "TOPIX連動ETF (1306.T)", 1, "円"),
  ]

  def _normalize_yf_df(df: pd.DataFrame, target_tz: str, normalize_date: bool = True) -> pd.DataFrame:
    if df is None or df.empty:
      return pd.DataFrame()
    df = df.copy()
    if isinstance(df.columns, pd.MultiIndex):
      df.columns = [col[0] if isinstance(col, tuple) else col for col in df.columns]
    if "Repaired?" in df.columns:
      df = df.drop(columns=["Repaired?"])
    if "Close" not in df.columns:
      return pd.DataFrame()
    df["Close"] = pd.to_numeric(df["Close"], errors="coerce")
    df = df.dropna(subset=["Close"])
    if df.empty:
      return pd.DataFrame()
    for c in ["Open", "High", "Low"]:
      df[c] = pd.to_numeric(df[c], errors="coerce").fillna(df["Close"]) if c in df.columns else df["Close"]
    df["High"] = df[["Open", "High", "Low", "Close"]].max(axis=1)
    df["Low"] = df[["Open", "High", "Low", "Close"]].min(axis=1)
    df.index = pd.to_datetime(df.index)
    if df.index.tz is not None:
      try:
        df.index = df.index.tz_convert(target_tz).tz_localize(None)
      except Exception:
        df.index = df.index.tz_localize(None)
    if normalize_date:
      df.index = df.index.normalize()
    return df[~df.index.duplicated(keep="last")].sort_index()

  def _patch_latest_from_intraday(t_sym: str, df_daily: pd.DataFrame, target_tz: str) -> pd.DataFrame:
    if df_daily is None or df_daily.empty:
      return pd.DataFrame()
    last_d = df_daily.index[-1]
    try:
      df_5d = _normalize_yf_df(yf.Ticker(t_sym).history(period="5d", interval="1d", auto_adjust=False), target_tz, True)
      if not df_5d.empty and df_5d.index[-1] > last_d:
        df_daily = pd.concat([df_daily, df_5d])
        df_daily = df_daily[~df_daily.index.duplicated(keep="last")].sort_index()
        last_d = df_daily.index[-1]
    except Exception:
      pass
    try:
      df_1h = _normalize_yf_df(yf.Ticker(t_sym).history(period="5d", interval="1h", auto_adjust=False), target_tz, False)
      if not df_1h.empty:
        df_1h["DateNorm"] = df_1h.index.normalize()
        agg_1h = df_1h.groupby("DateNorm").agg(Open=("Open", "first"), High=("High", "max"), Low=("Low", "min"), Close=("Close", "last"))
        new_dates = agg_1h[agg_1h.index > last_d]
        if not new_dates.empty:
          for col in df_daily.columns:
            if col not in new_dates.columns:
              new_dates[col] = 0.0
          df_daily = pd.concat([df_daily, new_dates[df_daily.columns]])
          df_daily = df_daily[~df_daily.index.duplicated(keep="last")].sort_index()
        elif last_d in agg_1h.index:
          df_daily.loc[last_d, "High"] = max(float(df_daily.loc[last_d, "High"]), float(agg_1h.loc[last_d, "High"]))
          df_daily.loc[last_d, "Low"] = min(float(df_daily.loc[last_d, "Low"]), float(agg_1h.loc[last_d, "Low"]))
    except Exception:
      pass
    return df_daily

  def _fetch_one(sym):
    target_tz = "Asia/Tokyo" if (sym in ["^N225", "USDJPY=X", "1306.T"] or sym.endswith(".T")) else "America/New_York"
    df, used_ticker = pd.DataFrame(), sym
    for t_sym in ([sym, "^VXV"] if sym == "^VIX3M" else [sym]):
      for period_try in ["1y", "6mo"]:
        try:
          raw = yf.Ticker(t_sym).history(period=period_try, interval="1d", auto_adjust=False)
          df = _normalize_yf_df(raw, target_tz, True)
          if not df.empty and len(df) >= 30:
            used_ticker = t_sym
            break
        except Exception:
          pass
        time.sleep(0.25)
      if not df.empty and len(df) >= 30:
        break

    if sym in CBOE_CSV_URLS:
      days_lag = (datetime.datetime.now(JST).date() - df.index[-1].date()).days if not df.empty else 999
      if df.empty or len(df) < 60 or days_lag >= 3:
        cboe_df = _fetch_cboe_csv_fallback(sym)
        if not cboe_df.empty:
          merged = cboe_df if df.empty else pd.concat([df, cboe_df])
          df = merged[~merged.index.duplicated(keep="last")].sort_index()
    elif not df.empty:
      days_lag = (datetime.datetime.now(JST).date() - df.index[-1].date()).days
      if sym in ["1306.T", "^N225", "USDJPY=X"] or days_lag >= 2:
        df = _patch_latest_from_intraday(used_ticker, df, target_tz)
    return sym, df

  histories = {}
  with ThreadPoolExecutor(max_workers=6) as executor:
    for f in as_completed([executor.submit(_fetch_one, s[0]) for s in macro_specs]):
      sym, df = f.result()
      histories[sym] = df

  try:
    fg_data = fear_greed.get()
    fg_str = f"{round(fg_data['score'], 1)} ({fg_data['rating']})"
  except Exception:
    fg_str = "取得エラー"

  def _fmt_v(val, digits, unit=""):
    if pd.isna(val):
      return "N/A"
    return f"{int(round(val))}{unit}" if digits == 0 else f"{val:.{digits}f}{unit}"

  def _judge_dynamic_regime(sym, digits, curr, z_score, diff_1d, c_series):
    if pd.isna(curr) or c_series.empty:
      return "N/A", None, None
    win252 = c_series.tail(252)
    pct252 = int(round((win252 <= curr).mean() * 100))
    q80, q90, q20 = float(win252.quantile(0.80)), float(win252.quantile(0.90)), float(win252.quantile(0.20))

    if sym in ["^VIX", "^VIX3M", "^VVIX", "^SKEW", "^MOVE"]:
      hard_cap = MACRO_HARD_CAPS.get(sym, float("inf"))
      warn_th = min(q80, hard_cap * 0.92)
      alert_th = min(max(q90, warn_th + 0.1), hard_cap)
      th_label = f"基準:{warn_th:.{digits}f}/{alert_th:.{digits}f}"
      z_v = 0.0 if pd.isna(z_score) else z_score
      if curr >= hard_cap:
        status = f"🔴絶対警戒圏(上限{hard_cap:g}突破)"
      elif curr >= alert_th and z_v >= 1.0:
        if sym in ["^VIX", "^VIX3M"]:
          status = f"🔴パニック圏({th_label})" if diff_1d > 0 else f"🟡高ボラ収縮開始({th_label})"
        elif sym == "^SKEW":
          status = f"🔴テールリスク極大({th_label})"
        elif sym == "^VVIX":
          status = f"🔴VIX急騰警報({th_label})"
        else:
          status = f"🔴金利発ショック警戒({th_label})"
      elif z_v >= 2.0:
        status = f"🔺急騰スパイク(+2σ超 | {th_label})"
      elif (curr >= warn_th and z_v >= 0.3) or curr >= alert_th or z_v >= 1.5:
        status = f"🟠警戒・変動拡大(1年{pct252}%分位 | {th_label})"
      elif curr >= warn_th and z_v < 0.3:
        status = f"🟡高止まり安定(Z:{z_v:+.1f}σ | {th_label})"
      else:
        status = f"🟢平穏・通常(1年{pct252}%分位 | 80%基準:{warn_th:.{digits}f})"
      return status, warn_th, alert_th

    if not pd.isna(z_score):
      if z_score >= 2.0:
        return f"🔺急騰スパイク(+2σ超 | 1年{pct252}%分位)", q80, q90
      if z_score <= -2.0:
        return f"🔻急落スパイク(-2σ超 | 1年{pct252}%分位)", q20, None
      if z_score >= 1.0:
        return f"↗上昇基調(1年{pct252}%分位)", q80, None
      if z_score <= -1.0:
        return f"↘下落基調(1年{pct252}%分位)", q20, None
    return f"➡️中立圏(1年{pct252}%分位)", q80, None

  rows = ["指標名 | 現在値 | 当日レンジ(安〜高) | 前日比(1D) | 5日比(5D) | 5日瞬間高値 | 20日比(20D) | 20日Zスコア | 60日位置 | 状態・警戒判定(252日自動補正)"]
  dynamic_thresholds = {}

  for sym, label, digits, unit in macro_specs:
    df = histories.get(sym, pd.DataFrame())
    if df is None or df.empty or df["Close"].dropna().empty:
      rows.append(f"{label} | 取得失敗 | - | - | - | - | - | - | - | -")
      continue
    c_series = df["Close"].dropna()
    h_series = np.maximum(df["High"].reindex(c_series.index).replace(0, np.nan).fillna(c_series), c_series) if "High" in df.columns else c_series
    l_series = np.minimum(df["Low"].reindex(c_series.index).replace(0, np.nan).fillna(c_series), c_series) if "Low" in df.columns else c_series
    curr, day_h, day_l = c_series.iloc[-1], h_series.iloc[-1], l_series.iloc[-1]

    def _calc_chg(n):
      if len(c_series) > n:
        prev = c_series.iloc[-(n + 1)]
        if prev != 0 and pd.notna(prev):
          pct = ((curr - prev) / abs(prev)) * 100
          return f"{'+' if pct > 0 else ''}{pct:.2f}%", curr - prev
      return "N/A", 0.0

    chg_1d_str, diff_1d_val = _calc_chg(1)
    chg_5d_str, _ = _calc_chg(5)
    chg_20d_str, _ = _calc_chg(20)
    max_5d_h = h_series.tail(5).max()

    if len(c_series) >= 20:
      sma20, std20 = c_series.tail(20).mean(), c_series.tail(20).std()
      z_val = (curr - sma20) / std20 if std20 and std20 > 0 else 0.0
      z_str = f"{'+' if z_val > 0 else ''}{z_val:.2f}σ"
    else:
      z_val, z_str = np.nan, "N/A"

    tail60_h, tail60_l = h_series.tail(60).max(), l_series.tail(60).min()
    pos_str = f"{int(round(((curr - tail60_l) / (tail60_h - tail60_l)) * 100))}%" if pd.notna(tail60_h) and pd.notna(tail60_l) and tail60_h > tail60_l else "N/A"
    regime_str, warn_th, alert_th = _judge_dynamic_regime(sym, digits, curr, z_val, diff_1d_val, c_series)
    dynamic_thresholds[sym] = {"warn": warn_th, "alert": alert_th}

    rows.append(
        f"{label} | {_fmt_v(curr, digits, unit)} | {_fmt_v(day_l, digits)}〜{_fmt_v(day_h, digits)} | "
        f"{chg_1d_str} | {chg_5d_str} | {_fmt_v(max_5d_h, digits, unit)} | {chg_20d_str} | {z_str} | {pos_str} | {regime_str}"
    )

  histories["_thresholds"] = dynamic_thresholds

  vix_df, vix3m_df = histories.get("^VIX", pd.DataFrame()), histories.get("^VIX3M", pd.DataFrame())
  term_line = ""
  if not vix_df.empty and not vix3m_df.empty:
    c_vix, c_vix3m = vix_df["Close"].iloc[-1], vix3m_df["Close"].iloc[-1]
    if c_vix3m > 0:
      ratio = round(c_vix / c_vix3m, 3)
      if ratio >= 1.0:
        term_state = "🔴バックワーデーション(1.00超：機関の直近プット買いパニック・急落警戒)"
      elif ratio >= 0.92:
        term_state = "🟠フラット化進行(0.92超：短期ヘッジ需要増加)"
      else:
        term_state = "🟢コンタンゴ(正常・平穏)"
      term_line = f"\n・VIX期間構造レシオ (VIX/VIX3M): {ratio} → {term_state}"

  n225_df, topix_df = histories.get("^N225", pd.DataFrame()), histories.get("1306.T", pd.DataFrame())
  jp_rel_line = ""
  if not n225_df.empty and not topix_df.empty:
    n_c, t_c = n225_df["Close"].dropna(), topix_df["Close"].dropna()
    common_jp = n_c.index.intersection(t_c.index)
    if len(common_jp) >= 21:
      n_c, t_c = n_c.loc[common_jp], t_c.loc[common_jp]
      def _rel_spread(days):
        if len(common_jp) > days:
          return ((t_c.iloc[-1] / t_c.iloc[-(days + 1)] - 1.0) - (n_c.iloc[-1] / n_c.iloc[-(days + 1)] - 1.0)) * 100.0
        return 0.0
      sp5, sp20, sp60 = _rel_spread(5), _rel_spread(20), _rel_spread(min(59, len(common_jp) - 1))
      if sp20 >= 1.0:
        jp_rot = "🔵TOPIX優位(内需・バリュー・東証全体への広範な資金流入)"
      elif sp20 <= -1.0:
        jp_rot = "🟣日経平均優位(半導体・値がさハイテク主導の指数牽引)"
      else:
        jp_rot = "⚪日経・TOPIX拮抗(セクター偏重なし)"
      jp_rel_line = f"\n・日本株インデックス相対強弱 (TOPIX[1306.T] - Nikkei225 騰落率差): 5D={sp5:+.2f}% / 20D={sp20:+.2f}% / 60D={sp60:+.2f}% → {jp_rot}"

  macro_text = (
      f"・CNN Fear & Greed Index: {fg_str}{term_line}{jp_rel_line}\n\n"
      "[グローバルマクロ＆ボラティリティ時系列マトリクス（13指標・瞬間高安値・Zスコア・252日動的基準補正付）]\n"
      + "\n".join(rows)
  )
  return macro_text, histories


@st.cache_data(ttl=1800, show_spinner=False)
def get_macro_policy_news() -> str:
  queries = [
      ("🇺🇸[米金融政策・米マクロ]", "(FOMC OR FRB OR パウエル OR 米利下げ OR 米利上げ OR 米雇用統計 OR 米CPI OR 米長期金利) when:7d", "(FOMC OR FRB OR 米雇用統計 OR 米CPI OR 米長期金利)", 3),
      ("🌍[世界情勢・通商・地政学]", "(米関税 OR トランプ関税 OR 対中規制 OR 半導体規制 OR 台湾海峡 OR 中東情勢 OR 原油先物 OR 地政学リスク) -火災 -事故 -芸能 when:7d", "(米関税 OR 対中規制 OR 半導体規制 OR 地政学リスク) -火災 -事故", 3),
      ("🏦[日銀政策・為替・国内金利]", "(日銀 決定会合 OR 日銀 利上げ OR 植田総裁 OR 為替介入 OR 円安 日銀 OR 長期金利 日銀) when:7d", "(日銀 金融政策 OR 植田総裁 OR 為替介入 OR 長期金利)", 3),
      ("🇯🇵[日本の国策・産業政策]", "(半導体 補助金 OR ラピダス OR 防衛省 OR 防衛費 OR GX 投資 OR 経済安保 OR 補正予算 OR 税制改正) -ETF -上場投信 -火災 -事故 when:7d", "(国策 半導体 OR 防衛費 OR 経済安保 OR 補正予算 OR 税制改正) -ETF -上場投信 -火災 -事故", 3),
  ]
  blacklist_sources = ["デイリー新潮", "文春オンライン", "週刊文春", "週刊新潮", "日刊ゲンダイ", "東スポ", "女性自身", "女性セブン", "ポストセブン", "アサ芸", "まいじつ", "スポーツ報知", "日刊スポーツ", "スポニチ", "サンスポ", "デイリースポーツ"]
  blacklist_words = [
      "火災で", "工場で火災", "行方不明", "交通事故", "逮捕", "不倫", "芸能",
      "上場投信", "ＥＴＦ", "ETF[", "ETF（", "グローバルＸ", "NEXT FUNDS",
      "iシェアーズ", "MAXIS", "上場インデックス", "投資法人",
      "株価・株式情報", "夜間PTS含む", "みんなの売買予想",
  ]
  preferred_sources = ["日本経済新聞", "日経", "Reuters", "ロイター", "Bloomberg", "ブルームバーグ", "株探", "Kabutan", "みんかぶ", "四季報", "東洋経済", "ダイヤモンド", "ウエルスアドバイザー", "時事通信", "共同通信", "NHK", "テレビ東京", "テレ東", "野村證券", "大和証券"]

  def _fetch_category_rss(tag: str, q_recent: str, q_fallback: str, limit: int):
    def _parse_query(q_str):
      url = f"https://news.google.com/rss/search?q={urllib.parse.quote(q_str)}&hl=ja&gl=JP&ceid=JP:ja"
      try:
        res = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=6)
        feed = feedparser.parse(res.content) if (res.status_code == 200 and res.content) else feedparser.parse(url)
      except Exception:
        try:
          feed = feedparser.parse(url)
        except Exception:
          return []
      candidates = []
      for entry in getattr(feed, "entries", [])[: limit + 10]:
        raw_title = getattr(entry, "title", "")
        if not raw_title:
          continue
        parts = raw_title.rsplit(" - ", 1)
        title = parts[0].strip()
        src_obj = entry.get("source")
        src_name = ((src_obj.get("title") if isinstance(src_obj, dict) else "") or (parts[1].strip() if len(parts) > 1 else "") or "ニュース配信").strip()
        if not title or any(bs in src_name or bs in raw_title for bs in blacklist_sources) or any(bw in title for bw in blacklist_words):
          continue
        pub = entry.get("published", "")[:16]
        is_pref = any(ps in src_name or ps in raw_title for ps in preferred_sources)
        candidates.append((0 if is_pref else 1, title, f"・{tag} {title} [出典: {src_name}] ({pub})"))
      candidates.sort(key=lambda x: x[0])
      return [(t, l) for _, t, l in candidates]
    return (_parse_query(q_recent) or _parse_query(q_fallback))[: limit + 2]

  results_by_idx = {}
  with ThreadPoolExecutor(max_workers=4) as executor:
    future_map = {executor.submit(_fetch_category_rss, tag, qr, qf, lim): (idx, lim) for idx, (tag, qr, qf, lim) in enumerate(queries)}
    for f in as_completed(future_map):
      idx, lim = future_map[f]
      try:
        results_by_idx[idx] = (f.result(), lim)
      except Exception:
        results_by_idx[idx] = ([], lim)

  unique_lines, seen_titles = [], []
  def _is_duplicate_topic(new_title: str) -> bool:
    norm_new = re.sub(r"[\s ・、。「」『』（）()【】\[\]―-]+", "", new_title)
    for old_t in seen_titles:
      if norm_new[:14] == old_t[:14] or difflib.SequenceMatcher(None, norm_new, old_t).ratio() >= 0.52:
        return True
      if "日銀" in norm_new and "日銀" in old_t and "主な意見" in norm_new and "主な意見" in old_t:
        return True
    return False

  for idx in sorted(results_by_idx.keys()):
    cand_list, lim = results_by_idx[idx]
    cat_count = 0
    for title, line in cand_list:
      if not _is_duplicate_topic(title):
        seen_titles.append(re.sub(r"[\s ・、。「」『』（）()【】\[\]―-]+", "", title))
        unique_lines.append(line)
        cat_count += 1
        if cat_count >= lim:
          break
  return "\n".join(unique_lines) if unique_lines else "・直近のマクロ・金融政策・国策に関する主要ヘッドラインは取得されませんでした（Step 0のGoogle検索で補完してください）。"


def _get_edinet_key_and_type() -> tuple:
  raw_key = st.secrets.get("EDINET_API_KEY", "") or st.secrets.get("EDINETDB_API_KEY", "")
  clean_key = str(raw_key).strip().strip('"').strip("'")
  if not clean_key:
    return "", "missing"
  return (clean_key, "edinetdb") if clean_key.lower().startswith("edb_") else (clean_key, "fsa")


def _run_edinet_connectivity_test(edinet_key: str, key_type: str) -> dict:
  def _st(ok: bool, ktype: str, sname: str, msg: str, guide: str) -> dict:
    return {"ok": ok, "key_type": ktype, "service_name": sname, "message": msg, "guide": guide}

  if not edinet_key or key_type == "missing":
    return _st(False, "missing", "未設定", "Streamlit Secrets に EDINET_API_KEY が設定されていません。", 'Streamlit Secrets に `EDINET_API_KEY = "..."` を設定してください。（金融庁公式EDINET API v2キー、または `edb_` で始まる EDINET DB APIキーの両方に自動対応しています）')

  if key_type == "edinetdb":
    sname = "EDINET DB API (edinetdb.jp)"
    try:
      res = requests.get("https://edinetdb.jp/v1/companies/E02144", headers={"X-API-Key": edinet_key}, timeout=10)
      if res.status_code == 200:
        return _st(True, "edinetdb", sname, "🟢 EDINET DB API (edinetdb.jp) 正常接続確認済 (HTTP 200)", "現在 `edb_` 始まりのEDINET DBキーで接続中です。有報XBRLベースの確定財務時系列・信用スコアを自動取得します。")
      if res.status_code in [401, 403]:
        return _st(False, "edinetdb", sname, f"🔴 EDINET DB API 認証エラー (HTTP {res.status_code})", "設定されている `edb_...` キーが無効または失効しています。edinetdb.jp/developers で確認してください。")
      if res.status_code == 429:
        return _st(False, "edinetdb", sname, "🟠 EDINET DB API レートリミット到達 (HTTP 429)", "EDINET DB (Freeプラン: 1日100リクエスト上限) の制限に達しています。")
      return _st(False, "edinetdb", sname, f"🟠 EDINET DB API 応答異常 (HTTP {res.status_code})", "EDINET DBサーバー側の一時的な障害または仕様変更の可能性があります。")
    except Exception as e:
      return _st(False, "edinetdb", sname, f"🔴 EDINET DB API 通信エラー: {e}", "ネットワーク接続または edinetdb.jp の稼働状況を確認してください。")

  sname = "金融庁公式 EDINET API v2"
  today_str = datetime.datetime.now(JST).strftime("%Y-%m-%d")
  is_hex32 = bool(re.match(r"^[0-9a-fA-F]{32}$", edinet_key))
  try:
    res = requests.get("https://api.edinet-fsa.go.jp/api/v2/documents.json", params={"date": today_str, "type": 1, "Subscription-Key": edinet_key}, headers={"Ocp-Apim-Subscription-Key": edinet_key}, timeout=10)
    data = res.json() if res.content else {}
    meta = data.get("metadata", {}) if isinstance(data, dict) else {}
    if res.status_code == 200 and str(meta.get("status", "")) == "200":
      return _st(True, "fsa", sname, f"🟢 金融庁公式 EDINET API v2 正常接続確認済 (HTTP 200 / {today_str})", "直近60日間の公式開示書類（有報・臨時報告書・大量保有報告書等）を自動取得します。")
    msg = data.get("message") or meta.get("message") or f"HTTP {res.status_code}"
    fmt_hint = "" if is_hex32 else "（※設定値が32桁の16進数ではないため、別サービスのキーまたはコピー漏れの可能性があります）"
    return _st(False, "fsa", sname, f"🔴 金融庁公式 EDINET API 認証失敗: {msg}", f"金融庁EDINET API (`api.edinet-fsa.go.jp`) で認証が拒否されました{fmt_hint}。Secretsの `EDINET_API_KEY` を確認してください。")
  except Exception as e:
    return _st(False, "fsa", sname, f"🔴 金融庁公式 EDINET API 通信エラー: {e}", "ネットワーク接続または金融庁EDINET APIのメンテナンス状況を確認してください。")


def check_edinet_api_status(force_refresh: bool = False) -> dict:
  cache_key = "_edinet_api_status_cache"
  edinet_key, key_type = _get_edinet_key_and_type()
  if not force_refresh and cache_key in st.session_state:
    cached_res, cached_ts, cached_key_hash = st.session_state[cache_key]
    if time.time() - cached_ts < 3600 and cached_key_hash == hash(edinet_key):
      return cached_res
  status_dict = _run_edinet_connectivity_test(edinet_key, key_type)
  st.session_state[cache_key] = (status_dict, time.time(), hash(edinet_key))
  return status_dict


def verify_edinet_api_key() -> tuple:
  status = check_edinet_api_status(force_refresh=True)
  msg = status["message"] + (f"\n💡 {status['guide']}" if status.get("guide") and not status["ok"] else "")
  return status["ok"], msg


def _fetch_edinetdb_company_summary(stock_code_4digit: str, edinet_code: str, api_key: str) -> tuple:
  headers, base_url = {"X-API-Key": api_key}, "https://edinetdb.jp/v1"
  edinet_code = edinet_code or _resolve_edinet_code(stock_code_4digit, edb_api_key=api_key)
  if not edinet_code:
    return "EDINETコードリストおよびEDINET DB検索に該当銘柄が見つかりませんでした。", []

  lines = [f"※接続ソース: EDINET DB API (`edinetdb.jp` / EDINETコード: {edinet_code})"]
  try:
    comp_res = requests.get(f"{base_url}/companies/{edinet_code}", headers=headers, timeout=10)
    if comp_res.status_code in [401, 403]:
      return f"EDINET DB API 認証エラー (HTTP {comp_res.status_code} — Secretsのキーを確認してください)", []
    if comp_res.status_code == 429:
      return "EDINET DB API レートリミット超過 (HTTP 429 — 1日100リクエスト上限に到達しました)", []
    if comp_res.status_code == 200:
      c_json = comp_res.json()
      c_data = c_json.get("data", c_json) if isinstance(c_json, dict) else {}
      if isinstance(c_data, dict):
        acc_std = c_data.get("accounting_standard") or "不明"
        c_score, c_rating = c_data.get("credit_score"), c_data.get("credit_rating") or ""
        score_str = f"{c_score} ({c_rating})" if c_score is not None else "N/A"
        lines.append(f"・有報基本属性: 業種={c_data.get('industry') or ''} | 会計基準={acc_std} | EDINET DB財務健全性スコア={score_str}")
  except Exception as e:
    lines.append(f"・企業概要取得エラー: {e}")

  try:
    fin_res = requests.get(f"{base_url}/companies/{edinet_code}/financials", params={"years": 3}, headers=headers, timeout=10)
    if fin_res.status_code == 200:
      f_json = fin_res.json()
      f_list = f_json.get("data", []) if isinstance(f_json, dict) else []
      if isinstance(f_list, list) and f_list:
        lines.append("・有価証券報告書 確定財務推移（直近最大3期）:")
        def _fmt_oku(val):
          return (f"{val / 1e8:,.1f}億円" if abs(val) >= 1e6 else f"{val:,}") if isinstance(val, (int, float)) and pd.notna(val) else "N/A"
        def _fmt_val(val, suffix=""):
          return f"{round(val, 2)}{suffix}" if isinstance(val, (int, float)) and pd.notna(val) else "N/A"

        for row in f_list[-3:]:
          if not isinstance(row, dict):
            continue
          fy = row.get("fiscal_year") or row.get("period_end") or row.get("year") or "期不明"
          rev, op, ni = _fmt_oku(row.get("revenue") or row.get("net_sales")), _fmt_oku(row.get("operating_income")), _fmt_oku(row.get("net_income"))
          eps_raw, bps_raw, roe_v = row.get("eps"), row.get("bps"), row.get("roe")
          if isinstance(roe_v, (int, float)) and pd.notna(roe_v):
            roe_s = f"{round(roe_v * 100, 1)}%" if abs(roe_v) <= 1.5 else f"{round(roe_v, 1)}%"
          elif isinstance(eps_raw, (int, float)) and isinstance(bps_raw, (int, float)) and bps_raw > 0:
            roe_s = f"{round((eps_raw / bps_raw) * 100, 1)}%(EPS/BPS算出)"
          else:
            roe_s = "N/A"
          lines.append(f"  [{fy}] 売上:{rev} | 営業益:{op} | 純利益:{ni} | EPS:{_fmt_val(eps_raw, '円')} | BPS:{_fmt_val(bps_raw, '円')} | DPS:{_fmt_val(row.get('dps') or row.get('dividend_per_share'), '円')} | ROE:{roe_s}")
  except Exception as e:
    lines.append(f"・財務時系列取得エラー: {e}")

  lines.append("（※注記: 現在EDINET DBキー `edb_...` で接続中のため有報XBRL確定財務データを表示しています。直近60日間の日次提出書類［臨時報告書・大量保有報告書等］は株探開示・Googleニュースから補完評価してください）")
  return "\n".join(lines), []


@st.cache_data(ttl=3600)
def get_edinet_documents(stock_code_4digit, days=60):
  edinet_key, key_type = _get_edinet_key_and_type()
  if key_type == "missing":
    return "EDINET APIキー未設定", []
  code_str = str(stock_code_4digit).strip().upper()
  edinet_code = _resolve_edinet_code(code_str, edb_api_key=edinet_key if key_type == "edinetdb" else "")
  if key_type == "edinetdb":
    return _fetch_edinetdb_company_summary(code_str, edinet_code, edinet_key)
  if not edinet_code:
    return "EDINETコードリストに該当銘柄がありません", []

  today_jst = datetime.datetime.now(JST).date()
  jp_holidays = get_dynamic_jp_holidays(today_jst.year) | get_dynamic_jp_holidays(today_jst.year - 1)
  date_list = [(today_jst - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days) if (today_jst - timedelta(days=i)).weekday() < 5 and (today_jst - timedelta(days=i)) not in jp_holidays]
  if not date_list:
    return f"直近{days}日間のEDINET公式開示書類はありません。", []

  all_found, all_reasons, error_statuses = [], [], []
  def fetch_edinet_for_date(target_date):
    url = "https://api.edinet-fsa.go.jp/api/v2/documents.json"
    params, headers = {"date": target_date, "type": 2, "Subscription-Key": edinet_key}, {"Ocp-Apim-Subscription-Key": edinet_key}
    found_docs, reasons, err_msg = [], [], None
    for attempt in range(3):
      try:
        res = requests.get(url, params=params, headers=headers, timeout=12)
        data = res.json() if res.content else None
        if isinstance(data, dict):
          meta = data.get("metadata")
          meta_status = str(meta.get("status", "")).strip() if isinstance(meta, dict) else ""
          top_code = data.get("StatusCode") or data.get("statusCode")
          if res.status_code == 200 and meta_status == "200" and isinstance(data.get("results"), list):
            err_msg = None
            for doc in data["results"]:
              if not isinstance(doc, dict):
                continue
              doc_ed, doc_sec = str(doc.get("edinetCode") or "").strip(), str(doc.get("secCode") or "").strip().upper()
              if (edinet_code and doc_ed == edinet_code) or (code_str and doc_sec.startswith(code_str)):
                title, desc, reason = doc.get("title") or "タイトル不明", doc.get("docDescription"), doc.get("currentReportReason")
                details = ([f"概要: {desc}"] if desc else []) + ([f"事由: {reason}"] if reason else [])
                if reason and any(k in title for k in ["臨時報告書", "修正", "配当"]):
                  reasons.append(reason)
                found_docs.append(f"[{target_date}] {title} ({' | '.join(details) if details else '詳細記載なし'})")
            break
          code_label = top_code or meta_status or res.status_code
          msg_label = data.get("message") or (meta.get("message") if isinstance(meta, dict) else "") or "InvalidResponse"
          err_msg = f"Status:{code_label}({str(msg_label)[:80]})"
          if str(code_label) in ["401", "403"]:
            break
        else:
          err_msg = f"HTTP:{res.status_code}"
          if res.status_code in [401, 403]:
            break
      except Exception:
        err_msg = "Timeout/ConnError"
      if attempt < 2:
        time.sleep(0.5 * (attempt + 1))
    return found_docs, reasons, err_msg

  first_docs, first_reasons, first_err = fetch_edinet_for_date(date_list[0])
  if first_err and any(c in first_err for c in ["Status:401", "Status:403", "HTTP:401", "HTTP:403"]):
    return f"EDINET API認証エラーのため取得を中断しました（{first_err} — Streamlit SecretsのEDINET_API_KEY設定またはキー有効期限を確認してください）。", []

  all_found.extend(first_docs)
  all_reasons.extend(first_reasons)
  if first_err:
    error_statuses.append(first_err)
  if len(date_list) > 1:
    with ThreadPoolExecutor(max_workers=4) as executor:
      for f in as_completed([executor.submit(fetch_edinet_for_date, d) for d in date_list[1:]]):
        docs, reasons, err = f.result()
        all_found.extend(docs)
        all_reasons.extend(reasons)
        if err:
          error_statuses.append(err)

  all_found.sort(reverse=True)
  if all_found:
    res_str = "\n".join(all_found) + (f"\n（※一部日程でEDINET API応答エラー {len(error_statuses)}件: {', '.join(sorted(set(error_statuses)))}）" if error_statuses else "")
  elif error_statuses:
    res_str = f"直近{days}日間のEDINET開示書類は検出されませんでした（※APIエラー {len(error_statuses)}/{len(date_list)}営業日発生: {', '.join(sorted(set(error_statuses)))}）。"
  else:
    res_str = f"直近{days}日間のEDINET公式開示書類はありません。"
  return res_str, list(set(all_reasons))


@st.cache_data(ttl=3600)
def get_news(company_name, is_jp, edinet_reasons=None):
  def _fetch_rss_items(url, limit, prefix="", filter_jp=False):
    items = []
    try:
      feed = feedparser.parse(url)
      for entry in feed.entries[:limit]:
        raw_title = getattr(entry, "title", "")
        parts = raw_title.rsplit(" - ", 1)
        title = parts[0].strip()
        src_obj = entry.get("source")
        src_name = ((src_obj.get("title") if isinstance(src_obj, dict) else "") or (parts[1].strip() if len(parts) > 1 else "") or "ニュース配信").strip()
        if filter_jp and not (company_name in title or any(kw in title for kw in ["TOPIX", "再編", "決算", "配当", "株", "業績", "アナリスト", "レーティング", "目標"])):
          continue
        items.append(f"■ {prefix}{title} [出典: {src_name}] ({entry.get('published', '')[:16]})")
    except Exception:
      pass
    return items

  if is_jp:
    q1 = urllib.parse.quote(f"{company_name} (TOPIX OR 再編 OR アナリスト OR レーティング OR 目標株価 OR 株探 OR 四季報 OR 日経 OR 決算 OR 増配)")
    news_list = _fetch_rss_items(f"https://news.google.com/rss/search?q={q1}&hl=ja&gl=JP&ceid=JP:ja", 8, filter_jp=True)
  else:
    q1 = urllib.parse.quote(f"{company_name} stock (earnings OR upgrade OR target OR guidance)")
    news_list = _fetch_rss_items(f"https://news.google.com/rss/search?q={q1}&hl=en-US&gl=US&ceid=US:en", 8, filter_jp=False)

  if is_jp and edinet_reasons:
    for reason in edinet_reasons:
      match = re.search(r"（(.*?)）", reason)
      kw = (match.group(1) if match else reason[:15]).replace("の件", "").replace("に関する", "")
      q2 = urllib.parse.quote(f"{company_name} {kw}")
      news_list.extend(_fetch_rss_items(f"https://news.google.com/rss/search?q={q2}&hl=ja&gl=JP&ceid=JP:ja", 3, prefix="[EDINET連動深掘り] "))

  unique_news, seen_titles = [], set()
  for item in news_list:
    t = item.split(" [出典:")[0]
    if t not in seen_titles:
      seen_titles.add(t)
      unique_news.append(item)
  return "\n\n".join(unique_news[:9]) if unique_news else "直近の重要な関連ニュースは見つかりませんでした。"


@st.cache_data(ttl=3600)
def get_japanese_name(stock_code):
  match_row = _match_edinet_row(stock_code)
  if not match_row.empty:
    raw_name = str(match_row["提出者名"].values[0])
    for rm in ["株式会社", "ホールディングス", "グループ本社", "グループ"]:
      raw_name = raw_name.replace(rm, "")
    return raw_name.strip()
  try:
    m = re.search(r"<title>(.*?)【", _http_get_html(f"https://kabutan.jp/stock/?code={stock_code}", timeout=5))
    if m:
      return m.group(1).strip()
  except Exception:
    pass
  return None


@st.cache_data(ttl=3600)
def get_us_stock_name(stock_code):
  code_up = stock_code.strip().upper()
  try:
    res = requests.get(f"https://query2.finance.yahoo.com/v1/finance/search?q={code_up}", headers={"User-Agent": USER_AGENT}, timeout=5)
    if res.status_code == 200:
      for q in res.json().get("quotes", []):
        if q.get("symbol", "").upper() == code_up:
          return q.get("longname") or q.get("shortname") or code_up
  except Exception:
    pass
  try:
    info = yf.Ticker(code_up).info
    if isinstance(info, dict):
      return info.get("longName") or info.get("shortName") or code_up
  except Exception:
    pass
  return code_up


def _estimate_next_earnings_period(announce_date_str: str) -> str:
  try:
    parts = [int(x) for x in announce_date_str.strip().split("/")]
    if len(parts) == 3:
      y, m, d = parts
      next_y, next_m = y + (m + 2) // 12, (m + 2) % 12 + 1
      jun = "上旬" if d <= 10 else ("中旬" if d <= 20 else "下旬")
      return f" / 次回目安:{next_y}年{next_m}月{jun}頃[+3ヶ月推計]"
  except Exception:
    pass
  return ""


def _normalize_jp_market_segment(raw_market: str) -> str:
  if not raw_market:
    return ""
  s = str(raw_market).strip()
  if any(k in s for k in ["東証Ｐ", "東証P", "プライム", "PRM", "Prime"]):
    return "東証プライム"
  if any(k in s for k in ["東証Ｓ", "東証S", "スタンダード", "STD", "Standard"]):
    return "東証スタンダード"
  if any(k in s for k in ["東証Ｇ", "東証G", "グロース", "GRT", "Growth"]):
    return "東証グロース"
  for reg in ["名証", "札証", "福証"]:
    if reg in s:
      return reg
  return ""


def evaluate_topix_passive_status(market_seg: str, market_cap_str: str) -> str:
  seg_label = market_seg if market_seg and market_seg != "N/A" else "東証上場(詳細区分要確認)"
  mcap_oku = None
  if market_cap_str and market_cap_str != "N/A":
    m = re.search(r"([0-9,]+(?:\.[0-9]+)?)", str(market_cap_str))
    if m:
      try:
        mcap_oku = float(m.group(1).replace(",", ""))
      except Exception:
        mcap_oku = None
  prefix = f"上場市場: {seg_label} | TOPIXパッシブ需給区分: "
  if mcap_oku is None:
    return f"{prefix}時価総額取得不可のため要検索確認"
  if seg_label in ["名証", "札証", "福証"]:
    return f"{prefix}⚪TOPIX非構成銘柄圏（地方単独上場・TOPIXパッシブ需給影響なし）"
  if mcap_oku >= 500.0:
    return f"{prefix}🟢TOPIX中核・継続採用圏（時価総額{mcap_oku:,.1f}億円：500億円超の主力銘柄・段階的ウエイト低減リスクなし）"
  if mcap_oku >= 250.0:
    return (
        f"{prefix}🔵TOPIX継続採用圏（時価総額{mcap_oku:,.1f}億円：浮動株比率が極端に低くない限り継続採用圏）"
        if "プライム" in seg_label
        else f"{prefix}🔵TOPIX第2段階・新規採用候補/継続採用圏（時価総額{mcap_oku:,.1f}億円：250億円以上のため新規採用基準候補圏・要検索確認）"
    )
  if mcap_oku >= 100.0:
    return f"{prefix}🟡TOPIX第2段階入替・境界圏（時価総額{mcap_oku:,.1f}億円：浮動株時価総額100億円基準の当落線上・1/4/7/10月末リバランス影響は要検索確認）"
  if "プライム" in seg_label:
    return f"{prefix}🟠段階的ウエイト低減警戒圏（時価総額{mcap_oku:,.1f}億円：100億円未満のため2026年10月〜2028年7月の四半期ごと8段階ウエイト低減対象の可能性大）"
  return f"{prefix}⚪TOPIX非構成銘柄圏（時価総額{mcap_oku:,.1f}億円：100億円未満のスタンダード/グロース銘柄・パッシブ需給影響軽微）"


def get_japanese_fundamentals(stock_code):
  cache_key = f"fund_cache_{stock_code}"
  if cache_key in st.session_state:
    cached_val, cached_ts = st.session_state[cache_key]
    if time.time() - cached_ts < 3600 and cached_val.get("market_segment", "N/A") != "N/A":
      return cached_val

  result = {"per": "N/A", "pbr": "N/A", "div": "N/A", "margin": "N/A", "market_cap": "N/A", "earnings_date": "要検索確認", "market_segment": "N/A"}

  def _clean_num(raw_str):
    if not raw_str:
      return "N/A"
    s = re.sub(r"\s+", "", str(raw_str)).replace(",", "")
    if any(x in s for x in ["－", "―", "---", "赤字"]) or s == "-":
      return "－"
    m = re.search(r"[0-9]+(?:\.[0-9]+)?", s)
    return m.group(0) if m else "N/A"

  def _extract_regex(pattern, text):
    m = re.search(pattern, text)
    return m.group(1).replace(",", "") if (m and m.group(1) not in ["---", "－", "―"]) else None

  # 1. 株探 (kabutan.jp)
  try:
    html_text = _http_get_html(f"https://kabutan.jp/stock/?code={stock_code}", timeout=8)
    if html_text:
      soup = BeautifulSoup(html_text, "html.parser")
      page_txt = soup.get_text(" ", strip=True)
      mkt_span = soup.find("span", class_="market")
      if mkt_span:
        result["market_segment"] = _normalize_jp_market_segment(mkt_span.get_text(strip=True)) or "N/A"
      if result["market_segment"] == "N/A":
        m_mkt = re.search(r"(東証[ＰＳＧPSG]|東証プライム|東証スタンダード|東証グロース|名証[ＰＭＮ]?|札証|福証)", page_txt)
        if m_mkt:
          result["market_segment"] = _normalize_jp_market_segment(m_mkt.group(1)) or "N/A"
      info_div = soup.find("div", id="stockinfo_i3")
      if info_div:
        tds = [td.get_text(strip=True) for td in info_div.find_all("td")]
        if len(tds) >= 4:
          per_v, pbr_v, div_v, mrg_v = [_clean_num(tds[i]) for i in range(4)]
          result["per"] = "測定不能(赤字・未定等)" if per_v == "－" else per_v
          result["pbr"] = pbr_v if pbr_v != "－" else "N/A"
          result["div"] = div_v if div_v != "－" else "N/A"
          result["margin"] = mrg_v if mrg_v != "－" else "N/A"
        if len(tds) >= 5 and "億円" in tds[4]:
          result["market_cap"] = tds[4].replace("億円", "").strip()
      if result["market_cap"] == "N/A":
        m_mc = re.search(r"時価総額\s*([0-9,]+(?:\.[0-9]+)?)\s*億円", page_txt)
        if m_mc:
          result["market_cap"] = m_mc.group(1)
      earn_m = re.search(r"決算発表予定日[^0-9]*?([0-9]{4}年[0-9]{1,2}月[0-9]{1,2}日|[0-9]{4}/[0-9]{1,2}/[0-9]{1,2}|[0-9]{1,2}月[0-9]{1,2}日)", page_txt)
      if earn_m:
        result["earnings_date"] = f"{earn_m.group(1)}頃(予定)"
      else:
        fy_m = re.search(r"(?:[連単]\s*)?([0-9]{2,4})\.([0-9]{2})\s*(?:予|実|連|単)", page_txt) or re.search(r"([0-9]{2})\.([0-9]{2})\s*[0-9,－―]+", page_txt)
        if fy_m and 1 <= int(fy_m.group(2)) <= 12:
          result["earnings_date"] = f"本決算 {int(fy_m.group(2))}月期 (次回発表日は要検索確認)"
  except Exception:
    pass

  # 2. Yahoo!ファイナンス日本版
  if any(result[k] == "N/A" for k in ["per", "pbr", "div", "margin", "market_cap", "market_segment"]) or result["earnings_date"] == "要検索確認":
    try:
      yh_html = _http_get_html(f"https://finance.yahoo.co.jp/quote/{stock_code}.T", timeout=8)
      if yh_html:
        full_txt = BeautifulSoup(yh_html, "html.parser").get_text(" ", strip=True)
        if result["market_segment"] == "N/A":
          m_ym = re.search(r"(東証プライム|東証スタンダード|東証グロース|東証PRM|東証STD|東証GRT)", full_txt)
          if m_ym:
            result["market_segment"] = _normalize_jp_market_segment(m_ym.group(1)) or "N/A"
        if result["margin"] == "N/A":
          result["margin"] = _extract_regex(r"信用倍率.{0,20}?([0-9,]+(?:\.[0-9]+)?|---|－|―)\s*倍", full_txt) or "N/A"
        if result["per"] == "N/A":
          m_per = re.search(r"PER\s*（会社予想）.{0,25}?([0-9,]+(?:\.[0-9]+)?|---|－|―)\s*倍", full_txt)
          if m_per:
            val_p = m_per.group(1).replace(",", "")
            result["per"] = "測定不能(赤字・未定等)" if val_p in ["---", "－", "―"] else val_p
        if result["pbr"] == "N/A":
          result["pbr"] = _extract_regex(r"PBR\s*（実績）.{0,25}?([0-9,]+(?:\.[0-9]+)?|---|－|―)\s*倍", full_txt) or "N/A"
        if result["div"] == "N/A":
          result["div"] = _extract_regex(r"配当利回り\s*（会社予想）.{0,25}?([0-9,]+(?:\.[0-9]+)?|---|－|―)\s*%", full_txt) or "N/A"
        if result["market_cap"] == "N/A":
          v = _extract_regex(r"時価総額.{0,25}?([0-9,]+)\s*百万円", full_txt)
          if v:
            result["market_cap"] = f"{round(float(v) / 100, 1):,}"
        if result["earnings_date"] == "要検索確認":
          m_yh_sum = re.search(r"([0-9]{4}/[0-9]{1,2}/[0-9]{1,2})\s*発表分.{0,40}?([0-9]{4}年[0-9]{1,2}月期\s*(?:第[1-4１-４]四半期|中間期|通期)?)", full_txt)
          if m_yh_sum:
            ann_dt, fy_term = m_yh_sum.group(1).strip(), m_yh_sum.group(2).strip()
            result["earnings_date"] = f"{fy_term} (直近発表:{ann_dt}{_estimate_next_earnings_period(ann_dt)})"
          else:
            m_yh_fy = re.search(r"(?:1株配当|EPS)（会社予想）.{0,30}?(20[0-9]{2})/([0-9]{1,2})", full_txt)
            if m_yh_fy:
              result["earnings_date"] = f"本決算 {int(m_yh_fy.group(2))}月期 ({m_yh_fy.group(1)}/{m_yh_fy.group(2)}期)"
    except Exception:
      pass

  # 3. 日経電子版 ＆ 4. みんかぶ
  fallback_sources = [
      (f"https://www.nikkei.com/nkd/company/?scode={stock_code}", r"予想PER.{0,25}?([0-9,]+(?:\.[0-9]+)?)\s*倍", r"PBR(?:（実績）)?.{0,25}?([0-9,]+(?:\.[0-9]+)?)\s*倍", r"予想配当利回り.{0,25}?([0-9,]+(?:\.[0-9]+)?)\s*[％%]", r"時価総額.{0,25}?([0-9,]+(?:\.[0-9]+)?)\s*億円", False),
      (f"https://minkabu.jp/stock/{stock_code}", r"PER\s*(?:\(調整後\))?.{0,20}?([0-9,]+(?:\.[0-9]+)?)\s*倍", r"PBR.{0,20}?([0-9,]+(?:\.[0-9]+)?)\s*倍", r"配当利回り.{0,20}?([0-9,]+(?:\.[0-9]+)?)\s*[％%]", r"時価総額.{0,20}?([0-9,]+)\s*百万円", True),
  ]
  for url, p_per, p_pbr, p_div, p_mc, is_million in fallback_sources:
    if not any(result[k] == "N/A" for k in ["per", "pbr", "div", "market_cap", "market_segment"]):
      break
    try:
      sub_html = _http_get_html(url, timeout=8)
      if not sub_html:
        continue
      sub_txt = BeautifulSoup(sub_html, "html.parser").get_text(" ", strip=True)
      if result["market_segment"] == "N/A":
        m_seg = re.search(r"(東証プライム|東証スタンダード|東証グロース)", sub_txt)
        if m_seg:
          result["market_segment"] = m_seg.group(1)
      if result["per"] == "N/A":
        result["per"] = _extract_regex(p_per, sub_txt) or "N/A"
      if result["pbr"] == "N/A":
        result["pbr"] = _extract_regex(p_pbr, sub_txt) or "N/A"
      if result["div"] == "N/A":
        result["div"] = _extract_regex(p_div, sub_txt) or "N/A"
      if result["market_cap"] == "N/A":
        v_mc = _extract_regex(p_mc, sub_txt)
        if v_mc:
          result["market_cap"] = f"{round(float(v_mc) / 100, 1):,}" if is_million else v_mc
    except Exception:
      pass

  # 5. EdinetcodeDlInfo.csv からの最終補完
  if result["earnings_date"] == "要検索確認":
    match_row = _match_edinet_row(stock_code)
    if not match_row.empty and "決算日" in match_row.columns:
      kessan_bi = str(match_row["決算日"].values[0]).strip()
      m_kb = re.search(r"([0-9]{1,2})月", kessan_bi)
      if m_kb:
        result["earnings_date"] = f"本決算 {int(m_kb.group(1))}月期 (EDINET基準:{kessan_bi})"
      elif kessan_bi and kessan_bi.lower() != "nan":
        result["earnings_date"] = f"決算日:{kessan_bi} (次回発表日は要検索確認)"

  if result["market_segment"] != "N/A" and any(result[k] != "N/A" for k in ["per", "pbr", "market_cap", "div"]):
    st.session_state[cache_key] = (result, time.time())
  return result


@st.cache_data(ttl=3600)
def search_japanese_code_by_name(query):
  if not query:
    return []
  try:
    df_code = _load_edinet_csv().dropna(subset=["証券コード"]).copy()
    def _fmt_sec_code(val):
      s = str(val).strip()
      s = s[:-2] if s.endswith(".0") else s
      return s[:4] if len(s) == 5 and s.endswith("0") else s

    df_code["証券コード_4桁"] = df_code["証券コード"].apply(_fmt_sec_code)
    df_code["提出者名"] = df_code["提出者名"].fillna("").astype(str)
    df_code["提出者名（ヨミ）"] = df_code["提出者名（ヨミ）"].fillna("").astype(str)
    mask = df_code["提出者名"].str.contains(query, case=False, na=False) | df_code["提出者名（ヨミ）"].str.contains(query, case=False, na=False)
    matches = df_code[mask].copy()
    if matches.empty:
      close_names = difflib.get_close_matches(query, df_code["提出者名"].tolist(), n=10, cutoff=0.4)
      close_yomis = difflib.get_close_matches(query, df_code["提出者名（ヨミ）"].tolist(), n=10, cutoff=0.4)
      matches = df_code[df_code["提出者名"].isin(close_names) | df_code["提出者名（ヨミ）"].isin(close_yomis)].copy()
    matches["sort_key"] = pd.to_numeric(matches["証券コード_4桁"], errors="coerce").fillna(99999)
    matches = matches.sort_values("sort_key")
    return [f"{row['証券コード_4桁']} - {row['提出者名']}" for _, row in matches.iterrows()]
  except Exception:
    return []


@st.cache_data(ttl=3600)
def search_us_ticker_by_name(query):
  if not query:
    return []
  def _extract_quotes(quotes_list):
    return [f"{q.get('symbol', '')} - {q.get('shortname') or q.get('longname') or ''}" for q in quotes_list if q.get("quoteType") in ["EQUITY", "ETF"] and q.get("symbol")][:10]
  try:
    res = requests.get(f"https://query2.finance.yahoo.com/v1/finance/search?q={urllib.parse.quote(query)}", headers={"User-Agent": USER_AGENT}, timeout=5)
    if res.status_code == 200:
      matches = _extract_quotes(res.json().get("quotes", []))
      if matches:
        return matches
  except Exception:
    pass
  try:
    return _extract_quotes(getattr(yf.Search(query, max_results=10), "quotes", []))
  except Exception:
    return []