import streamlit as st
import yfinance as yf
import pandas as pd
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


# --- 東証休場日（祝日・年末年始）の動的生成関数（年次ハードコード不要・半永久対応） ---
def get_dynamic_jp_holidays(year: int) -> set:
    """
    指定された年の日本の祝日および東証年末年始休場日(1/1〜1/3, 12/31)を
    標準ライブラリのみで動的に算出する（2028年以降もコード修正不要）。
    """
    holidays = set()

    # 1. 東証年末年始休場日 ＆ 固定祝日
    fixed_days = [
        (1, 1), (1, 2), (1, 3),   # 年始休場
        (2, 11), (2, 23),         # 建国記念の日、天皇誕生日
        (4, 29),                  # 昭和の日
        (5, 3), (5, 4), (5, 5),   # 憲法記念日、みどりの日、こどもの日
        (8, 11),                  # 山の日
        (11, 3), (11, 23),        # 文化の日、勤労感謝の日
        (12, 31)                  # 年末休場
    ]
    for m, d in fixed_days:
        holidays.add(date(year, m, d))

    # 2. ハッピーマンデー（第N月曜日）の算出
    def nth_monday(y, m, n):
        first_day = date(y, m, 1)
        first_monday_day = 1 + (7 - first_day.weekday()) % 7
        return date(y, m, first_monday_day + (n - 1) * 7)

    holidays.add(nth_monday(year, 1, 2))   # 成人の日（1月第2月曜）
    holidays.add(nth_monday(year, 7, 3))   # 海の日（7月第3月曜）
    holidays.add(nth_monday(year, 9, 3))   # 敬老の日（9月第3月曜）
    holidays.add(nth_monday(year, 10, 2))  # スポーツの日（10月第2月曜）

    # 3. 春分の日・秋分の日の簡易算出（2099年まで対応）
    vernal_day = int(20.8431 + 0.242194 * (year - 1980) - int((year - 1980) / 4))
    autumnal_day = int(23.2488 + 0.242194 * (year - 1980) - int((year - 1980) / 4))
    holidays.add(date(year, 3, vernal_day))
    holidays.add(date(year, 9, autumnal_day))

    # 4. 国民の休日（敬老の日と秋分の日に挟まれた平日がある場合）
    keiro = nth_monday(year, 9, 3)
    shubun = date(year, 9, autumnal_day)
    if (shubun - keiro).days == 2:
        holidays.add(keiro + timedelta(days=1))

    # 5. 振替休日（祝日が日曜日の場合、翌平日を休日に追加）
    base_holidays = sorted(list(holidays))
    for h in base_holidays:
        if h.weekday() == 6:  # 日曜日
            sub = h + timedelta(days=1)
            while sub in holidays:
                sub += timedelta(days=1)
            holidays.add(sub)

    return holidays


# --- HYPER SBI 2 期間指定カレンダー自動算出関数（JST固定・東証祝日・権利最終日考慮） ---
def get_sbi_order_limit_dates() -> str:
    """
    日本標準時(JST)の現在時刻（15:30前後）と東証の祝日・年末年始休場日を考慮し、
    HYPER SBI 2の期間指定における「起算日(1営業日目)」「3/5/10営業日目」「最長上限(15営業日目)」
    および「当月・翌月の月末権利付き最終日」を正確に算出する。
    """
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
    """EDINETコード変換用CSVを1日1回のみ読み込みメモリにキャッシュする"""
    try:
        df_code = pd.read_csv('EdinetcodeDlInfo.csv', encoding='cp932', skiprows=1)
    except UnicodeDecodeError:
        df_code = pd.read_csv('EdinetcodeDlInfo.csv', encoding='utf-8', skiprows=1)
    df_code['証券コード_数値'] = pd.to_numeric(df_code['証券コード'], errors='coerce')
    return df_code


@st.cache_data(ttl=3600)
def get_macro_data():
    """Fear & Greed、VIX（前日比付）、ドル円（前日比付）、日経平均（前日比付）を一括取得"""
    def _fetch_quote_with_diff(ticker_sym, digits=2, suffix=""):
        try:
            df = yf.Ticker(ticker_sym).history(period="5d", auto_adjust=False)
            df = df.dropna(subset=['Close'])
            if len(df) >= 2:
                curr = df['Close'].iloc[-1]
                prev = df['Close'].iloc[-2]
                diff = curr - prev
                pct = (diff / prev) * 100 if prev != 0 else 0.0
                sign = "+" if diff > 0 else ""
                curr_str = f"{int(round(curr))}" if digits == 0 else f"{curr:.{digits}f}"
                diff_str = f"{int(round(diff))}" if digits == 0 else f"{diff:.{digits}f}"
                return f"{curr_str}{suffix} (前日比 {sign}{diff_str} / {sign}{pct:.2f}%)"
            elif len(df) == 1:
                curr = df['Close'].iloc[-1]
                curr_str = f"{int(round(curr))}" if digits == 0 else f"{curr:.{digits}f}"
                return f"{curr_str}{suffix}"
        except Exception:
            pass
        return "取得失敗"

    vix_str = _fetch_quote_with_diff("^VIX", digits=2)
    usdjpy_str = _fetch_quote_with_diff("USDJPY=X", digits=2, suffix="円")
    nikkei_str = _fetch_quote_with_diff("^N225", digits=0, suffix="円")

    try:
        fg_data = fear_greed.get()
        fg_str = f"{round(fg_data['score'], 1)} ({fg_data['rating']})"
    except Exception:
        fg_str = "取得エラー"

    return (
        f"Fear & Greed Index: {fg_str} | VIX: {vix_str}\n"
        f"ドル円(USD/JPY): {usdjpy_str} | 日経平均株価: {nikkei_str}"
    )


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
        url = f"https://kabutan.jp/stock/?code={stock_code}"
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        res = requests.get(url, headers=headers, timeout=5)
        res.encoding = 'utf-8'
        m = re.search(r'<title>(.*?)【', res.text)
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
    """
    株探(kabutan.jp)の銘柄ページ内 id="stockinfo_i3" の <table> 全体を切り出し、
    PER・PBR・利回り・信用倍率・時価総額・決算発表予定日（または決算期）を正確に抽出する。
    """
    result = {
        'per': 'N/A', 'pbr': 'N/A', 'div': 'N/A',
        'margin': 'N/A', 'market_cap': 'N/A', 'earnings_date': '要検索確認'
    }
    try:
        url = f"https://kabutan.jp/stock/?code={stock_code}"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36'
        }
        res = requests.get(url, headers=headers, timeout=8)
        if res.status_code != 200:
            return result
        res.encoding = 'utf-8'
        html_text = res.text

        # 子divに惑わされないよう id="stockinfo_i3" から最初の </table> までを切り出す
        block_match = re.search(
            r'id=["\']stockinfo_i3["\'].*?</table>',
            html_text,
            re.DOTALL | re.IGNORECASE
        )
        if block_match:
            target_html = block_match.group(0)
            td_raw_list = re.findall(r'<td[^>]*>(.*?)</td>', target_html, re.DOTALL | re.IGNORECASE)
            td_clean_list = []
            for td in td_raw_list:
                cleaned = re.sub(r'<[^>]+>', '', td)
                cleaned = html.unescape(cleaned)
                cleaned = re.sub(r'\s+', '', cleaned).strip()
                td_clean_list.append(cleaned)

            def _extract_val(raw_str, unit_char):
                if not raw_str:
                    return 'N/A'
                val_part = raw_str.replace(unit_char, '').replace('%', '').replace('％', '').strip()
                if val_part in ['－', '-', '―', '']:
                    return '－'
                m = re.search(r'[0-9,]+(?:\.[0-9]+)?', val_part)
                return m.group(0).replace(',', '') if m else 'N/A'

            if len(td_clean_list) >= 4:
                per_v = _extract_val(td_clean_list[0], '倍')
                pbr_v = _extract_val(td_clean_list[1], '倍')
                div_v = _extract_val(td_clean_list[2], '％')
                mrg_v = _extract_val(td_clean_list[3], '倍')

                result['per'] = '測定不能(赤字・未定等)' if per_v == '－' else per_v
                result['pbr'] = pbr_v if pbr_v != '－' else 'N/A'
                result['div'] = div_v if div_v != '－' else 'N/A'
                result['margin'] = mrg_v if mrg_v != '－' else 'N/A'

            if len(td_clean_list) >= 5 and '億円' in td_clean_list[4]:
                result['market_cap'] = td_clean_list[4].replace('億円', '')

        # 時価総額のフォールバック抽出
        if result['market_cap'] == 'N/A':
            mcap_match = re.search(
                r'時価総額</th[^>]*>\s*<td[^>]*>(.*?)</td>',
                html_text,
                re.DOTALL | re.IGNORECASE
            )
            if mcap_match:
                mcap_clean = re.sub(r'<[^>]+>', '', mcap_match.group(1))
                mcap_clean = re.sub(r'\s+', '', html.unescape(mcap_clean)).replace('億円', '').strip()
                if mcap_clean and mcap_clean not in ['－', '-']:
                    result['market_cap'] = mcap_clean

        # 決算発表予定日 または 決算期の抽出
        earn_sched_match = re.search(
            r'決算発表予定日[^0-9]*?([0-9]{4}年[0-9]{1,2}月[0-9]{1,2}日|[0-9]{4}/[0-9]{1,2}/[0-9]{1,2}|[0-9]{1,2}月[0-9]{1,2}日)',
            html_text,
            re.DOTALL
        )
        if earn_sched_match:
            result['earnings_date'] = f"{earn_sched_match.group(1)}頃(予定)"
        else:
            # 業績テーブルから本決算月（例: 26.09, 27.02）を抽出
            fy_match = re.search(r'[連単単独]\s*([0-9]{2})\.([0-9]{2})\s*(?:予|実)', html_text)
            if not fy_match:
                fy_match = re.search(r'<th[^>]*>\s*(?:[連単]\s*)?([0-9]{2})\.([0-9]{2})[予]?\s*</th>', html_text)
            if fy_match:
                fy_month = int(fy_match.group(2))
                result['earnings_date'] = f"本決算 {fy_month}月期 (次回発表日は要検索確認)"

        return result
    except Exception:
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