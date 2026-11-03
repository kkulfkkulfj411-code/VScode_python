# ==============================================================================
# AI株式分析データジェネレーター - data_fetcher.py (完全版 v2026.11)
# 実装済仕様：東証年末年始の振替休日誤判定修正 / 日本株ファンダ5段階フォールバック /
#            取得失敗時の非キャッシュ保護 / EDINET API稼働日フィルタ＆1日目Fail-Fast /
#            EDINET API接続テスト関数(verify_edinet_api_key)
# ==============================================================================
import calendar
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime
from datetime import date, time as dt_time, timedelta, timezone
import difflib
import html
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


def _http_get_html(url: str, timeout: int = 8) -> str:
  if HAS_CURL_CFFI:
    try:
      res = cffi_requests.get(
          url,
          headers={"Accept-Language": "ja,en-US;q=0.9,en;q=0.8"},
          impersonate="chrome",
          timeout=timeout,
      )
      if res.status_code == 200 and len(res.content) > 500:
        return res.content.decode("utf-8", errors="ignore")
    except Exception:
      pass

  headers = {
      "User-Agent": (
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0"
          " Safari/537.36"
      ),
      "Accept": (
          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
      ),
      "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
      "Cache-Control": "no-cache",
  }
  try:
    res = requests.get(url, headers=headers, timeout=timeout)
    if res.status_code == 200 and len(res.content) > 500:
      return res.content.decode("utf-8", errors="ignore")
  except Exception:
    pass
  return ""


def get_dynamic_jp_holidays(year: int) -> set:
  holidays = set()
  # 国民の祝日（固定日）※1/2, 1/3, 12/31は振替休日の対象外のため後段で追加
  national_fixed_days = [
      (1, 1),
      (2, 11),
      (2, 23),
      (4, 29),
      (5, 3),
      (5, 4),
      (5, 5),
      (8, 11),
      (11, 3),
      (11, 23),
  ]
  for m, d in national_fixed_days:
    holidays.add(date(year, m, d))

  def nth_monday(y, m, n):
    first_day = date(y, m, 1)
    first_monday_day = 1 + (7 - first_day.weekday()) % 7
    return date(y, m, first_monday_day + (n - 1) * 7)

  holidays.add(nth_monday(year, 1, 2))
  holidays.add(nth_monday(year, 7, 3))
  holidays.add(nth_monday(year, 9, 3))
  holidays.add(nth_monday(year, 10, 2))

  vernal_day = int(
      20.8431 + 0.242194 * (year - 1980) - int((year - 1980) / 4)
  )
  autumnal_day = int(
      23.2488 + 0.242194 * (year - 1980) - int((year - 1980) / 4)
  )
  holidays.add(date(year, 3, vernal_day))
  holidays.add(date(year, 9, autumnal_day))

  keiro = nth_monday(year, 9, 3)
  shubun = date(year, 9, autumnal_day)
  if (shubun - keiro).days == 2:
    holidays.add(keiro + timedelta(days=1))

  # 国民の祝日が日曜日の場合の振替休日算出
  base_holidays = sorted(list(holidays))
  for h in base_holidays:
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
  jst = timezone(timedelta(hours=9))
  now = datetime.datetime.now(jst)
  curr_date = now.date()

  jp_holidays = get_dynamic_jp_holidays(
      curr_date.year
  ) | get_dynamic_jp_holidays(curr_date.year + 1)

  def is_tse_open(d):
    return d.weekday() < 5 and d not in jp_holidays

  def fmt_d(d):
    return f"{d.strftime('%m/%d')}({weekdays_jp[d.weekday()]})"

  if is_tse_open(curr_date) and now.time() < dt_time(15, 30):
    start_date = curr_date
  else:
    start_date = curr_date + timedelta(days=1)
    while not is_tse_open(start_date):
      start_date += timedelta(days=1)

  biz_days = []
  d_cursor = start_date
  while len(biz_days) < 15:
    if is_tse_open(d_cursor):
      biz_days.append(d_cursor)
    d_cursor += timedelta(days=1)

  def get_month_end_kenri_date(year, month):
    last_day = calendar.monthrange(year, month)[1]
    d = datetime.datetime(year, month, last_day).date()
    open_days = []
    while len(open_days) < 3:
      if is_tse_open(d):
        open_days.append(d)
      d -= timedelta(days=1)
    return open_days[2]

  y1, m1 = start_date.year, start_date.month
  y2, m2 = (y1 + 1, 1) if m1 == 12 else (y1, m1 + 1)
  kenri_curr = get_month_end_kenri_date(y1, m1)
  kenri_next = get_month_end_kenri_date(y2, m2)

  return (
      f"[HYPER SBI 2 期間指定カレンダー（JST {now.strftime('%m/%d %H:%M')}"
      " 時点・東証祝日自動算出）]\n"
      f"・初回有効営業日(1営業日目): {fmt_d(biz_days[0])}\n"
      f"・第1注文(新規買い)推奨期限目安: 3営業日目={fmt_d(biz_days[2])} |"
      f" 5営業日目={fmt_d(biz_days[4])} | 10営業日目={fmt_d(biz_days[9])}\n"
      f"・第2注文(決済OCO)システム最長上限(15営業日目): {fmt_d(biz_days[14])}\n"
      f"・月末銘柄の権利付き最終日目安: {m1}月末基準={fmt_d(kenri_curr)} /"
      f" {m2}月末基準={fmt_d(kenri_next)}"
      " （※当該銘柄の権利確定月である場合、権利付き最終日をまたぐ期間指定は不可のためその日が上限となる）\n"
  )


@st.cache_data(ttl=86400, show_spinner=False)
def _load_edinet_csv():
  try:
    df_code = pd.read_csv("EdinetcodeDlInfo.csv", encoding="cp932", skiprows=1)
  except UnicodeDecodeError:
    df_code = pd.read_csv("EdinetcodeDlInfo.csv", encoding="utf-8", skiprows=1)
  df_code["証券コード_数値"] = pd.to_numeric(
      df_code["証券コード"], errors="coerce"
  )
  return df_code


MACRO_HARD_CAPS = {
    "^VIX": 30.0,
    "^VIX3M": 28.0,
    "^VVIX": 130.0,
    "^SKEW": 162.0,
    "^MOVE": 135.0,
}


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
      ("USDJPY=X", "USD/JPY (ドル円)", 2, "円"),
      ("^N225", "Nikkei225 (日経平均)", 0, "円"),
  ]

  def _fetch_one(sym):
    try:
      target_tz = (
          "Asia/Tokyo" if sym in ["^N225", "USDJPY=X"] else "America/New_York"
      )
      df = yf.Ticker(sym).history(period="1y", interval="1d", auto_adjust=False)
      if not df.empty:
        if "Repaired?" in df.columns:
          df = df.drop(columns=["Repaired?"])
        df.index = pd.to_datetime(df.index)
        if df.index.tz is not None:
          try:
            df.index = df.index.tz_convert(target_tz).tz_localize(None)
          except Exception:
            df.index = df.index.tz_localize(None)
        df.index = df.index.normalize()
        df = df[~df.index.duplicated(keep="last")].sort_index()
        df = df.dropna(subset=["Close"])
      return sym, df
    except Exception:
      return sym, pd.DataFrame()

  histories = {}
  with ThreadPoolExecutor(max_workers=10) as executor:
    futures = [executor.submit(_fetch_one, s[0]) for s in macro_specs]
    for f in as_completed(futures):
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
    q80 = float(win252.quantile(0.80))
    q90 = float(win252.quantile(0.90))
    q20 = float(win252.quantile(0.20))

    if sym in ["^VIX", "^VVIX", "^SKEW", "^MOVE"]:
      hard_cap = MACRO_HARD_CAPS.get(sym, float("inf"))
      warn_th = min(q80, hard_cap * 0.92)
      alert_th = min(max(q90, warn_th + 0.1), hard_cap)
      th_label = f"基準:{warn_th:.{digits}f}/{alert_th:.{digits}f}"

      z_v = 0.0 if pd.isna(z_score) else z_score

      if curr >= hard_cap:
        status = f"🔴絶対警戒圏(上限{hard_cap:g}突破)"
      elif curr >= alert_th and z_v >= 1.0:
        if sym == "^VIX":
          status = (
              f"🔴パニック圏({th_label})"
              if diff_1d > 0
              else f"🟡高ボラ収縮開始({th_label})"
          )
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
      elif z_score <= -2.0:
        return f"🔻急落スパイク(-2σ超 | 1年{pct252}%分位)", q20, None
      elif z_score >= 1.0:
        return f"↗上昇基調(1年{pct252}%分位)", q80, None
      elif z_score <= -1.0:
        return f"↘下落基調(1年{pct252}%分位)", q20, None
    return f"➡️中立圏(1年{pct252}%分位)", q80, None

  rows = [
      "指標名 | 現在値 | 当日レンジ(安〜高) | 前日比(1D) | 5日比(5D) |"
      " 5日瞬間高値 | 20日比(20D) | 20日Zスコア | 60日位置 |"
      " 状態・警戒判定(252日自動補正)"
  ]

  dynamic_thresholds = {}

  for sym, label, digits, unit in macro_specs:
    df = histories.get(sym, pd.DataFrame())
    if df is None or df.empty:
      rows.append(f"{label} | 取得失敗 | - | - | - | - | - | - | - | -")
      continue

    c_series = df["Close"].dropna()
    if c_series.empty:
      rows.append(f"{label} | 取得失敗 | - | - | - | - | - | - | - | -")
      continue

    if "High" in df.columns:
      h_series = (
          df["High"]
          .reindex(c_series.index)
          .replace(0, np.nan)
          .fillna(c_series)
      )
      h_series = np.maximum(h_series, c_series)
    else:
      h_series = c_series

    if "Low" in df.columns:
      l_series = (
          df["Low"]
          .reindex(c_series.index)
          .replace(0, np.nan)
          .fillna(c_series)
      )
      l_series = np.minimum(l_series, c_series)
    else:
      l_series = c_series

    curr = c_series.iloc[-1]
    day_h = h_series.iloc[-1]
    day_l = l_series.iloc[-1]

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
      sma20 = c_series.tail(20).mean()
      std20 = c_series.tail(20).std()
      z_val = (curr - sma20) / std20 if std20 and std20 > 0 else 0.0
      z_str = f"{'+' if z_val > 0 else ''}{z_val:.2f}σ"
    else:
      z_val = np.nan
      z_str = "N/A"

    tail60_h = h_series.tail(60).max()
    tail60_l = l_series.tail(60).min()
    if pd.notna(tail60_h) and pd.notna(tail60_l) and tail60_h > tail60_l:
      pos_60d = int(round(((curr - tail60_l) / (tail60_h - tail60_l)) * 100))
      pos_str = f"{pos_60d}%"
    else:
      pos_str = "N/A"

    regime_str, warn_th, alert_th = _judge_dynamic_regime(
        sym, digits, curr, z_val, diff_1d_val, c_series
    )
    dynamic_thresholds[sym] = {"warn": warn_th, "alert": alert_th}

    rows.append(
        f"{label} | {_fmt_v(curr, digits, unit)} | "
        f"{_fmt_v(day_l, digits)}〜{_fmt_v(day_h, digits)} | "
        f"{chg_1d_str} | {chg_5d_str} | {_fmt_v(max_5d_h, digits, unit)} | "
        f"{chg_20d_str} | {z_str} | {pos_str} | {regime_str}"
    )

  histories["_thresholds"] = dynamic_thresholds

  vix_df = histories.get("^VIX", pd.DataFrame())
  vix3m_df = histories.get("^VIX3M", pd.DataFrame())
  term_line = ""
  if not vix_df.empty and not vix3m_df.empty:
    c_vix = vix_df["Close"].iloc[-1]
    c_vix3m = vix3m_df["Close"].iloc[-1]
    if c_vix3m > 0:
      ratio = round(c_vix / c_vix3m, 3)
      if ratio >= 1.0:
        term_state = "🔴バックワーデーション(1.00超：機関の直近プット買いパニック・急落警戒)"
      elif ratio >= 0.92:
        term_state = "🟠フラット化進行(0.92超：短期ヘッジ需要増加)"
      else:
        term_state = "🟢コンタンゴ(正常・平穏)"
      term_line = f"・VIX期間構造レシオ (VIX/VIX3M): {ratio} → {term_state}"

  macro_text = (
      f"・CNN Fear & Greed Index: {fg_str}\n"
      f"{term_line}\n\n"
      "[グローバルマクロ＆ボラティリティ時系列マトリクス（瞬間高安値・Zスコア・252日動的基準補正付）]\n"
      + "\n".join(rows)
  )
  return macro_text, histories


@st.cache_data(ttl=1800, show_spinner=False)
def get_macro_data():
  text, _ = get_macro_data_and_history()
  return text


def verify_edinet_api_key() -> tuple:
  """サイドバーからのワンクリック診断用：EDINET APIキーの疎通と有効性を即座にテストする。"""
  raw_key = st.secrets.get("EDINET_API_KEY", "")
  edinet_key = str(raw_key).strip().strip('"').strip("'")
  if not edinet_key:
    return False, "Streamlit Secrets に EDINET_API_KEY が設定されていません。"

  jst = timezone(timedelta(hours=9))
  today_str = datetime.datetime.now(jst).strftime("%Y-%m-%d")
  url = "https://api.edinet-fsa.go.jp/api/v2/documents.json"
  params = {"date": today_str, "type": 1, "Subscription-Key": edinet_key}
  headers = {"Ocp-Apim-Subscription-Key": edinet_key}
  try:
    res = requests.get(url, params=params, headers=headers, timeout=10)
    data = res.json() if res.content else {}
    meta = data.get("metadata", {}) if isinstance(data, dict) else {}
    meta_status = str(meta.get("status", ""))
    if res.status_code == 200 and meta_status == "200":
      return True, f"EDINET API 正常接続確認済 (HTTP 200 / {today_str})"
    msg = (
        data.get("message")
        or meta.get("message")
        or f"HTTP {res.status_code}"
    )
    return False, f"EDINET API 認証失敗: {msg}"
  except Exception as e:
    return False, f"EDINET API 通信エラー: {e}"


@st.cache_data(ttl=3600)
def get_edinet_documents(stock_code_4digit, days=60):
  raw_key = st.secrets.get("EDINET_API_KEY", "")
  edinet_key = str(raw_key).strip().strip('"').strip("'")
  if not edinet_key:
    return "EDINET APIキー未設定", []

  code_str = str(stock_code_4digit).strip().upper()
  edinet_code = ""
  try:
    df_code = _load_edinet_csv()
    if code_str.isdigit():
      target_sec_code = float(code_str) * 10
      match_row = df_code[df_code["証券コード_数値"] == target_sec_code]
    else:
      sec_str_series = (
          df_code["証券コード"].astype(str).str.strip().str.upper()
      )
      match_row = df_code[sec_str_series.str.startswith(code_str)]

    if not match_row.empty:
      edinet_code = str(match_row["ＥＤＩＮＥＴコード"].values[0]).strip()
    else:
      return "EDINETコードリストに該当銘柄がありません", []
  except Exception:
    return "EDINETコード変換用CSVの読み込みエラー", []

  jst = timezone(timedelta(hours=9))
  today_jst = datetime.datetime.now(jst).date()
  jp_holidays = get_dynamic_jp_holidays(
      today_jst.year
  ) | get_dynamic_jp_holidays(today_jst.year - 1)

  date_list = []
  for i in range(days):
    d = today_jst - datetime.timedelta(days=i)
    if d.weekday() < 5 and d not in jp_holidays:
      date_list.append(d.strftime("%Y-%m-%d"))

  if not date_list:
    return f"直近{days}日間のEDINET公式開示書類はありません。", []

  all_found, all_reasons, error_statuses = [], [], []

  def fetch_edinet_for_date(target_date, ed_code, sec_prefix, api_key):
    url = "https://api.edinet-fsa.go.jp/api/v2/documents.json"
    params = {"date": target_date, "type": 2, "Subscription-Key": api_key}
    headers = {"Ocp-Apim-Subscription-Key": api_key}
    found_docs, reasons = [], []
    err_msg = None

    for attempt in range(3):
      try:
        res = requests.get(url, params=params, headers=headers, timeout=12)
        data = None
        try:
          data = res.json()
        except Exception:
          data = None

        if isinstance(data, dict):
          meta = data.get("metadata")
          meta_status = (
              str(meta.get("status", "")).strip()
              if isinstance(meta, dict)
              else ""
          )
          top_code = data.get("StatusCode") or data.get("statusCode")

          if (
              res.status_code == 200
              and meta_status == "200"
              and "results" in data
              and isinstance(data["results"], list)
          ):
            err_msg = None
            for doc in data["results"]:
              if not isinstance(doc, dict):
                continue
              doc_ed = str(doc.get("edinetCode") or "").strip()
              doc_sec = str(doc.get("secCode") or "").strip().upper()
              if (ed_code and doc_ed == ed_code) or (
                  sec_prefix and doc_sec.startswith(sec_prefix)
              ):
                title = doc.get("title") or "タイトル不明"
                desc = doc.get("docDescription")
                reason = doc.get("currentReportReason")
                content_details = []
                if desc:
                  content_details.append(f"概要: {desc}")
                if reason:
                  content_details.append(f"事由: {reason}")
                  if (
                      "臨時報告書" in title
                      or "修正" in title
                      or "配当" in title
                  ):
                    reasons.append(reason)
                content_str = (
                    " | ".join(content_details)
                    if content_details
                    else "詳細記載なし"
                )
                found_docs.append(f"[{target_date}] {title} ({content_str})")
            break
          else:
            code_label = top_code or meta_status or res.status_code
            msg_label = (
                data.get("message")
                or (meta.get("message") if isinstance(meta, dict) else "")
                or "InvalidResponse"
            )
            err_msg = f"Status:{code_label}({str(msg_label)[:80]})"
            if str(code_label) in ["401", "403"]:
              break
            if attempt < 2:
              time.sleep(0.5 * (attempt + 1))
        else:
          err_msg = f"HTTP:{res.status_code}"
          if res.status_code in [401, 403]:
            break
          if attempt < 2:
            time.sleep(0.5 * (attempt + 1))
      except Exception:
        err_msg = "Timeout/ConnError"
        if attempt < 2:
          time.sleep(0.5 * (attempt + 1))
    return found_docs, reasons, err_msg

  # 1日分プリフライト検証（401/403認証拒否時は残り営業日の並列リクエストを即座に中断）
  first_docs, first_reasons, first_err = fetch_edinet_for_date(
      date_list[0], edinet_code, code_str, edinet_key
  )
  if first_err and any(
      auth_code in first_err
      for auth_code in ["Status:401", "Status:403", "HTTP:401", "HTTP:403"]
  ):
    return (
        f"EDINET API認証エラーのため取得を中断しました（{first_err} — "
        "Streamlit"
        " SecretsのEDINET_API_KEY設定またはキー有効期限を確認してください）。",
        [],
    )

  if first_docs:
    all_found.extend(first_docs)
  if first_reasons:
    all_reasons.extend(first_reasons)
  if first_err:
    error_statuses.append(first_err)

  remaining_dates = date_list[1:]
  if remaining_dates:
    with ThreadPoolExecutor(max_workers=4) as executor:
      futures = {
          executor.submit(
              fetch_edinet_for_date, d, edinet_code, code_str, edinet_key
          ): d
          for d in remaining_dates
      }
      for future in as_completed(futures):
        docs, reasons, err = future.result()
        if docs:
          all_found.extend(docs)
        if reasons:
          all_reasons.extend(reasons)
        if err:
          error_statuses.append(err)

  all_found.sort(reverse=True)
  if all_found:
    res_str = "\n".join(all_found)
    if error_statuses:
      unique_errs = ", ".join(sorted(set(error_statuses)))
      res_str += (
          f"\n（※一部日程でEDINET API応答エラー {len(error_statuses)}件:"
          f" {unique_errs}）"
      )
  else:
    if error_statuses:
      unique_errs = ", ".join(sorted(set(error_statuses)))
      res_str = (
          f"直近{days}日間のEDINET開示書類は検出されませんでした（※APIエラー"
          f" {len(error_statuses)}/{len(date_list)}営業日発生: {unique_errs}）。"
      )
    else:
      res_str = f"直近{days}日間のEDINET公式開示書類はありません。"
  return res_str, list(set(all_reasons))


@st.cache_data(ttl=3600)
def get_news(company_name, is_jp, edinet_reasons=None):
  news_list = []
  if is_jp:
    q1 = urllib.parse.quote(
        f"{company_name} (TOPIX OR 再編 OR アナリスト OR レーティング OR"
        " 目標株価 OR 株探 OR 四季報 OR 日経 OR 決算 OR 増配)"
    )
    url1 = f"https://news.google.com/rss/search?q={q1}&hl=ja&gl=JP&ceid=JP:ja"
  else:
    q1 = urllib.parse.quote(
        f"{company_name} stock (earnings OR upgrade OR target OR guidance)"
    )
    url1 = (
        f"https://news.google.com/rss/search?q={q1}&hl=en-US&gl=US&ceid=US:en"
    )

  try:
    feed = feedparser.parse(url1)
    for entry in feed.entries[:8]:
      title = entry.title.split(" - ")[0]
      pub = entry.get("published", "")

      desc = html.unescape(re.sub(r"<[^>]+>", "", entry.get("description", "")))
      desc_clean = re.sub(r"\s+", " ", desc).strip()
      if len(desc_clean) > 120:
        desc_clean = desc_clean[:120] + "..."

      news_item = f"■ {title} ({pub[:16]})\n   概要: {desc_clean}"

      if is_jp:
        if company_name in title or any(
            kw in title
            for kw in [
                "TOPIX",
                "再編",
                "決算",
                "配当",
                "株",
                "業績",
                "アナリスト",
                "レーティング",
                "目標",
            ]
        ):
          news_list.append(news_item)
      else:
        news_list.append(news_item)
  except Exception:
    pass

  if is_jp and edinet_reasons:
    for reason in edinet_reasons:
      match = re.search(r"（(.*?)）", reason)
      keyword = match.group(1) if match else reason[:15]
      keyword = keyword.replace("の件", "").replace("に関する", "")
      q2 = urllib.parse.quote(f"{company_name} {keyword}")
      url2 = f"https://news.google.com/rss/search?q={q2}&hl=ja&gl=JP&ceid=JP:ja"
      try:
        feed2 = feedparser.parse(url2)
        for entry in feed2.entries[:3]:
          title = entry.title.split(" - ")[0]
          pub = entry.get("published", "")
          desc = html.unescape(
              re.sub(r"<[^>]+>", "", entry.get("description", ""))
          )
          desc_clean = re.sub(r"\s+", " ", desc).strip()
          if len(desc_clean) > 120:
            desc_clean = desc_clean[:120] + "..."

          news_list.append(
              f"■ [EDINET連動深掘り] {title} ({pub[:16]})\n   概要:"
              f" {desc_clean}"
          )
      except Exception:
        pass

  unique_news = []
  seen_titles = set()
  for item in news_list:
    t = item.split("\n")[0]
    if t not in seen_titles:
      seen_titles.add(t)
      unique_news.append(item)

  unique_news = unique_news[:9]
  return (
      "\n\n".join(unique_news)
      if unique_news
      else "直近の重要な関連ニュースは見つかりませんでした。"
  )


@st.cache_data(ttl=3600)
def get_japanese_name(stock_code):
  try:
    df_code = _load_edinet_csv()
    code_str = str(stock_code).strip().upper()
    if code_str.isdigit():
      target_sec_code = float(code_str) * 10
      match_row = df_code[df_code["証券コード_数値"] == target_sec_code]
    else:
      sec_str_series = (
          df_code["証券コード"].astype(str).str.strip().str.upper()
      )
      match_row = df_code[sec_str_series.str.startswith(code_str)]

    if not match_row.empty:
      raw_name = match_row["提出者名"].values[0]
      for rm in ["株式会社", "ホールディングス", "グループ本社", "グループ"]:
        raw_name = str(raw_name).replace(rm, "")
      return raw_name.strip()
  except Exception:
    pass

  try:
    html_text = _http_get_html(
        f"https://kabutan.jp/stock/?code={stock_code}", timeout=5
    )
    m = re.search(r"<title>(.*?)【", html_text)
    if m:
      return m.group(1).strip()
  except Exception:
    pass
  return None


@st.cache_data(ttl=3600)
def get_us_stock_name(stock_code):
  code_up = stock_code.strip().upper()
  try:
    url = f"https://query2.finance.yahoo.com/v1/finance/search?q={code_up}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    res = requests.get(url, headers=headers, timeout=5)
    if res.status_code == 200:
      quotes = res.json().get("quotes", [])
      for q in quotes:
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
  """直近の決算発表日（YYYY/M/D）から約3ヶ月後の次回四半期決算発表目安時期（上旬・中旬・下旬）を推計する。"""
  try:
    parts = [int(x) for x in announce_date_str.strip().split("/")]
    if len(parts) == 3:
      y, m, d = parts
      total_m = m + 3
      next_y = y + (total_m - 1) // 12
      next_m = (total_m - 1) % 12 + 1
      if d <= 10:
        jun = "上旬"
      elif d <= 20:
        jun = "中旬"
      else:
        jun = "下旬"
      return f" / 次回目安:{next_y}年{next_m}月{jun}頃[+3ヶ月推計]"
  except Exception:
    pass
  return ""


def get_japanese_fundamentals(stock_code):
  """日本株ファンダメンタルズを5段階フォールバック（株探 → Yahoo!日本版 → 日経電子版 → みんかぶ → EDINET CSV）で取得する。

  ※一時的な通信遮断で全項目が N/A
  になった場合はキャッシュせず、有効値が取れた場合のみ session_state に保持する。
  """
  cache_key = f"fund_cache_{stock_code}"
  if cache_key in st.session_state:
    cached_val, cached_ts = st.session_state[cache_key]
    if time.time() - cached_ts < 3600:
      return cached_val

  result = {
      "per": "N/A",
      "pbr": "N/A",
      "div": "N/A",
      "margin": "N/A",
      "market_cap": "N/A",
      "earnings_date": "要検索確認",
  }

  def _clean_num(raw_str):
    if not raw_str:
      return "N/A"
    s = re.sub(r"\s+", "", str(raw_str)).replace(",", "")
    if any(x in s for x in ["－", "―", "---", "赤字"]):
      return "－"
    m = re.search(r"[0-9]+(?:\.[0-9]+)?", s)
    return m.group(0) if m else "N/A"

  # 1. 株探 (kabutan.jp) からの取得試行
  try:
    html_text = _http_get_html(
        f"https://kabutan.jp/stock/?code={stock_code}", timeout=8
    )
    if html_text:
      soup = BeautifulSoup(html_text, "html.parser")
      page_txt = soup.get_text(" ", strip=True)
      info_div = soup.find("div", id="stockinfo_i3")
      if info_div:
        tds = [td.get_text(strip=True) for td in info_div.find_all("td")]
        if len(tds) >= 4:
          per_v = _clean_num(tds[0])
          pbr_v = _clean_num(tds[1])
          div_v = _clean_num(tds[2])
          mrg_v = _clean_num(tds[3])

          result["per"] = "測定不能(赤字・未定等)" if per_v == "－" else per_v
          result["pbr"] = pbr_v if pbr_v != "－" else "N/A"
          result["div"] = div_v if div_v != "－" else "N/A"
          result["margin"] = mrg_v if mrg_v != "－" else "N/A"

        if len(tds) >= 5 and "億円" in tds[4]:
          result["market_cap"] = tds[4].replace("億円", "").strip()

      if result["market_cap"] == "N/A":
        m_mc_kb = re.search(
            r"時価総額\s*([0-9,]+(?:\.[0-9]+)?)\s*億円", page_txt
        )
        if m_mc_kb:
          result["market_cap"] = m_mc_kb.group(1)

      earn_m = re.search(
          r"決算発表予定日[^0-9]*?([0-9]{4}年[0-9]{1,2}月[0-9]{1,2}日|[0-9]{4}/[0-9]{1,2}/[0-9]{1,2}|[0-9]{1,2}月[0-9]{1,2}日)",
          page_txt,
      )
      if earn_m:
        result["earnings_date"] = f"{earn_m.group(1)}頃(予定)"
      else:
        fy_m = re.search(
            r"(?:[連単]\s*)?([0-9]{2,4})\.([0-9]{2})\s*(?:予|実|連|単)",
            page_txt,
        )
        if not fy_m:
          fy_m = re.search(r"([0-9]{2})\.([0-9]{2})\s*[0-9,－―]+", page_txt)
        if fy_m:
          month_num = int(fy_m.group(2))
          if 1 <= month_num <= 12:
            result["earnings_date"] = (
                f"本決算 {month_num}月期 (次回発表日は要検索確認)"
            )
  except Exception:
    pass

  # 2. Yahoo!ファイナンス日本版からのフォールバック補完
  if (
      result["per"] == "N/A"
      or result["pbr"] == "N/A"
      or result["margin"] == "N/A"
      or result["earnings_date"] == "要検索確認"
  ):
    try:
      yh_html = _http_get_html(
          f"https://finance.yahoo.co.jp/quote/{stock_code}.T", timeout=8
      )
      if yh_html:
        yh_soup = BeautifulSoup(yh_html, "html.parser")
        full_txt = yh_soup.get_text(" ", strip=True)

        if result["margin"] == "N/A":
          m_mrg = re.search(
              r"信用倍率.{0,20}?([0-9,]+(?:\.[0-9]+)?|－|―)\s*倍", full_txt
          )
          if m_mrg and m_mrg.group(1) not in ["－", "―"]:
            result["margin"] = m_mrg.group(1).replace(",", "")

        if result["per"] == "N/A":
          m_per = re.search(
              r"PER\s*（会社予想）.{0,25}?([0-9,]+(?:\.[0-9]+)?|－|―)\s*倍",
              full_txt,
          )
          if m_per:
            val_p = m_per.group(1).replace(",", "")
            result["per"] = (
                "測定不能(赤字・未定等)" if val_p in ["－", "―"] else val_p
            )

        if result["pbr"] == "N/A":
          m_pbr = re.search(
              r"PBR\s*（実績）.{0,25}?([0-9,]+(?:\.[0-9]+)?|－|―)\s*倍",
              full_txt,
          )
          if m_pbr and m_pbr.group(1) not in ["－", "―"]:
            result["pbr"] = m_pbr.group(1).replace(",", "")

        if result["div"] == "N/A":
          m_div = re.search(
              r"配当利回り\s*（会社予想）.{0,25}?([0-9,]+(?:\.[0-9]+)?|－|―)\s*%",
              full_txt,
          )
          if m_div and m_div.group(1) not in ["－", "―"]:
            result["div"] = m_div.group(1).replace(",", "")

        if result["market_cap"] == "N/A":
          m_mc = re.search(r"時価総額.{0,25}?([0-9,]+)\s*百万円", full_txt)
          if m_mc:
            val_million = float(m_mc.group(1).replace(",", ""))
            result["market_cap"] = f"{round(val_million / 100, 1):,}"

        if result["earnings_date"] == "要検索確認":
          m_yh_sum = re.search(
              r"([0-9]{4}/[0-9]{1,2}/[0-9]{1,2})\s*発表分.{0,40}?([0-9]{4}年[0-9]{1,2}月期\s*(?:第[1-4１-４]四半期|中間期|通期)?)",
              full_txt,
          )
          if m_yh_sum:
            ann_dt = m_yh_sum.group(1).strip()
            fy_term = m_yh_sum.group(2).strip()
            next_est = _estimate_next_earnings_period(ann_dt)
            result["earnings_date"] = (
                f"{fy_term} (直近発表:{ann_dt}{next_est})"
            )
          else:
            m_yh_fy = re.search(
                r"(?:1株配当|EPS)（会社予想）.{0,30}?(20[0-9]{2})/([0-9]{1,2})",
                full_txt,
            )
            if m_yh_fy:
              result["earnings_date"] = (
                  f"本決算 {int(m_yh_fy.group(2))}月期"
                  f" ({m_yh_fy.group(1)}/{m_yh_fy.group(2)}期)"
              )
    except Exception:
      pass

  # 3. 日経電子版 (nikkei.com) からのフォールバック補完（海外クラウドIP耐性）
  if (
      result["per"] == "N/A"
      or result["pbr"] == "N/A"
      or result["div"] == "N/A"
      or result["market_cap"] == "N/A"
  ):
    try:
      nk_html = _http_get_html(
          f"https://www.nikkei.com/nkd/company/?scode={stock_code}", timeout=8
      )
      if nk_html:
        nk_soup = BeautifulSoup(nk_html, "html.parser")
        nk_txt = nk_soup.get_text(" ", strip=True)
        if result["per"] == "N/A":
          m = re.search(r"予想PER.{0,25}?([0-9,]+(?:\.[0-9]+)?)\s*倍", nk_txt)
          if m:
            result["per"] = m.group(1).replace(",", "")
        if result["pbr"] == "N/A":
          m = re.search(
              r"PBR(?:（実績）)?.{0,25}?([0-9,]+(?:\.[0-9]+)?)\s*倍", nk_txt
          )
          if m:
            result["pbr"] = m.group(1).replace(",", "")
        if result["div"] == "N/A":
          m = re.search(
              r"予想配当利回り.{0,25}?([0-9,]+(?:\.[0-9]+)?)\s*[％%]", nk_txt
          )
          if m:
            result["div"] = m.group(1).replace(",", "")
        if result["market_cap"] == "N/A":
          m = re.search(
              r"時価総額.{0,25}?([0-9,]+(?:\.[0-9]+)?)\s*億円", nk_txt
          )
          if m:
            result["market_cap"] = m.group(1)
    except Exception:
      pass

  # 4. みんかぶ (minkabu.jp) からのフォールバック補完
  if (
      result["per"] == "N/A"
      or result["pbr"] == "N/A"
      or result["div"] == "N/A"
      or result["market_cap"] == "N/A"
  ):
    try:
      mk_html = _http_get_html(
          f"https://minkabu.jp/stock/{stock_code}", timeout=8
      )
      if mk_html:
        mk_soup = BeautifulSoup(mk_html, "html.parser")
        mk_txt = mk_soup.get_text(" ", strip=True)
        if result["per"] == "N/A":
          m = re.search(
              r"PER\s*(?:\(調整後\))?.{0,20}?([0-9,]+(?:\.[0-9]+)?)\s*倍",
              mk_txt,
          )
          if m:
            result["per"] = m.group(1).replace(",", "")
        if result["pbr"] == "N/A":
          m = re.search(r"PBR.{0,20}?([0-9,]+(?:\.[0-9]+)?)\s*倍", mk_txt)
          if m:
            result["pbr"] = m.group(1).replace(",", "")
        if result["div"] == "N/A":
          m = re.search(
              r"配当利回り.{0,20}?([0-9,]+(?:\.[0-9]+)?)\s*[％%]", mk_txt
          )
          if m:
            result["div"] = m.group(1).replace(",", "")
        if result["market_cap"] == "N/A":
          m = re.search(r"時価総額.{0,20}?([0-9,]+)\s*百万円", mk_txt)
          if m:
            val_m = float(m.group(1).replace(",", ""))
            result["market_cap"] = f"{round(val_m / 100, 1):,}"
    except Exception:
      pass

  # 5. ローカルの EdinetcodeDlInfo.csv（「決算日」カラム）からの最終補完
  if result["earnings_date"] == "要検索確認":
    try:
      df_code = _load_edinet_csv()
      code_str = str(stock_code).strip().upper()
      if code_str.isdigit():
        match_row = df_code[df_code["証券コード_数値"] == float(code_str) * 10]
      else:
        sec_s = df_code["証券コード"].astype(str).str.strip().str.upper()
        match_row = df_code[sec_s.str.startswith(code_str)]
      if not match_row.empty and "決算日" in match_row.columns:
        kessan_bi = str(match_row["決算日"].values[0]).strip()
        m_kb = re.search(r"([0-9]{1,2})月", kessan_bi)
        if m_kb:
          result["earnings_date"] = (
              f"本決算 {int(m_kb.group(1))}月期 (EDINET基準:{kessan_bi})"
          )
        elif kessan_bi and kessan_bi.lower() != "nan":
          result["earnings_date"] = (
              f"決算日:{kessan_bi} (次回発表日は要検索確認)"
          )
    except Exception:
      pass

  # PER, PBR, 時価総額のいずれか1つ以上が正常取得できた場合のみキャッシュに保存する
  if any(result[k] != "N/A" for k in ["per", "pbr", "market_cap", "div"]):
    st.session_state[cache_key] = (result, time.time())

  return result


@st.cache_data(ttl=3600)
def search_japanese_code_by_name(query):
  if not query:
    return []
  try:
    df_code = _load_edinet_csv().copy()
    df_code = df_code.dropna(subset=["証券コード"])

    def _fmt_sec_code(val):
      s = str(val).strip()
      if s.endswith(".0"):
        s = s[:-2]
      if len(s) == 5 and s.endswith("0"):
        return s[:4]
      return s

    df_code["証券コード_4桁"] = df_code["証券コード"].apply(_fmt_sec_code)
    df_code["提出者名"] = df_code["提出者名"].fillna("").astype(str)
    df_code["提出者名（ヨミ）"] = (
        df_code["提出者名（ヨミ）"].fillna("").astype(str)
    )

    mask = df_code["提出者名"].str.contains(
        query, case=False, na=False
    ) | df_code["提出者名（ヨミ）"].str.contains(query, case=False, na=False)
    matches = df_code[mask].copy()

    if matches.empty:
      names = df_code["提出者名"].tolist()
      yomis = df_code["提出者名（ヨミ）"].tolist()
      close_names = difflib.get_close_matches(query, names, n=10, cutoff=0.4)
      close_yomis = difflib.get_close_matches(query, yomis, n=10, cutoff=0.4)
      mask_fuzzy = df_code["提出者名"].isin(close_names) | df_code[
          "提出者名（ヨミ）"
      ].isin(close_yomis)
      matches = df_code[mask_fuzzy].copy()

    matches["sort_key"] = pd.to_numeric(
        matches["証券コード_4桁"], errors="coerce"
    ).fillna(99999)
    matches = matches.sort_values("sort_key")
    return [
        f"{row['証券コード_4桁']} - {row['提出者名']}"
        for _, row in matches.iterrows()
    ]
  except Exception:
    return []


@st.cache_data(ttl=3600)
def search_us_ticker_by_name(query):
  if not query:
    return []
  try:
    url = f"https://query2.finance.yahoo.com/v1/finance/search?q={urllib.parse.quote(query)}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    res = requests.get(url, headers=headers, timeout=5)
    if res.status_code == 200:
      data = res.json()
      quotes = data.get("quotes", [])
      matches = []
      for q in quotes:
        if q.get("quoteType") in ["EQUITY", "ETF"]:
          symbol = q.get("symbol", "")
          name = q.get("shortname") or q.get("longname") or ""
          if symbol:
            matches.append(f"{symbol} - {name}")
      if matches:
        return matches[:10]
  except Exception:
    pass

  try:
    s = yf.Search(query, max_results=10)
    matches = []
    for q in getattr(s, "quotes", []):
      if q.get("quoteType") in ["EQUITY", "ETF"]:
        symbol = q.get("symbol", "")
        name = q.get("shortname") or q.get("longname") or ""
        if symbol:
          matches.append(f"{symbol} - {name}")
    return matches[:10]
  except Exception:
    return []


# --- 以下、GASスプレッドシート連携用レガシー互換関数 ---
def save_analysis_to_sheet(
    date_str, stock_code, name, judgement, target_price, time_limit, report_text
):
  webapp_url = st.secrets.get("GAS_WEBAPP_URL", "")
  if not webapp_url:
    st.error("GAS_WEBAPP_URLが設定されていません。")
    return False

  payload = {
      "date": date_str,
      "stock_code": stock_code,
      "name": name,
      "judgement": judgement,
      "target_price": target_price,
      "time_limit": time_limit,
      "report_text": report_text,
  }

  try:
    response = requests.post(webapp_url, json=payload, timeout=10)
    if (
        response.status_code == 200
        and response.json().get("status") == "success"
    ):
      return True
    else:
      st.error("スプレッドシートへの保存に失敗しました。")
      return False
  except Exception as e:
    st.error(f"通信エラー: {e}")
    return False


def load_history_from_sheet():
  webapp_url = st.secrets.get("GAS_WEBAPP_URL", "")
  if not webapp_url:
    return pd.DataFrame()

  try:
    response = requests.get(webapp_url, timeout=10)
    if response.status_code == 200:
      data = response.json()
      if isinstance(data, list) and len(data) > 0:
        return pd.DataFrame(data)
    return pd.DataFrame()
  except Exception:
    return pd.DataFrame()