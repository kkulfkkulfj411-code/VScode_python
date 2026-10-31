import streamlit as st
import yfinance as yf
import pandas as pd
import numpy as np
import datetime
from datetime import date, timedelta, timezone, time as dt_time
import calendar
import fear_greed
import feedparser
import urllib.parse
import requests
import re
import difflib
from concurrent.futures import ThreadPoolExecutor, as_completed
import html
from bs4 import BeautifulSoup

try:
    from curl_cffi import requests as cffi_requests
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False


def _http_get_html(url: str, timeout: int = 8) -> str:
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache"
    }
    if HAS_CURL_CFFI:
        try:
            res = cffi_requests.get(url, headers=headers, impersonate="chrome", timeout=timeout)
            if res.status_code == 200:
                return res.content.decode("utf-8", errors="ignore")
        except Exception:
            pass

    headers["User-Agent"] = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    )
    res = requests.get(url, headers=headers, timeout=timeout)
    if res.status_code == 200:
        return res.content.decode("utf-8", errors="ignore")
    return ""


def get_dynamic_jp_holidays(year: int) -> set:
    holidays = set()
    fixed_days = [
        (1, 1), (1, 2), (1, 3),
        (2, 11), (2, 23),
        (4, 29),
        (5, 3), (5, 4), (5, 5),
        (8, 11),
        (11, 3), (11, 23),
        (12, 31)
    ]
    for m, d in fixed_days:
        holidays.add(date(year, m, d))

    def nth_monday(y, m, n):
        first_day = date(y, m, 1)
        first_monday_day = 1 + (7 - first_day.weekday()) % 7
        return date(y, m, first_monday_day + (n - 1) * 7)

    holidays.add(nth_monday(year, 1, 2))
    holidays.add(nth_monday(year, 7, 3))
    holidays.add(nth_monday(year, 9, 3))
    holidays.add(nth_monday(year, 10, 2))

    vernal_day = int(20.8431 + 0.242194 * (year - 1980) - int((year - 1980) / 4))
    autumnal_day = int(23.2488 + 0.242194 * (year - 1980) - int((year - 1980) / 4))
    holidays.add(date(year, 3, vernal_day))
    holidays.add(date(year, 9, autumnal_day))

    keiro = nth_monday(year, 9, 3)
    shubun = date(year, 9, autumnal_day)
    if (shubun - keiro).days == 2:
        holidays.add(keiro + timedelta(days=1))

    base_holidays = sorted(list(holidays))
    for h in base_holidays:
        if h.weekday() == 6:
            sub = h + timedelta(days=1)
            while sub in holidays:
                sub += timedelta(days=1)
            holidays.add(sub)

    return holidays


def get_sbi_order_limit_dates() -> str:
    weekdays_jp = ["月", "火", "水", "木", "金", "土", "日"]
    jst = timezone(timedelta(hours=9))
    now = datetime.datetime.now(jst)
    curr_date = now.date()

    jp_holidays = get_dynamic_jp_holidays(curr_date.year) | get_dynamic_jp_holidays(curr_date.year + 1)

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
        f"[HYPER SBI 2 期間指定カレンダー（JST {now.strftime('%m/%d %H:%M')} 時点・東証祝日自動算出）]\n"
        f"・初回有効営業日(1営業日目): {fmt_d(biz_days[0])}\n"
        f"・第1注文(新規買い)推奨期限目安: 3営業日目={fmt_d(biz_days[2])} | 5営業日目={fmt_d(biz_days[4])} | 10営業日目={fmt_d(biz_days[9])}\n"
        f"・第2注文(決済OCO)システム最長上限(15営業日目): {fmt_d(biz_days[14])}\n"
        f"・月末銘柄の権利付き最終日目安: {m1}月末基準={fmt_d(kenri_curr)} / {m2}月末基準={fmt_d(kenri_next)} （※当該銘柄の権利確定月である場合、権利付き最終日をまたぐ期間指定は不可のためその日が上限となる）\n"
    )


@st.cache_data(ttl=86400, show_spinner=False)
def _load_edinet_csv():
    try:
        df_code = pd.read_csv('EdinetcodeDlInfo.csv', encoding='cp932', skiprows=1)
    except UnicodeDecodeError:
        df_code = pd.read_csv('EdinetcodeDlInfo.csv', encoding='utf-8', skiprows=1)
    df_code['証券コード_数値'] = pd.to_numeric(df_code['証券コード'], errors='coerce')
    return df_code


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
            df = yf.Ticker(sym).history(period="4mo", interval="1d", auto_adjust=False)
            if not df.empty:
                df.index = pd.to_datetime(df.index)
                if df.index.tz is not None:
                    df.index = df.index.tz_localize(None)
                df = df.dropna(subset=['Close'])
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

    def _judge_regime(sym, curr, z_score, diff_1d):
        if pd.isna(curr):
            return "N/A"
        if sym == "^VIX":
            if curr >= 25:
                return "🔴パニック圏(ピークアウト待ち)" if diff_1d > 0 else "🟡高ボラ収縮開始(反発監視)"
            elif curr >= 20:
                return "🟠警戒圏(ロット抑制)"
            return "🟢平穏"
        if sym == "^VVIX":
            if curr >= 115:
                return "🔴VIX急騰警報(115超)"
            elif curr >= 100:
                return "🟠変動拡大注意(100超)"
            return "🟢安定"
        if sym == "^SKEW":
            if curr >= 145:
                return "🔴テールリスク極大(145超)"
            elif curr >= 135:
                return "🟠機関プット買い警戒(135超)"
            return "🟢通常"
        if sym == "^MOVE":
            if curr >= 120:
                return "🔴金利発ショック警戒(120超)"
            elif curr >= 105:
                return "🟠債券ボラ高止まり"
            return "🟢安定"
        if not pd.isna(z_score):
            if z_score >= 2.0:
                return "🔺急騰スパイク(+2σ超)"
            elif z_score <= -2.0:
                return "🔻急落スパイク(-2σ超)"
            elif z_score >= 1.0:
                return "↗上昇基調"
            elif z_score <= -1.0:
                return "↘下落基調"
        return "➡️中立圏"

    rows = [
        "指標名 | 現在値 | 当日レンジ(安〜高) | 前日比(1D) | 5日比(5D) | 5日瞬間高値 | 20日比(20D) | 20日Zスコア | 60日位置 | 状態・警戒判定"
    ]

    for sym, label, digits, unit in macro_specs:
        df = histories.get(sym, pd.DataFrame())
        if df is None or df.empty:
            rows.append(f"{label} | 取得失敗 | - | - | - | - | - | - | - | -")
            continue

        c_series = df['Close']
        h_series = df['High'] if 'High' in df.columns else c_series
        l_series = df['Low'] if 'Low' in df.columns else c_series

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

        regime_str = _judge_regime(sym, curr, z_val, diff_1d_val)

        rows.append(
            f"{label} | {_fmt_v(curr, digits, unit)} | "
            f"{_fmt_v(day_l, digits)}〜{_fmt_v(day_h, digits)} | "
            f"{chg_1d_str} | {chg_5d_str} | {_fmt_v(max_5d_h, digits, unit)} | "
            f"{chg_20d_str} | {z_str} | {pos_str} | {regime_str}"
        )

    vix_df = histories.get("^VIX", pd.DataFrame())
    vix3m_df = histories.get("^VIX3M", pd.DataFrame())
    term_line = ""
    if not vix_df.empty and not vix3m_df.empty:
        c_vix = vix_df['Close'].iloc[-1]
        c_vix3m = vix3m_df['Close'].iloc[-1]
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
        f"[グローバルマクロ＆ボラティリティ時系列マトリクス（瞬間高安値・Zスコア付）]\n"
        + "\n".join(rows)
    )
    return macro_text, histories


@st.cache_data(ttl=1800, show_spinner=False)
def get_macro_data():
    text, _ = get_macro_data_and_history()
    return text


@st.cache_data(ttl=3600)
def get_edinet_documents(stock_code_4digit, days=60):
    edinet_key = st.secrets.get("EDINET_API_KEY", "")
    if not edinet_key:
        return "EDINET APIキー未設定", []
    try:
        df_code = _load_edinet_csv()
        code_str = str(stock_code_4digit).strip().upper()
        if code_str.isdigit():
            target_sec_code = float(code_str) * 10
            match_row = df_code[df_code['証券コード_数値'] == target_sec_code]
        else:
            sec_str_series = df_code['証券コード'].astype(str).str.strip().str.upper()
            match_row = df_code[sec_str_series.str.startswith(code_str)]

        if match_row.empty:
            return "EDINETコードリストに該当銘柄がありません", []
        edinet_code = match_row['ＥＤＩＮＥＴコード'].values[0]
    except Exception:
        return "EDINETコード変換用CSVの読み込みエラー", []

    date_list = [(datetime.date.today() - datetime.timedelta(days=i)).strftime('%Y-%m-%d') for i in range(days)]
    all_found, all_reasons = [], []

    def fetch_edinet_for_date(target_date, edinet_code, api_key):
        url = "https://api.edinet-fsa.go.jp/api/v2/documents.json"
        params = {"date": target_date, "type": 2, "Subscription-Key": api_key}
        found_docs, reasons = [], []
        try:
            res = requests.get(url, params=params, timeout=10)
            if res.status_code == 200:
                data = res.json()
                if "results" in data and isinstance(data["results"], list):
                    for doc in data["results"]:
                        if doc.get("edinetCode") == edinet_code:
                            title = doc.get("title", "タイトル不明")
                            desc = doc.get("docDescription")
                            reason = doc.get("currentReportReason")
                            content_details = []
                            if desc:
                                content_details.append(f"概要: {desc}")
                            if reason:
                                content_details.append(f"事由: {reason}")
                                if "臨時報告書" in title or "修正" in title or "配当" in title:
                                    reasons.append(reason)
                            content_str = " | ".join(content_details) if content_details else "詳細記載なし"
                            found_docs.append(f"[{target_date}] {title} ({content_str})")
        except Exception:
            pass
        return found_docs, reasons

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(fetch_edinet_for_date, d, edinet_code, edinet_key): d for d in date_list}
        for future in as_completed(futures):
            docs, reasons = future.result()
            if docs:
                all_found.extend(docs)
            if reasons:
                all_reasons.extend(reasons)

    all_found.sort(reverse=True)
    res_str = "\n".join(all_found) if all_found else f"直近{days}日間のEDINET公式開示書類はありません。"
    return res_str, list(set(all_reasons))


@st.cache_data(ttl=3600)
def get_news(company_name, is_jp, edinet_reasons=None):
    news_list = []
    if is_jp:
        q1 = urllib.parse.quote(f"{company_name} (TOPIX OR 再編 OR アナリスト OR レーティング OR 目標株価 OR 株探 OR 四季報 OR 日経 OR 決算 OR 増配)")
        url1 = f"https://news.google.com/rss/search?q={q1}&hl=ja&gl=JP&ceid=JP:ja"
    else:
        q1 = urllib.parse.quote(f"{company_name} stock (earnings OR upgrade OR target OR guidance)")
        url1 = f"https://news.google.com/rss/search?q={q1}&hl=en-US&gl=US&ceid=US:en"

    try:
        feed = feedparser.parse(url1)
        for entry in feed.entries[:8]:
            title = entry.title.split(' - ')[0]
            pub = entry.get('published', '')

            desc = html.unescape(re.sub(r'<[^>]+>', '', entry.get('description', '')))
            desc_clean = re.sub(r'\s+', ' ', desc).strip()
            if len(desc_clean) > 120:
                desc_clean = desc_clean[:120] + "..."

            news_item = f"■ {title} ({pub[:16]})\n   概要: {desc_clean}"

            if is_jp:
                if company_name in title or any(kw in title for kw in ['TOPIX', '再編', '決算', '配当', '株', '業績', 'アナリスト', 'レーティング', '目標']):
                    news_list.append(news_item)
            else:
                news_list.append(news_item)
    except Exception:
        pass

    if is_jp and edinet_reasons:
        for reason in edinet_reasons:
            match = re.search(r'（(.*?)）', reason)
            keyword = match.group(1) if match else reason[:15]
            keyword = keyword.replace("の件", "").replace("に関する", "")
            q2 = urllib.parse.quote(f"{company_name} {keyword}")
            url2 = f"https://news.google.com/rss/search?q={q2}&hl=ja&gl=JP&ceid=JP:ja"
            try:
                feed2 = feedparser.parse(url2)
                for entry in feed2.entries[:3]:
                    title = entry.title.split(' - ')[0]
                    pub = entry.get('published', '')
                    desc = html.unescape(re.sub(r'<[^>]+>', '', entry.get('description', '')))
                    desc_clean = re.sub(r'\s+', ' ', desc).strip()
                    if len(desc_clean) > 120:
                        desc_clean = desc_clean[:120] + "..."

                    news_list.append(f"■ [EDINET連動深掘り] {title} ({pub[:16]})\n   概要: {desc_clean}")
            except Exception:
                pass

    unique_news = []
    seen_titles = set()
    for item in news_list:
        t = item.split('\n')[0]
        if t not in seen_titles:
            seen_titles.add(t)
            unique_news.append(item)

    unique_news = unique_news[:9]
    return "\n\n".join(unique_news) if unique_news else "直近の重要な関連ニュースは見つかりませんでした。"


@st.cache_data(ttl=3600)
def get_japanese_name(stock_code):
    try:
        df_code = _load_edinet_csv()
        code_str = str(stock_code).strip().upper()
        if code_str.isdigit():
            target_sec_code = float(code_str) * 10
            match_row = df_code[df_code['証券コード_数値'] == target_sec_code]
        else:
            sec_str_series = df_code['証券コード'].astype(str).str.strip().str.upper()
            match_row = df_code[sec_str_series.str.startswith(code_str)]

        if not match_row.empty:
            raw_name = match_row['提出者名'].values[0]
            for rm in ['株式会社', 'ホールディングス', 'グループ本社', 'グループ']:
                raw_name = str(raw_name).replace(rm, '')
            return raw_name.strip()
    except Exception:
        pass

    try:
        html_text = _http_get_html(f"https://kabutan.jp/stock/?code={stock_code}", timeout=5)
        m = re.search(r'<title>(.*?)【', html_text)
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
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200:
            quotes = res.json().get('quotes', [])
            for q in quotes:
                if q.get('symbol', '').upper() == code_up:
                    return q.get('longname') or q.get('shortname') or code_up
    except Exception:
        pass

    try:
        info = yf.Ticker(code_up).info
        if isinstance(info, dict):
            return info.get('longName') or info.get('shortName') or code_up
    except Exception:
        pass
    return code_up


@st.cache_data(ttl=3600)
def get_japanese_fundamentals(stock_code):
    result = {
        'per': 'N/A', 'pbr': 'N/A', 'div': 'N/A',
        'margin': 'N/A', 'market_cap': 'N/A', 'earnings_date': '要検索確認'
    }

    def _clean_num(raw_str):
        if not raw_str:
            return 'N/A'
        s = re.sub(r'\s+', '', raw_str).replace(',', '')
        if any(x in s for x in ['－', '―', '---', '赤字']):
            return '－'
        m = re.search(r'[0-9]+(?:\.[0-9]+)?', s)
        return m.group(0) if m else 'N/A'

    # 1. 株探からの取得試行 (curl_cffi + BeautifulSoup)
    try:
        html_text = _http_get_html(f"https://kabutan.jp/stock/?code={stock_code}", timeout=8)
        if html_text:
            soup = BeautifulSoup(html_text, "html.parser")
            info_div = soup.find("div", id="stockinfo_i3")
            if info_div:
                tds = [td.get_text(strip=True) for td in info_div.find_all("td")]
                if len(tds) >= 4:
                    per_v = _clean_num(tds[0])
                    pbr_v = _clean_num(tds[1])
                    div_v = _clean_num(tds[2])
                    mrg_v = _clean_num(tds[3])

                    result['per'] = '測定不能(赤字・未定等)' if per_v == '－' else per_v
                    result['pbr'] = pbr_v if pbr_v != '－' else 'N/A'
                    result['div'] = div_v if div_v != '－' else 'N/A'
                    result['margin'] = mrg_v if mrg_v != '－' else 'N/A'

                if len(tds) >= 5 and '億円' in tds[4]:
                    result['market_cap'] = tds[4].replace('億円', '').strip()

            if result['market_cap'] == 'N/A':
                for th in soup.find_all("th"):
                    if "時価総額" in th.get_text():
                        nxt_td = th.find_next_sibling("td")
                        if nxt_td:
                            mc = nxt_td.get_text(strip=True).replace('億円', '').strip()
                            if mc and mc not in ['－', '-']:
                                result['market_cap'] = mc
                                break

            earn_m = re.search(
                r'決算発表予定日[^0-9]*?([0-9]{4}年[0-9]{1,2}月[0-9]{1,2}日|[0-9]{4}/[0-9]{1,2}/[0-9]{1,2}|[0-9]{1,2}月[0-9]{1,2}日)',
                html_text, re.DOTALL
            )
            if earn_m:
                result['earnings_date'] = f"{earn_m.group(1)}頃(予定)"
            else:
                fy_m = re.search(r'[連単]\s*([0-9]{2})\.([0-9]{2})\s*(?:予|実)', html_text)
                if fy_m:
                    result['earnings_date'] = f"本決算 {int(fy_m.group(2))}月期 (発表日は要検索確認)"
    except Exception:
        pass

    # 2. Yahoo!ファイナンス日本版からのフォールバック補完
    if result['margin'] == 'N/A' or result['per'] == 'N/A':
        try:
            yh_html = _http_get_html(f"https://finance.yahoo.co.jp/quote/{stock_code}.T", timeout=8)
            if yh_html:
                yh_soup = BeautifulSoup(yh_html, "html.parser")
                full_txt = yh_soup.get_text(" ", strip=True)

                if result['margin'] == 'N/A':
                    m_mrg = re.search(r'信用倍率[^\d－―]*([0-9,]+(?:\.[0-9]+)?|－|―)\s*倍', full_txt)
                    if m_mrg and m_mrg.group(1) not in ['－', '―']:
                        result['margin'] = m_mrg.group(1).replace(',', '')

                if result['per'] == 'N/A':
                    m_per = re.search(r'PER[^\d－―]*([0-9,]+(?:\.[0-9]+)?|－|―)\s*倍', full_txt)
                    if m_per and m_per.group(1) not in ['－', '―']:
                        result['per'] = m_per.group(1).replace(',', '')

                if result['pbr'] == 'N/A':
                    m_pbr = re.search(r'PBR[^\d－―]*([0-9,]+(?:\.[0-9]+)?|－|―)\s*倍', full_txt)
                    if m_pbr and m_pbr.group(1) not in ['－', '―']:
                        result['pbr'] = m_pbr.group(1).replace(',', '')

                if result['div'] == 'N/A':
                    m_div = re.search(r'配当利回り[^\d－―]*([0-9,]+(?:\.[0-9]+)?|－|―)\s*%', full_txt)
                    if m_div and m_div.group(1) not in ['－', '―']:
                        result['div'] = m_div.group(1).replace(',', '')

                if result['market_cap'] == 'N/A':
                    m_mc = re.search(r'時価総額[^\d]*([0-9,]+)\s*百万円', full_txt)
                    if m_mc:
                        val_million = float(m_mc.group(1).replace(',', ''))
                        result['market_cap'] = f"{round(val_million / 100, 1):,}"
        except Exception:
            pass

    return result


@st.cache_data(ttl=3600)
def search_japanese_code_by_name(query):
    if not query:
        return []
    try:
        df_code = _load_edinet_csv().copy()
        df_code = df_code.dropna(subset=['証券コード'])

        def _fmt_sec_code(val):
            s = str(val).strip()
            if s.endswith('.0'):
                s = s[:-2]
            if len(s) == 5 and s.endswith('0'):
                return s[:4]
            return s

        df_code['証券コード_4桁'] = df_code['証券コード'].apply(_fmt_sec_code)
        df_code['提出者名'] = df_code['提出者名'].fillna('').astype(str)
        df_code['提出者名（ヨミ）'] = df_code['提出者名（ヨミ）'].fillna('').astype(str)

        mask = (
            df_code['提出者名'].str.contains(query, case=False, na=False) |
            df_code['提出者名（ヨミ）'].str.contains(query, case=False, na=False)
        )
        matches = df_code[mask].copy()

        if matches.empty:
            names = df_code['提出者名'].tolist()
            yomis = df_code['提出者名（ヨミ）'].tolist()
            close_names = difflib.get_close_matches(query, names, n=10, cutoff=0.4)
            close_yomis = difflib.get_close_matches(query, yomis, n=10, cutoff=0.4)
            mask_fuzzy = df_code['提出者名'].isin(close_names) | df_code['提出者名（ヨミ）'].isin(close_yomis)
            matches = df_code[mask_fuzzy].copy()

        matches['sort_key'] = pd.to_numeric(matches['証券コード_4桁'], errors='coerce').fillna(99999)
        matches = matches.sort_values('sort_key')
        return [f"{row['証券コード_4桁']} - {row['提出者名']}" for _, row in matches.iterrows()]
    except Exception:
        return []


@st.cache_data(ttl=3600)
def search_us_ticker_by_name(query):
    if not query:
        return []
    try:
        url = f"https://query2.finance.yahoo.com/v1/finance/search?q={urllib.parse.quote(query)}"
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200:
            data = res.json()
            quotes = data.get('quotes', [])
            matches = []
            for q in quotes:
                if q.get('quoteType') in ['EQUITY', 'ETF']:
                    symbol = q.get('symbol', '')
                    name = q.get('shortname') or q.get('longname') or ''
                    if symbol:
                        matches.append(f"{symbol} - {name}")
            if matches:
                return matches[:10]
    except Exception:
        pass

    try:
        s = yf.Search(query, max_results=10)
        matches = []
        for q in getattr(s, 'quotes', []):
            if q.get('quoteType') in ['EQUITY', 'ETF']:
                symbol = q.get('symbol', '')
                name = q.get('shortname') or q.get('longname') or ''
                if symbol:
                    matches.append(f"{symbol} - {name}")
        return matches[:10]
    except Exception:
        return []


def save_analysis_to_sheet(date_str, stock_code, name, judgement, target_price, time_limit, report_text):
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
        "report_text": report_text
    }

    try:
        response = requests.post(webapp_url, json=payload, timeout=10)
        if response.status_code == 200 and response.json().get("status") == "success":
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