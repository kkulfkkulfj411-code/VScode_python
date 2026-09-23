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

@st.cache_data(ttl=3600)
def get_macro_data():
    try:
        vix_df = yf.Ticker("^VIX").history(period="5d")
        current_vix = round(vix_df['Close'].iloc[-1], 2)
    except:
        current_vix = "取得失敗"
    try:
        fg_data = fear_greed.get()
        return f"Fear & Greed Index: {round(fg_data['score'], 1)} ({fg_data['rating']}), VIX: {current_vix}"
    except:
        return f"Fear & Greed Index: 取得エラー, VIX: {current_vix}"

@st.cache_data(ttl=3600)
def get_edinet_documents(stock_code_4digit, days=60):
    edinet_key = st.secrets.get("EDINET_API_KEY", "")
    if not edinet_key:
        return "EDINET APIキー未設定", []
    try:
        try:
            df_code = pd.read_csv('EdinetcodeDlInfo.csv', encoding='cp932', skiprows=1)
        except UnicodeDecodeError:
            df_code = pd.read_csv('EdinetcodeDlInfo.csv', encoding='utf-8', skiprows=1)
            
        df_code['証券コード'] = pd.to_numeric(df_code['証券コード'], errors='coerce')
        target_sec_code = float(stock_code_4digit) * 10
        match_row = df_code[df_code['証券コード'] == target_sec_code]
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
                if "results" in data:
                    for doc in data["results"]:
                        if doc.get("edinetCode") == edinet_code:
                            title = doc.get("title", "タイトル不明")
                            desc = doc.get("docDescription")
                            reason = doc.get("currentReportReason")
                            content_details = []
                            if desc: content_details.append(f"概要: {desc}")
                            if reason: 
                                content_details.append(f"事由: {reason}")
                                if "臨時報告書" in title or "修正" in title or "配当" in title:
                                    reasons.append(reason)
                            content_str = " | ".join(content_details) if content_details else "詳細記載なし"
                            found_docs.append(f"[{target_date}] {title} ({content_str})")
        except:
            pass
        return found_docs, reasons

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(fetch_edinet_for_date, d, edinet_code, edinet_key): d for d in date_list}
        for future in as_completed(futures):
            docs, reasons = future.result()
            if docs: all_found.extend(docs)
            if reasons: all_reasons.extend(reasons)
    
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
            if is_jp:
                if company_name in title or any(kw in title for kw in ['TOPIX', '再編', '決算', '配当', '株', '業績', 'アナリスト', 'レーティング', '目標']):
                    news_list.append(title)
            else:
                news_list.append(title)
    except:
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
                    news_list.append(f"[EDINET連動深掘り] {entry.title.split(' - ')[0]}")
            except:
                pass
                
    unique_news = list(dict.fromkeys(news_list))[:9]
    return " / ".join(unique_news) if unique_news else "直近の重要な関連ニュースは見つかりませんでした。"

@st.cache_data(ttl=3600)
def get_japanese_name(stock_code):
    try:
        try:
            df_code = pd.read_csv('EdinetcodeDlInfo.csv', encoding='cp932', skiprows=1)
        except UnicodeDecodeError:
            df_code = pd.read_csv('EdinetcodeDlInfo.csv', encoding='utf-8', skiprows=1)
            
        target_sec_code = float(stock_code) * 10
        df_code['証券コード'] = pd.to_numeric(df_code['証券コード'], errors='coerce')
        match_row = df_code[df_code['証券コード'] == target_sec_code]
        
        if not match_row.empty:
            raw_name = match_row['提出者名'].values[0]
            for rm in ['株式会社', 'ホールディングス', 'グループ本社', 'グループ']:
                raw_name = str(raw_name).replace(rm, '')
            return raw_name.strip()
    except Exception:
        pass
    return None

@st.cache_data(ttl=3600)
def get_us_stock_name(stock_code):
    try:
        url = f"https://query2.finance.yahoo.com/v1/finance/search?q={stock_code.upper()}"
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200:
            quotes = res.json().get('quotes', [])
            for q in quotes:
                if q.get('symbol', '').upper() == stock_code.upper():
                    return q.get('longname') or q.get('shortname') or stock_code.upper()
    except Exception:
        pass
    return stock_code.upper()

@st.cache_data(ttl=3600)
def get_japanese_fundamentals(stock_code):
    try:
        url = f"https://kabutan.jp/stock/?code={stock_code}"
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        res = requests.get(url, headers=headers, timeout=5)
        html = res.text
        
        per = re.search(r'PER[^0-9]*?([0-9\.]+)[^0-9]*?倍', html)
        pbr = re.search(r'PBR[^0-9]*?([0-9\.]+)[^0-9]*?倍', html)
        div = re.search(r'利回り[^0-9]*?([0-9\.]+)[^0-9]*?％', html)
        margin = re.search(r'信用倍率[^0-9]*?([0-9\.]+)[^0-9]*?倍', html)
        
        return {
            'per': per.group(1) if per else 'N/A',
            'pbr': pbr.group(1) if pbr else 'N/A',
            'div': div.group(1) if div else 'N/A',
            'margin': margin.group(1) if margin else 'N/A'
        }
    except Exception:
        return {'per': 'N/A', 'pbr': 'N/A', 'div': 'N/A', 'margin': 'N/A'}
    
@st.cache_data(ttl=3600)
def search_japanese_code_by_name(query):
    if not query: return []
    try:
        try:
            df_code = pd.read_csv('EdinetcodeDlInfo.csv', encoding='cp932', skiprows=1)
        except UnicodeDecodeError:
            df_code = pd.read_csv('EdinetcodeDlInfo.csv', encoding='utf-8', skiprows=1)
        
        df_code['証券コード'] = pd.to_numeric(df_code['証券コード'], errors='coerce')
        df_code = df_code.dropna(subset=['証券コード'])
        df_code['証券コード'] = (df_code['証券コード'] / 10).astype(int).astype(str)
        
        df_code['提出者名'] = df_code['提出者名'].fillna('').astype(str)
        df_code['提出者名（ヨミ）'] = df_code['提出者名（ヨミ）'].fillna('').astype(str)
        
        mask = df_code['提出者名'].str.contains(query, case=False, na=False) | \
               df_code['提出者名（ヨミ）'].str.contains(query, case=False, na=False)
        matches = df_code[mask]
        
        if matches.empty:
            names = df_code['提出者名'].tolist()
            yomis = df_code['提出者名（ヨミ）'].tolist()
            close_names = difflib.get_close_matches(query, names, n=10, cutoff=0.4)
            close_yomis = difflib.get_close_matches(query, yomis, n=10, cutoff=0.4)
            mask_fuzzy = df_code['提出者名'].isin(close_names) | df_code['提出者名（ヨミ）'].isin(close_yomis)
            matches = df_code[mask_fuzzy]

        matches['sort_key'] = pd.to_numeric(matches['証券コード'], errors='coerce')
        matches = matches.sort_values('sort_key')
        return [f"{row['証券コード']} - {row['提出者名']}" for _, row in matches.iterrows()]
    except Exception:
        return []
    
@st.cache_data(ttl=3600)
def search_us_ticker_by_name(query):
    if not query: return []
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
                    name = q.get('shortname', '')
                    if symbol:
                        matches.append(f"{symbol} - {name}")
            return matches[:10]
    except Exception:
        pass
    return []

# ==========================================
# Googleスプレッドシート連携（GAS Web版）
# ==========================================

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
        response = requests.post(webapp_url, json=payload)
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
        response = requests.get(webapp_url)
        if response.status_code == 200:
            data = response.json()
            if isinstance(data, list) and len(data) > 0:
                return pd.DataFrame(data)
        return pd.DataFrame()
    except Exception:
        return pd.DataFrame()