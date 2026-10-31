import datetime
from datetime import timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import re
import urllib.parse
import xml.etree.ElementTree as ET
import numpy as np
import pandas as pd
import yfinance as yf
from bs4 import BeautifulSoup

try:
    from curl_cffi import requests as cffi_requests
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False
import requests as std_requests


# ==========================================
# 1. マクロ10指標定義 ＆ 絶対ハードキャップ（安全弁）
# ==========================================
# chart_maker.py の generate_macro_chart_image が参照するキー名と完全一致
MACRO_SYMBOLS = [
    ("^VIX", "VIX (恐怖指数)", "America/New_York", "vol"),
    ("^VIX3M", "VIX3M (3ヶ月VIX)", "America/New_York", "vol"),
    ("^VVIX", "VVIX (VIXのVol)", "America/New_York", "vol"),
    ("^SKEW", "SKEW (黒鳥指数)", "America/New_York", "vol"),
    ("^MOVE", "MOVE (米債Vol)", "America/New_York", "vol"),
    ("HYG", "HYG (ハイイールド債)", "America/New_York", "credit"),
    ("^SOX", "SOX (半導体指数)", "America/New_York", "equity"),
    ("^TNX", "US10Y (米10年債利回り)", "America/New_York", "rate"),
    ("USDJPY=X", "USD/JPY (ドル円)", "Asia/Tokyo", "fx"),
    ("^N225", "Nikkei225 (日経平均)", "Asia/Tokyo", "equity"),
]

# 長期高止まりによる麻痺（ゆでガエル化）を防ぐ絶対ハードキャップ
HARD_CAPS = {
    "^VIX": 30.0,
    "^VIX3M": 28.0,
    "^VVIX": 125.0,
    "^SKEW": 160.0,   # 140〜150近辺の常態化は252日分位で自動吸収し、160超は強制警戒
    "^MOVE": 135.0,
    "^TNX": 5.0,
}


# ==========================================
# 2. HTTP通信ヘルパー（curl_cffi Chrome偽装 ＋ 標準requestsフォールバック）
# ==========================================
def _fetch_url_text(url: str, timeout: int = 8, is_json: bool = False):
    headers = {
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        "Referer": "https://www.google.co.jp/",
    }
    try:
        if HAS_CURL_CFFI:
            resp = cffi_requests.get(url, impersonate="chrome", headers=headers, timeout=timeout)
        else:
            headers["User-Agent"] = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            )
            resp = std_requests.get(url, headers=headers, timeout=timeout)

        if resp.status_code == 200:
            if is_json:
                return resp.json()
            resp.encoding = resp.apparent_encoding or "utf-8"
            return resp.text
    except Exception:
        pass
    return {} if is_json else ""


# ==========================================
# 3. タイムゾーン正規化・日本の祝日＆SBI期間指定カレンダー算出
# ==========================================
def _normalize_macro_df(df: pd.DataFrame, target_tz: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
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
    return df


def _calc_vernal_equinox(year: int) -> int:
    return int(20.8431 + 0.242194 * (year - 1980) - int((year - 1980) / 4))


def _calc_autumnal_equinox(year: int) -> int:
    return int(23.2488 + 0.242194 * (year - 1980) - int((year - 1980) / 4))


def _nth_monday(year: int, month: int, n: int) -> datetime.date:
    first = datetime.date(year, month, 1)
    offset = (0 - first.weekday()) % 7
    return first + datetime.timedelta(days=offset + 7 * (n - 1))


def get_jp_holidays(year: int) -> set:
    holidays = set()
    fixed = [
        (1, 1), (2, 11), (2, 23), (4, 29),
        (5, 3), (5, 4), (5, 5), (8, 11),
        (11, 3), (11, 23),
    ]
    for m, d in fixed:
        holidays.add(datetime.date(year, m, d))

    holidays.add(_nth_monday(year, 1, 2))
    holidays.add(_nth_monday(year, 7, 3))
    holidays.add(_nth_monday(year, 9, 3))
    holidays.add(_nth_monday(year, 10, 2))

    holidays.add(datetime.date(year, 3, _calc_vernal_equinox(year)))
    holidays.add(datetime.date(year, 9, _calc_autumnal_equinox(year)))

    sorted_h = sorted(list(holidays))
    for i in range(len(sorted_h) - 1):
        if (sorted_h[i + 1] - sorted_h[i]).days == 2:
            mid = sorted_h[i] + datetime.timedelta(days=1)
            if mid.weekday() < 5:
                holidays.add(mid)

    base_holidays = sorted(list(holidays))
    for h in base_holidays:
        if h.weekday() == 6:
            sub = h + datetime.timedelta(days=1)
            while sub in holidays:
                sub += datetime.timedelta(days=1)
            holidays.add(sub)

    for d in [1, 2, 3]:
        holidays.add(datetime.date(year, 1, d))
    holidays.add(datetime.date(year, 12, 31))
    return holidays


def get_sbi_order_limit_dates(now_jst: datetime.datetime = None, max_days: int = 15) -> str:
    """
    app.py から呼ばれる HYPER SBI 2 期間指定カレンダー生成関数（最長15営業日）。
    """
    if now_jst is None:
        now_jst = datetime.datetime.now(timezone(timedelta(hours=9)))

    holidays = get_jp_holidays(now_jst.year) | get_jp_holidays(now_jst.year + 1)
    curr_date = now_jst.date()

    is_after_close = (now_jst.hour > 15) or (now_jst.hour == 15 and now_jst.minute >= 30)
    if is_after_close or curr_date.weekday() >= 5 or curr_date in holidays:
        curr_date += datetime.timedelta(days=1)
        while curr_date.weekday() >= 5 or curr_date in holidays:
            curr_date += datetime.timedelta(days=1)

    weekdays_jp = ["月", "火", "水", "木", "金", "土", "日"]
    b_days = []
    d = curr_date
    while len(b_days) < max_days:
        if d.weekday() < 5 and d not in holidays:
            b_days.append(d)
        d += datetime.timedelta(days=1)

    lines = ["[HYPER SBI 2 期間指定カレンダー（発注日を含め最長15営業日・15:30以降は翌営業日起算）]"]
    lines.append(f"起算日(1営業日目): {b_days[0].strftime('%Y-%m-%d')}({weekdays_jp[b_days[0].weekday()]})")
    row_items = []
    for idx, bd in enumerate(b_days, start=1):
        row_items.append(f"{idx}営業日目: {bd.strftime('%m/%d')}({weekdays_jp[bd.weekday()]})")
        if idx % 5 == 0 or idx == max_days:
            lines.append(" / ".join(row_items))
            row_items = []
    return "\n".join(lines) + "\n"


# ==========================================
# 4. マクロ10指標の並列取得 ＆ 252日動的レジーム自動補正
# ==========================================
def _fetch_one_macro(sym: str, tz_name: str) -> tuple:
    try:
        tk = yf.Ticker(sym)
        # 252営業日（1年）分を取得して中期レジーム分位を算出可能にする
        df = tk.history(period="1y", interval="1d", auto_adjust=False)
        df = _normalize_macro_df(df, tz_name)
        if not df.empty and "Close" in df.columns:
            return sym, df
    except Exception:
        pass
    return sym, pd.DataFrame()


def _evaluate_macro_series(sym: str, label: str, cat: str, df: pd.DataFrame) -> dict:
    """
    過去1年（約252営業日）のDataFrameから
    当日レンジ・1D/5D/20D比・5日瞬間高値・20日Zスコア・60日位置・252日分位（動的基準値）・警戒判定を算出。
    """
    if df is None or df.empty or "Close" not in df.columns:
        return {"valid": False, "sym": sym, "label": label}

    s_close = df["Close"].dropna()
    if len(s_close) < 20:
        return {"valid": False, "sym": sym, "label": label}

    curr = float(s_close.iloc[-1])
    prev_1d = float(s_close.iloc[-2]) if len(s_close) >= 2 else curr
    prev_5d = float(s_close.iloc[-6]) if len(s_close) >= 6 else float(s_close.iloc[0])
    prev_20d = float(s_close.iloc[-21]) if len(s_close) >= 21 else float(s_close.iloc[0])

    chg_1d = ((curr - prev_1d) / prev_1d * 100.0) if abs(prev_1d) > 1e-8 else 0.0
    chg_5d = ((curr - prev_5d) / prev_5d * 100.0) if abs(prev_5d) > 1e-8 else 0.0
    chg_20d = ((curr - prev_20d) / prev_20d * 100.0) if abs(prev_20d) > 1e-8 else 0.0

    # High / Low の欠損・0異常値をCloseで安全補完
    s_high = df["High"].reindex(s_close.index).replace(0, np.nan).fillna(s_close) if "High" in df.columns else s_close
    s_high = np.maximum(s_high, s_close)
    s_low = df["Low"].reindex(s_close.index).replace(0, np.nan).fillna(s_close) if "Low" in df.columns else s_close
    s_low = np.minimum(s_low, s_close)

    today_low = float(s_low.iloc[-1])
    today_high = float(s_high.iloc[-1])
    high_5d = float(s_high.iloc[-5:].max())

    # 1. 直近20営業日Zスコア（短期スパイク検知）
    win20 = s_close.iloc[-20:]
    std20 = float(win20.std(ddof=0))
    z20 = (curr - float(win20.mean())) / std20 if std20 > 1e-8 else 0.0

    # 2. 直近60営業日レンジ位置（0〜100%）
    win60 = s_close.iloc[-60:]
    min60, max60 = float(win60.min()), float(win60.max())
    pct60 = ((curr - min60) / (max60 - min60) * 100.0) if (max60 - min60) > 1e-8 else 50.0

    # 3. 過去252営業日（1年）パーセンタイル分位（中期レジーム自動補正）
    win252 = s_close.iloc[-252:]
    pct252 = float((win252 <= curr).mean() * 100.0)
    q80_252 = float(win252.quantile(0.80))
    q90_252 = float(win252.quantile(0.90))
    q20_252 = float(win252.quantile(0.20))

    alert = "🟢平常"
    dyn_ref = q80_252

    if cat in ["vol", "rate"]:
        hard_cap = HARD_CAPS.get(sym, float("inf"))
        dyn_ref = min(q80_252, hard_cap)
        if curr >= hard_cap:
            alert = f"🔴警戒(上限{hard_cap:g}突破)"
        elif (pct252 >= 90.0 and z20 >= 2.0) or z20 >= 2.5:
            alert = "🔴警戒(急騰スパイク)"
        elif pct252 >= 80.0 or z20 >= 1.5:
            alert = "🟡注意(1年上位20%/上昇)"
    elif cat in ["credit", "equity"]:
        dyn_ref = q20_252
        if (pct252 <= 10.0 and z20 <= -2.0) or z20 <= -2.5:
            alert = "🔴警戒(急落/崩れ)"
        elif pct60 <= 15.0 or z20 <= -1.5:
            alert = "🟡注意(短期軟調)"
    elif cat == "fx":
        dyn_ref = float(win20.mean())
        if abs(z20) >= 2.5:
            alert = "🔴警戒(為替急変)"
        elif abs(z20) >= 1.8:
            alert = "🟡注意(変動拡大)"

    return {
        "valid": True,
        "sym": sym,
        "label": label,
        "curr": curr,
        "today_low": today_low,
        "today_high": today_high,
        "chg_1d": chg_1d,
        "chg_5d": chg_5d,
        "chg_20d": chg_20d,
        "high_5d": high_5d,
        "z20": z20,
        "pct60": pct60,
        "pct252": pct252,
        "q80_252": q80_252,
        "q90_252": q90_252,
        "q20_252": q20_252,
        "dyn_ref": dyn_ref,
        "hard_cap": HARD_CAPS.get(sym),
        "alert": alert,
        "date_str": s_close.index[-1].strftime("%m/%d"),
    }


def get_macro_data_and_history() -> tuple:
    """
    app.py から呼ばれるマクロデータ取得関数。
    戻り値: (macro_text: str, macro_histories: dict)
    ※macro_histories には過去1年（252営業日）のDataFrameと動的閾値メタデータ('_metrics')を格納。
    """
    raw_histories = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(_fetch_one_macro, sym, tz): (sym, label, cat)
            for sym, label, tz, cat in MACRO_SYMBOLS
        }
        for fut in as_completed(futures):
            sym, df = fut.result()
            raw_histories[sym] = df

    # 統計評価とX軸同期補完
    metrics_map = {}
    for sym, label, tz, cat in MACRO_SYMBOLS:
        df = raw_histories.get(sym, pd.DataFrame())
        metrics_map[sym] = _evaluate_macro_series(sym, label, cat, df)

    # SKEWやMOVEの1営業日遅延および日米祝日差を吸収するため、共通営業日軸で最大3日ffillした履歴を構築
    all_indexes = [
        df.index for df in raw_histories.values()
        if df is not None and not df.empty
    ]
    synced_histories = {}
    if all_indexes:
        union_idx = all_indexes[0]
        for idx in all_indexes[1:]:
            union_idx = union_idx.union(idx)
        union_idx = union_idx.sort_values()
        union_idx = union_idx[union_idx.dayofweek < 5]

        for sym, df in raw_histories.items():
            if df is not None and not df.empty:
                re_df = df.reindex(union_idx).ffill(limit=3)
                synced_histories[sym] = re_df
            else:
                synced_histories[sym] = pd.DataFrame()
    else:
        synced_histories = raw_histories

    # chart_maker.py 側で動的基準線を直接参照できるようメタデータを同梱
    synced_histories["_metrics"] = metrics_map

    # VIX / VIX3M 比（構造境界型：固定閾値1.00を厳守）
    vix_m = metrics_map.get("^VIX", {})
    vix3m_m = metrics_map.get("^VIX3M", {})
    if vix_m.get("valid") and vix3m_m.get("valid") and vix3m_m["curr"] > 0:
        ratio = vix_m["curr"] / vix3m_m["curr"]
        if ratio >= 1.00:
            ts_state = "🔴逆転(バックワーデーション：短期パニック警戒)"
        elif ratio >= 0.95:
            ts_state = "🟡接近(フラット化：短期ヘッジ需要増)"
        elif ratio < 0.85:
            ts_state = "🟢順イールド(コンタンゴ安定：楽観/通常)"
        else:
            ts_state = "🟢通常(コンタンゴ)"
        ratio_line = f"VIX/VIX3M比(期間構造): {ratio:.3f} [{ts_state}]"
    else:
        ratio_line = "VIX/VIX3M比(期間構造): N/A"

    lines = [
        f"・{ratio_line}",
        "・判定基準: 過去252営業日(1年)のパーセンタイル分位(中期レジーム自動補正) ＋ 直近20日Zスコア(短期急変) ＋ 絶対上限キャップ",
        "指標名 | 現在値(基準日) | 当日レンジ(安〜高) | 1D比 | 5D比 | 20D比 | 5日瞬間高値 | 20日Zスコア | 60日位置 | 1年分位(動的基準値) | 警戒判定",
    ]

    for sym, label, _, _ in MACRO_SYMBOLS:
        m = metrics_map.get(sym, {})
        if not m.get("valid"):
            lines.append(f"{label} | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | ➖取得不可")
            continue

        fmt = ",.0f" if sym == "^N225" else (",.1f" if sym == "^SOX" else ".2f")
        curr_s = f"{m['curr']:{fmt}}({m['date_str']})"
        rng_s = f"{m['today_low']:{fmt}}〜{m['today_high']:{fmt}}"
        h5_s = f"{m['high_5d']:{fmt}}"
        ref_s = f"{m['pct252']:.0f}% (基準:{m['dyn_ref']:{fmt}})"

        lines.append(
            f"{label} | {curr_s} | {rng_s} | "
            f"{m['chg_1d']:+.2f}% | {m['chg_5d']:+.2f}% | {m['chg_20d']:+.2f}% | "
            f"{h5_s} | {m['z20']:+.2f}σ | {m['pct60']:.0f}% | {ref_s} | {m['alert']}"
        )

    return "\n".join(lines), synced_histories


# ==========================================
# 5. 日本株・米国株 銘柄名取得 ＆ キーワード検索関数
# ==========================================
def get_japanese_name(code: str) -> str:
    """株探 → Yahoo!ファイナンス日本版 → yfinance の順で日本株の社名を取得"""
    code_clean = str(code).strip().upper().replace(".T", "")

    # 1. 株探
    html = _fetch_url_text(f"https://kabutan.jp/stock/?code={code_clean}")
    if html:
        soup = BeautifulSoup(html, "html.parser")
        h2 = soup.select_one("div.company_block h3, h2")
        if h2:
            txt = h2.get_text(" ", strip=True)
            txt = re.sub(rf"^{code_clean}\s*", "", txt).strip()
            if txt:
                return txt

    # 2. Yahoo!ファイナンス日本版
    yh_html = _fetch_url_text(f"https://finance.yahoo.co.jp/quote/{code_clean}.T")
    if yh_html:
        soup = BeautifulSoup(yh_html, "html.parser")
        title_tag = soup.find("title")
        if title_tag:
            t_txt = title_tag.get_text(strip=True)
            m = re.match(r"^(.+?)\【", t_txt)
            if m:
                name_cand = m.group(1).replace("(株)", "").strip()
                if name_cand:
                    return name_cand

    # 3. yfinance
    try:
        info = yf.Ticker(f"{code_clean}.T").info or {}
        return info.get("shortName") or info.get("longName") or code_clean
    except Exception:
        return code_clean


def get_us_stock_name(code: str) -> str:
    """yfinance または Yahoo US Search API から米国株の企業名を取得"""
    sym = str(code).strip().upper()
    try:
        info = yf.Ticker(sym).info or {}
        name = info.get("shortName") or info.get("longName")
        if name:
            return name
    except Exception:
        pass

    url = f"https://query2.finance.yahoo.com/v1/finance/search?q={urllib.parse.quote(sym)}&quotesCount=5"
    data = _fetch_url_text(url, is_json=True)
    for q in data.get("quotes", []):
        if q.get("symbol", "").upper() == sym:
            return q.get("shortname") or q.get("longname") or sym
    return sym


def search_japanese_code_by_name(query: str) -> list:
    """
    企業名キーワードから日本株の候補を "CODE - 社名" のリスト形式で返す。
    Yahoo!ファイナンス日本版検索 ＆ 株探検索のハイブリッド。
    """
    if not query or not query.strip():
        return []
    q_enc = urllib.parse.quote(query.strip())
    results = []
    seen_codes = set()

    # 1. Yahoo!ファイナンス日本版 検索
    yh_url = f"https://finance.yahoo.co.jp/search/?query={q_enc}"
    html = _fetch_url_text(yh_url)
    if html:
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.select("a[href*='/quote/']"):
            href = a.get("href", "")
            m = re.search(r"/quote/([0-9]{3}[0-9A-Za-z])(?:\.T)?", href)
            if m:
                c = m.group(1).upper()
                if c not in seen_codes:
                    name_txt = a.get_text(" ", strip=True)
                    name_txt = re.sub(r"s+", " ", name_txt).replace(c, "").strip("  -：【】()")
                    if not name_txt or len(name_txt) > 40:
                        name_txt = get_japanese_name(c)
                    seen_codes.add(c)
                    results.append(f"{c} - {name_txt}")
                    if len(results) >= 10:
                        return results

    # 2. 株探 フォールバック
    kb_url = f"https://kabutan.jp/search/?q={q_enc}"
    kb_html = _fetch_url_text(kb_url)
    if kb_html:
        soup = BeautifulSoup(kb_html, "html.parser")
        for a in soup.select("a[href*='/stock/?code=']"):
            href = a.get("href", "")
            m = re.search(r"code=([0-9]{3}[0-9A-Za-z])", href)
            if m:
                c = m.group(1).upper()
                if c not in seen_codes:
                    name_txt = a.get_text(strip=True)
                    if not name_txt or name_txt == c:
                        name_txt = get_japanese_name(c)
                    seen_codes.add(c)
                    results.append(f"{c} - {name_txt}")
                    if len(results) >= 10:
                        break

    return results


def search_us_ticker_by_name(query: str) -> list:
    """
    英語社名・ティッカーから米国株の候補を "TICKER - Company Name" のリスト形式で返す。
    """
    if not query or not query.strip():
        return []
    url = f"https://query2.finance.yahoo.com/v1/finance/search?q={urllib.parse.quote(query.strip())}&quotesCount=10&newsCount=0"
    data = _fetch_url_text(url, is_json=True)
    results = []
    for item in data.get("quotes", []):
        quote_type = item.get("quoteType", "")
        symbol = item.get("symbol", "")
        exch = item.get("exchange", "")
        # 日本株(.T)や暗号資産などを除外し米国市場銘柄に限定
        if quote_type in ["EQUITY", "ETF"] and "." not in symbol and exch in ["NMS", "NYQ", "NGM", "PCX", "ASE", "BTS", "NCM"]:
            sname = item.get("shortname") or item.get("longname") or symbol
            results.append(f"{symbol} - {sname}")
    return results


# ==========================================
# 6. 日本株ファンダメンタルズ 2段階取得（株探 ＋ Yahoo!日本版補完）
# ==========================================
def _clean_metric_number(raw_str: str) -> str:
    """
    app.py 側で『倍』『%』『億円』を付与するため、単位や空白を除去した純粋な数値文字列に整形する。
    """
    if not raw_str:
        return "N/A"
    s = raw_str.strip().replace(",", "")
    if any(k in s for k in ["－", "-", "―", "N/A", "赤字"]):
        return "N/A"
    m = re.search(r"([+-]?\d+(?:\.\d+)?)", s)
    return m.group(1) if m else "N/A"


def _clean_market_cap_oku(raw_str: str) -> str:
    """時価総額文字列を『億円』単位のカンマ付き数値文字列に変換（app.pyが末尾に『億円』を付与するため）"""
    if not raw_str:
        return "N/A"
    s = raw_str.strip().replace(",", "")
    if "兆" in s:
        m_cho = re.search(r"(\d+(?:\.\d+)?)兆", s)
        m_oku = re.search(r"兆(\d+(?:\.\d+)?)億", s)
        total_oku = 0.0
        if m_cho:
            total_oku += float(m_cho.group(1)) * 10000.0
        if m_oku:
            total_oku += float(m_oku.group(1))
        return f"{int(round(total_oku)):,}" if total_oku > 0 else "N/A"
    elif "百万" in s:
        m_hyaku = re.search(r"(\d+(?:\.\d+)?)", s)
        if m_hyaku:
            oku = float(m_hyaku.group(1)) / 100.0
            return f"{round(oku, 1):,}"
    else:
        m = re.search(r"(\d+(?:\.\d+)?)", s)
        if m:
            val = float(m.group(1))
            return f"{int(round(val)):,}" if val >= 100 else f"{val}"
    return "N/A"


def get_japanese_fundamentals(stock_code: str) -> dict:
    """
    app.py が参照するキー ('per', 'pbr', 'div', 'market_cap', 'margin', 'earnings_date') を返す。
    第1段階: 株探（curl_cffi Chrome偽装）
    第2段階: 海外IP遮断等で欠損時は Yahoo!ファイナンス日本版 から自動補完
    """
    code_clean = str(stock_code).strip().upper().replace(".T", "")
    res = {
        "per": "N/A",
        "pbr": "N/A",
        "div": "N/A",
        "margin": "N/A",
        "market_cap": "N/A",
        "earnings_date": "要検索確認",
    }

    # --- Step 1: 株探 ---
    kb_html = _fetch_url_text(f"https://kabutan.jp/stock/?code={code_clean}")
    if kb_html:
        soup = BeautifulSoup(kb_html, "html.parser")
        info_box = soup.select_one("div#stockinfo_i3")
        if info_box:
            tds = info_box.select("td")
            if len(tds) >= 4:
                res["per"] = _clean_metric_number(tds[0].get_text(strip=True))
                res["pbr"] = _clean_metric_number(tds[1].get_text(strip=True))
                res["div"] = _clean_metric_number(tds[2].get_text(strip=True))
                res["margin"] = _clean_metric_number(tds[3].get_text(strip=True))
            cap_td = info_box.select_one("td.v_zika2")
            if cap_td:
                res["market_cap"] = _clean_market_cap_oku(cap_td.get_text(strip=True))

        full_txt = soup.get_text(" ", strip=True)
        m_ed = re.search(r"決算発表予定日[^\d]*(\d{4}/\d{1,2}/\d{1,2}|\d{1,2}/\d{1,2})", full_txt)
        if m_ed:
            res["earnings_date"] = f"{m_ed.group(1)}頃(株探予定)"

    # --- Step 2: Yahoo!ファイナンス日本版（補完） ---
    if any(res[k] == "N/A" for k in ["per", "pbr", "div", "margin", "market_cap"]) or res["earnings_date"] == "要検索確認":
        yh_html = _fetch_url_text(f"https://finance.yahoo.co.jp/quote/{code_clean}.T")
        if yh_html:
            ysoup = BeautifulSoup(yh_html, "html.parser")
            ytxt = ysoup.get_text(" ", strip=True)

            if res["per"] == "N/A":
                m = re.search(r"PER[^\d－\-]*([0-9,.]+)\s*倍", ytxt)
                if m:
                    res["per"] = _clean_metric_number(m.group(1))
            if res["pbr"] == "N/A":
                m = re.search(r"PBR[^\d－\-]*([0-9,.]+)\s*倍", ytxt)
                if m:
                    res["pbr"] = _clean_metric_number(m.group(1))
            if res["div"] == "N/A":
                m = re.search(r"配当利回り[^\d－\-]*([0-9,.]+)\s*%", ytxt)
                if m:
                    res["div"] = _clean_metric_number(m.group(1))
            if res["margin"] == "N/A":
                m = re.search(r"信用倍率[^\d－\-]*([0-9,.]+)\s*倍", ytxt)
                if m:
                    res["margin"] = _clean_metric_number(m.group(1))
            if res["market_cap"] == "N/A":
                m = re.search(r"時価総額[^\d]*([0-9,]+(?:\.\d+)?\s*(?:百万円|億円|兆円))", ytxt)
                if m:
                    res["market_cap"] = _clean_market_cap_oku(m.group(1))
            if res["earnings_date"] == "要検索確認":
                m = re.search(r"決算発表[^\d]*(\d{4}/\d{1,2}/\d{1,2}|\d{1,2}月\d{1,2}日)", ytxt)
                if m:
                    res["earnings_date"] = f"{m.group(1)}頃(Yahoo予定)"

    return res


# ==========================================
# 7. EDINET / 適時開示・大量保有報告書 ＆ Googleニュース取得
# ==========================================
def get_edinet_documents(stock_code: str, days: int = 60) -> tuple:
    """
    app.py から呼ばれる開示情報取得関数。
    戻り値: (edinet_str: str, edinet_reasons: list)
    株探の開示ニュース（適時開示・5%ルール大量保有報告書・決算短信）から直近の重要開示を抽出する。
    """
    code_clean = str(stock_code).strip().upper().replace(".T", "")
    cutoff_dt = datetime.datetime.now() - datetime.timedelta(days=days)
    items = []
    reasons = []

    url = f"https://kabutan.jp/stock/news?code={code_clean}&b=k"
    html = _fetch_url_text(url)
    if html:
        soup = BeautifulSoup(html, "html.parser")
        for tr in soup.select("table.s_news_list tr"):
            time_el = tr.select_one("td.news_time time, td time")
            link_el = tr.select_one("a")
            if not link_el:
                continue
            title = link_el.get_text(" ", strip=True)
            dt_str = time_el.get_text(strip=True) if time_el else ""

            # 日付フィルタ（解析可能な場合）
            m_dt = re.search(r"(\d{2})/(\d{2})/(\d{2})", dt_str)
            if m_dt:
                yr = 2000 + int(m_dt.group(1))
                mo = int(m_dt.group(2))
                da = int(m_dt.group(3))
                try:
                    doc_dt = datetime.datetime(yr, mo, da)
                    if doc_dt < cutoff_dt:
                        continue
                except Exception:
                    pass

            items.append(f"・[{dt_str}] {title}" if dt_str else f"・{title}")
            if any(k in title for k in ["大量保有", "5％", "5%", "変更報告", "自社株買", "上方修正", "下方修正", "増配", "減配", "株式分割", "TOB"]):
                reasons.append(title[:25])
            if len(items) >= 8:
                break

    if not items:
        return "（直近の重要な適時開示・EDINET大量保有報告は検出されませんでした）", []

    return "\n".join(items), reasons[:3]


def get_news(name: str, is_jp: bool = True, edinet_reasons: list = None) -> str:
    """
    Google News RSS から対象銘柄の直近ニュースヘッドラインを取得。
    """
    if not name:
        return "（ニュース取得なし）"

    clean_name = re.sub(r"\(株\)|株式会社|ホールディングス|HD", "", name).strip() or name
    if is_jp:
        q = f'"{clean_name}" 株価 OR 決算 OR 業績'
        rss_url = f"https://news.google.com/rss/search?q={urllib.parse.quote(q)}&hl=ja&gl=JP&ceid=JP:ja"
    else:
        q = f'"{clean_name}" stock OR earnings OR guidance'
        rss_url = f"https://news.google.com/rss/search?q={urllib.parse.quote(q)}&hl=en-US&gl=US&ceid=US:en"

    xml_text = _fetch_url_text(rss_url, timeout=8)
    headlines = []
    if xml_text:
        try:
            root = ET.fromstring(xml_text)
            for item in root.findall(".//item")[:6]:
                title_el = item.find("title")
                pub_el = item.find("pubDate")
                if title_el is not None and title_el.text:
                    pub_short = ""
                    if pub_el is not None and pub_el.text:
                        pub_short = f" ({pub_el.text[:16]})"
                    headlines.append(f"・{title_el.text}{pub_short}")
        except Exception:
            pass

    if not headlines:
        return "（直近の関連ニュースヘッドライン取得なし）"
    return "\n".join(headlines)