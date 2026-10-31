import streamlit as st
import yfinance as yf
import pandas as pd
import datetime
import fear_greed
import feedparser
import urllib.parse
import requests
import re
import difflib
from concurrent.futures import ThreadPoolExecutor, as_completed
import html


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
    try:
        vix_df = yf.Ticker("^VIX").history(period="5d", auto_adjust=False)
        current_vix = round(vix_df['Close'].iloc[-1], 2) if not vix_df.empty else "取得失敗"
    except Exception:
        current_vix = "取得失敗"
    try:
        fg_data = fear_greed.get()
        return f"Fear & Greed Index: {round(fg_data['score'], 1)} ({fg_data['rating']}), VIX: {current_vix}"
    except Exception:
        return f"Fear & Greed Index: 取得エラー, VIX: {current_vix}"


@st.cache_data(ttl=3600)
def get_edinet_documents(stock_code_4digit, days=60):
    edinet_key = st.secrets.get("EDINET_API_KEY", "")
    if not edinet_key:
        return "EDINET APIキー未設定", []
    try:
        df_code = _load_edinet_csv()
        # 4桁数字コードおよび英文字入りコード（例: 296A）の両方に対応
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

    # EDINETに無い新規上場銘柄等は株探のタイトルからフォールバック取得
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

    # query2直接アクセスがブロックされた場合はyfinance内部経由で取得
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
    株探(kabutan.jp)の銘柄ページ内 id="stockinfo_i3" テーブルから
    PER・PBR・利回り・信用倍率・時価総額をHTMLタグ構造に沿って正確に抽出する。
    """
    result = {'per': 'N/A', 'pbr': 'N/A', 'div': 'N/A', 'margin': 'N/A', 'market_cap': 'N/A'}
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

        # id="stockinfo_i3" ブロックを切り出して <td> 要素を順番通りに抽出
        block_match = re.search(
            r'<div[^>]*id=["\']stockinfo_i3["\'][^>]*>(.*?)</div>',
            html_text,
            re.DOTALL | re.IGNORECASE
        )
        target_html = block_match.group(1) if block_match else html_text

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

        # stockinfo_i3 の最初の4つの <td> は必ず [PER, PBR, 利回り, 信用倍率]
        if len(td_clean_list) >= 4 and block_match:
            per_v = _extract_val(td_clean_list[0], '倍')
            pbr_v = _extract_val(td_clean_list[1], '倍')
            div_v = _extract_val(td_clean_list[2], '％')
            mrg_v = _extract_val(td_clean_list[3], '倍')

            result['per'] = '測定不能(赤字・未定等)' if per_v == '－' else per_v
            result['pbr'] = pbr_v if pbr_v != '－' else 'N/A'
            result['div'] = div_v if div_v != '－' else 'N/A'
            result['margin'] = mrg_v if mrg_v != '－' else 'N/A'

        # 時価総額の抽出（「1兆2,345億円」および「1,234億円」の両表記に対応）
        if len(td_clean_list) >= 5 and block_match and '億円' in td_clean_list[4]:
            result['market_cap'] = td_clean_list[4].replace('億円', '')
        else:
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

        # 5桁コード（末尾0）を4桁表示に整形（英文字入りコードにも対応）
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

    # フォールバック：yfinanceのSearchクラスを使用
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