import streamlit as st
import yfinance as yf
import pandas as pd
import numpy as np
import ta
import datetime
import fear_greed
import google.generativeai as genai
import mplfinance as mpf
from PIL import Image
import feedparser
import urllib.parse
import requests
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
import matplotlib.font_manager as fm
import os
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import tempfile
import matplotlib.dates as mdates
import tempfile
import difflib

# --- ページ設定 ---
st.set_page_config(page_title="AI株式分析ダッシュボード", layout="wide")

# --- フォントのパス設定 ---
SYS_FONT_PATH = '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'
if not os.path.exists(SYS_FONT_PATH):
    SYS_FONT_PATH = 'C:/Windows/Fonts/meiryo.ttc'

# --- API初期設定 ---
try:
    api_key = st.secrets["GEMINI_API_KEY"]
    edinet_key = st.secrets.get("EDINET_API_KEY", "")
    genai.configure(api_key=api_key)
except KeyError:
    st.error("エラー: Streamlit CloudのSecretsに 'GEMINI_API_KEY' が設定されていません。")
    st.stop()

# --- セッションステート ---
if "chat_session" not in st.session_state:
    st.session_state.chat_session = None
if "chat_history" not in st.session_state:
    st.session_state.chat_history = []
if "report_text" not in st.session_state:
    st.session_state.report_text = ""
if "chart_images" not in st.session_state:
    st.session_state.chart_images = None

# ==========================================
# データ取得関数群
# ==========================================
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
    if not edinet_key:
        return "EDINET APIキー未設定", []
    try:
        df_code = pd.read_csv('EdinetcodeDlInfo.csv', encoding='cp932', skiprows=1)
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

        # 🌟修正点：matchesのDataFrameを「証券コード」の数値順（昇順）に並べ替える
        matches['sort_key'] = pd.to_numeric(matches['証券コード'], errors='coerce')
        matches = matches.sort_values('sort_key')

        return [f"{row['証券コード']} - {row['提出者名']}" for _, row in matches.iterrows()]
    except Exception:
        return []
    
@st.cache_data(ttl=3600)
def search_us_ticker_by_name(query):
    if not query: return []
    try:
        # Yahoo!ファイナンスのサジェストAPIを利用して企業名からティッカーを検索
        url = f"https://query2.finance.yahoo.com/v1/finance/search?q={urllib.parse.quote(query)}"
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200:
            data = res.json()
            quotes = data.get('quotes', [])
            matches = []
            for q in quotes:
                # 株式(EQUITY)やETFのみを抽出し、ティッカーと企業名を結合
                if q.get('quoteType') in ['EQUITY', 'ETF']:
                    symbol = q.get('symbol', '')
                    name = q.get('shortname', '')
                    if symbol:
                        matches.append(f"{symbol} - {name}")
            return matches[:10]  # 最大10件まで表示
    except Exception:
        pass
    return []

def add_indicators(df):
    if df.empty: return df
    df['SMA25'] = ta.trend.sma_indicator(df['Close'], window=25)
    df['SMA75'] = ta.trend.sma_indicator(df['Close'], window=75)
    df['SMA200'] = ta.trend.sma_indicator(df['Close'], window=200)
    
    bb = ta.volatility.BollingerBands(close=df['Close'], window=20, window_dev=2)
    df['BB_UP'] = bb.bollinger_hband()
    df['BB_MID'] = bb.bollinger_mavg()
    df['BB_LOW'] = bb.bollinger_lband()

    df['RSI'] = ta.momentum.rsi(df['Close'], window=14)
    df['Vol_SMA20'] = ta.trend.sma_indicator(df['Volume'].astype(float), window=20)
    
    # 🌟修正点：MACDの各種ラインを全データ範囲で一括計算しておく
    df['MACD_Line'] = ta.trend.macd(df['Close'])
    df['MACD_Signal'] = ta.trend.macd_signal(df['Close'])
    df['MACD_Hist'] = ta.trend.macd_diff(df['Close'])
    df['MACD'] = df['MACD_Hist'] # AI分析用の互換性維持
    return df

def generate_safe_chart_image(df_full, filename, title, tail_count, timeframe_type):
    if df_full.empty: return None
    df_full.index = pd.to_datetime(df_full.index)
    if df_full.index.tz is not None: df_full.index = df_full.index.tz_convert(None)
    
    df_plot = df_full.tail(tail_count).copy()
    df_plot = df_plot.dropna(subset=['Open', 'High', 'Low', 'Close'])
    if df_plot.empty: return None

    aps = []
    
    legend_texts = []
    if 'SMA25' in df_plot.columns and df_plot['SMA25'].notna().any():
        aps.append(mpf.make_addplot(df_plot['SMA25'], color='blue', width=1.2))
        legend_texts.append("SMA25(青)")
    if 'SMA75' in df_plot.columns and df_plot['SMA75'].notna().any():
        aps.append(mpf.make_addplot(df_plot['SMA75'], color='orange', width=1.2))
        legend_texts.append("SMA75(橙)")
    if 'SMA200' in df_plot.columns and df_plot['SMA200'].notna().any():
        aps.append(mpf.make_addplot(df_plot['SMA200'], color='red', width=1.2))
        legend_texts.append("SMA200(赤)")
        
    if legend_texts:
        title = f"{title}   [{' / '.join(legend_texts)}]"
    
    if 'BB_UP' in df_plot.columns and df_plot['BB_UP'].notna().any():
        aps.append(mpf.make_addplot(df_plot['BB_UP'], color='gray', width=0.8, alpha=0.6))
        aps.append(mpf.make_addplot(df_plot['BB_MID'], color='purple', width=0.8, alpha=0.6))
        aps.append(mpf.make_addplot(df_plot['BB_LOW'], color='gray', width=0.8, alpha=0.6))

    panel_ratios = [6]
    panels_count = 0
    
    has_volume = bool('Volume' in df_plot.columns and df_plot['Volume'].notna().any())
    if has_volume:
        panels_count += 1
        panel_ratios.append(1.5)
        if 'Vol_SMA20' in df_plot.columns and df_plot['Vol_SMA20'].notna().any():
            aps.append(mpf.make_addplot(df_plot['Vol_SMA20'], color='darkgreen', width=1.0, panel=panels_count))

    has_rsi = bool('RSI' in df_plot.columns and df_plot['RSI'].notna().any())
    if has_rsi:
        panels_count += 1
        panel_ratios.append(1.5)
        aps.append(mpf.make_addplot(df_plot['RSI'], color='purple', width=1.2, panel=panels_count))
        df_plot['RSI_70'] = 70
        df_plot['RSI_30'] = 30
        aps.append(mpf.make_addplot(df_plot['RSI_70'], color='gray', linestyle='--', width=0.8, panel=panels_count))
        aps.append(mpf.make_addplot(df_plot['RSI_30'], color='gray', linestyle='--', width=0.8, panel=panels_count))

    has_macd = bool('MACD_Line' in df_plot.columns and df_plot['MACD_Line'].notna().any())
    if has_macd:
        panels_count += 1
        panel_ratios.append(1.5)
        
        colors = ['green' if val >= 0 else 'red' for val in df_plot['MACD_Hist']]
        aps.append(mpf.make_addplot(df_plot['MACD_Hist'], type='bar', color=colors, panel=panels_count, alpha=0.5))
        aps.append(mpf.make_addplot(df_plot['MACD_Line'], color='blue', width=1.0, panel=panels_count))
        aps.append(mpf.make_addplot(df_plot['MACD_Signal'], color='orange', width=1.0, panel=panels_count))

    try:
        meiryo_prop = fm.FontProperties(fname=SYS_FONT_PATH)
        my_style = mpf.make_mpf_style(base_mpf_style='yahoo', rc={'font.family': meiryo_prop.get_name()})
    except:
        my_style = 'yahoo'

    plot_kwargs = dict(
        type='candle',
        style=my_style,
        volume=has_volume,
        figratio=(12, 10),
        title=title,
        ylabel='Price',
        returnfig=True
    )
    if aps:
        plot_kwargs['addplot'] = aps
    
    try:
        if len(panel_ratios) > 1:
            plot_kwargs['panel_ratios'] = tuple(panel_ratios)
    
        fig, axes = mpf.plot(df_plot, **plot_kwargs)
        ax_main = axes[0]
        
        from matplotlib.ticker import ScalarFormatter
        for ax in fig.axes:
            formatter = ScalarFormatter(useOffset=False, useMathText=False)
            formatter.set_scientific(False)
            ax.yaxis.set_major_formatter(formatter)
            ax.yaxis.offsetText.set_visible(False)
            
        for ax in axes:
            ax.spines['top'].set_visible(True)
            ax.spines['bottom'].set_visible(True)
            ax.spines['left'].set_visible(True)
            ax.spines['right'].set_visible(True)
            ax.spines['top'].set_linewidth(1.5)
            ax.spines['bottom'].set_linewidth(1.5)
            ax.spines['left'].set_linewidth(1.0)
            ax.spines['right'].set_linewidth(1.0)
            ax.spines['top'].set_color('black')
            ax.spines['bottom'].set_color('black')
            ax.spines['left'].set_color('black')
            ax.spines['right'].set_color('black')
            
        last_close = df_plot['Close'].iloc[-1]
        price_str = f"{int(last_close)}" if last_close > 100 else f"{last_close:.2f}"
        
        ax_main.text(1.0, last_close, f' {price_str} ', color='white', 
                     backgroundcolor='black', verticalalignment='center', horizontalalignment='left',
                     transform=ax_main.get_yaxis_transform(), fontsize=9, fontweight='bold', zorder=10)
                     
        # 🌟修正点：get_geometryを廃止し、mpf.plotが返す配列(axes)から直接パネルを特定する安全な方法に変更
        current_panel_idx = 2  # axes[0]とaxes[1]はメインチャート用
        
        if has_volume and current_panel_idx < len(axes):
            ax_vol = axes[current_panel_idx]
            ax_vol.text(0.01, 0.85, 'Volume', transform=ax_vol.transAxes, fontsize=10, fontweight='bold', color='black', alpha=0.7)
            current_panel_idx += 2
            
        if has_rsi and current_panel_idx < len(axes):
            ax_rsi = axes[current_panel_idx]
            ax_rsi.text(0.01, 0.85, 'RSI', transform=ax_rsi.transAxes, fontsize=10, fontweight='bold', color='black', alpha=0.7)
            current_panel_idx += 2
            
        if has_macd and current_panel_idx < len(axes):
            ax_macd = axes[current_panel_idx]
            ax_macd.text(0.01, 0.85, 'MACD', transform=ax_macd.transAxes, fontsize=10, fontweight='bold', color='black', alpha=0.7)
            current_panel_idx += 2

        window_size = 10 if timeframe_type == 'weekly' else (8 if timeframe_type == 'daily' else 20)
        highs = []
        lows = []
        for i in range(window_size, len(df_plot) - window_size):
            is_high = True
            is_low = True
            for j in range(i - window_size, i + window_size + 1):
                if i != j:
                    if df_plot['High'].iloc[i] <= df_plot['High'].iloc[j]:
                        is_high = False
                    if df_plot['Low'].iloc[i] >= df_plot['Low'].iloc[j]:
                        is_low = False
            if is_high: highs.append((i, df_plot['High'].iloc[i]))
            if is_low: lows.append((i, df_plot['Low'].iloc[i]))
            
        for idx, val in highs:
            val_str = f"{int(val)}" if val > 100 else f"{val:.1f}"
            ax_main.text(idx, val + (val*0.015), val_str, ha='center', va='bottom', color='green', fontsize=8, fontweight='bold')
        for idx, val in lows:
            val_str = f"{int(val)}" if val > 100 else f"{val:.1f}"
            ax_main.text(idx, val - (val*0.015), val_str, ha='center', va='top', color='red', fontsize=8, fontweight='bold')

        tick_indices = []
        tick_labels = []
        
        if timeframe_type == 'weekly':
            last_month = None
            temp_indices = []
            temp_labels = []
            for i, dt in enumerate(df_plot.index):
                if dt.month != last_month:
                    temp_indices.append(i)
                    temp_labels.append(dt)
                    last_month = dt.month
            
            filtered_indices = []
            filtered_labels = []
            for j in range(len(temp_indices)):
                if j % 3 == 0:
                    filtered_indices.append(temp_indices[j])
                    filtered_labels.append(temp_labels[j])
                    
            seen_years = set()
            for k in range(len(filtered_labels)):
                dt = filtered_labels[k]
                is_last = (k == len(filtered_labels) - 1)
                is_first_of_year = dt.year not in seen_years
                
                if is_last or is_first_of_year:
                    tick_labels.append(dt.strftime('%y/%m'))
                    seen_years.add(dt.year)
                else:
                    tick_labels.append(dt.strftime('%m'))
            tick_indices = filtered_indices
    
        elif timeframe_type == 'daily':
            last_month = None
            for i, dt in enumerate(df_plot.index):
                if dt.month != last_month:
                    tick_indices.append(i)
                    tick_labels.append(dt.strftime('%y/%m'))
                    last_month = dt.month
    
        elif timeframe_type == 'hourly':
            last_date_printed = None
            last_month_printed = -1
            for i, dt in enumerate(df_plot.index):
                current_date = dt.date()
                if current_date != last_date_printed:
                    if not tick_indices or (i - tick_indices[-1]) >= 4:
                        tick_indices.append(i)
                        if dt.month != last_month_printed:
                            tick_labels.append(dt.strftime('%m/%d'))
                            last_month_printed = dt.month
                        else:
                            tick_labels.append(dt.strftime('%d'))
                        last_date_printed = current_date
    
        ax_main.set_xticks(tick_indices)
        ax_main.set_xticklabels(tick_labels, rotation=45)
            
        fig.savefig(filename, dpi=300, bbox_inches='tight')
        plt.close(fig)
        return filename
    except Exception:
        return None

# ==========================================
# UI構築・メインロジック
# ==========================================
st.title("📈 AI株式分析ダッシュボード")

with st.sidebar:
    st.header("分析設定")
    # プレースホルダーの文字を少し変更
    raw_input = st.text_input("銘柄コード または 企業名（日米対応・一部でも可）", value="6844")
    
    stock_code = raw_input
    is_jp = False
    
    # 入力内容の自動判別ロジック
    if re.match(r'^\d{4}[A-Za-z]?$', raw_input):
        # 4桁数字（日本株コード）なら直接確定
        stock_code = raw_input
        is_jp = True
    else:
        # 日本株と米国株の企業名検索を同時に走らせる
        jp_matches = search_japanese_code_by_name(raw_input)
        
        us_matches = []
        # 入力にアルファベットが含まれている場合のみ、米国株（Yahoo API）検索も実行
        if re.search(r'[A-Za-z]', raw_input):
            us_matches = search_us_ticker_by_name(raw_input)
            
        all_matches = jp_matches + us_matches
        
        if all_matches:
            # 検索で候補が見つかった場合はプルダウンを表示
            selected = st.selectbox("複数の候補が見つかりました。対象を選択してください:", all_matches)
            stock_code = selected.split(" - ")[0]
            # 選ばれたコードが数字で始まれば日本株、アルファベットなら米国株と判定
            is_jp = bool(re.match(r'^\d{4}', stock_code))
        else:
            # 検索にヒットしないが、アルファベットのみの場合は直接ティッカーとして扱う（最後の砦）
            if re.match(r'^[A-Za-z]+$', raw_input):
                stock_code = raw_input.upper()
                is_jp = False
            else:
                st.warning("該当する銘柄が見つかりませんでした。")
                stock_code = ""
    
    st.markdown("---")
    st.subheader("🔗 調査サイトへ一発アクセス")
    
    if is_jp and stock_code:
        yahoo_url = f"https://finance.yahoo.co.jp/quote/{stock_code}.T"
        kabutan_disclose_url = f"https://kabutan.jp/stock/news?code={stock_code}&b=k"
        kabutan_finance_url = f"https://kabutan.jp/stock/finance?code={stock_code}"
        tv_url = f"https://jp.tradingview.com/chart/?symbol=TSE%3A{stock_code}"
        
        st.markdown(f"""
        * [Yahoo!ファイナンス（四季報・信用残）]({yahoo_url})
        * [株探（適時開示・IR速報）]({kabutan_disclose_url})
        * [株探（財務・業績推移）]({kabutan_finance_url})
        * [TradingView（詳細チャート）]({tv_url})
        * [SBI証券（メインサイト）](https://www.sbisec.co.jp/)
        """)
        st.caption("※四季報概況や信用残はYahoo!ファイナンス、公式IR速報は株探（適時開示）から素早く確認できます。")
    elif not is_jp and stock_code:
        tv_url = f"https://jp.tradingview.com/chart/?symbol={stock_code.upper()}"
        yh_url = f"https://finance.yahoo.com/quote/{stock_code.upper()}"
        st.markdown(f"""
        * [TradingView（チャート分析）]({tv_url})
        * [Yahoo! Finance (US)]({yh_url})
        """)
        st.caption("※米国株はyfinanceでデータを完全取得できるため、手動の資料追加は必須ではありません。")

    st.markdown("---")
    st.subheader("📁 追加資料（ドラッグ＆ドロップ）")
    st.caption("PDFレポートや、画面切り取り（Win+Shift+S → ここをクリックしてCtrl+V）で画像を直接AIに渡せます。")
    uploaded_files = st.file_uploader("ファイルをここにドロップ", accept_multiple_files=True, type=['png', 'jpg', 'jpeg', 'pdf'])
    
    st.markdown("---")
    # stock_codeが空（検索失敗時）はボタンを無効化する安全処理
    analyze_button = st.button("AI分析スタート", type="primary", disabled=not bool(stock_code))

if analyze_button and stock_code:
    st.session_state.chat_history = [] 
    st.session_state.report_text = ""
    st.session_state.chart_images = None
    ticker = f"{stock_code}.T" if is_jp else stock_code.upper()
    
    with st.spinner('市場データとAIによる分析を取得中...（約1〜2分）'):
        macro_text = get_macro_data()
        
        stock = yf.Ticker(ticker)
        if is_jp:
            jp_name = get_japanese_name(stock_code)
            name = jp_name if jp_name else stock.info.get('longName', stock_code)
        else:
            name = stock.info.get('longName', stock.info.get('shortName', stock_code))
        
        df_w = stock.history(period="10y", interval="1wk").ffill().bfill()
        df_d = stock.history(period="2y", interval="1d").ffill().bfill()
        df_h = stock.history(period="1y", interval="1h").ffill().bfill()
        
        if df_d.empty:
            st.error("株価データが取得できませんでした。ティッカーコードを確認してください。")
            st.stop()
            
        df_w = add_indicators(df_w)
        df_d = add_indicators(df_d)
        df_h = add_indicators(df_h)
        
        img_w = generate_safe_chart_image(df_w, "temp_weekly.png", f"{name} Weekly", 260, 'weekly')
        img_d = generate_safe_chart_image(df_d, "temp_daily.png", f"{name} Daily", 130, 'daily')
        img_h = generate_safe_chart_image(df_h, "temp_hourly.png", f"{name} Hourly", 130, 'hourly')
        
        st.session_state.chart_images = {
            "name": name,
            "w": img_w,
            "d": img_d,
            "h": img_h
        }
        
        image_payloads = []
        if img_w: image_payloads.append(Image.open(img_w))
        if img_d: image_payloads.append(Image.open(img_d))
        if img_h: image_payloads.append(Image.open(img_h))
        
        doc_payloads = []
        if uploaded_files:
            for f in uploaded_files:
                if f.name.lower().endswith('.pdf'):
                    doc_payloads.append({
                        "mime_type": "application/pdf",
                        "data": f.getvalue()
                    })
                else:
                    doc_payloads.append(Image.open(f))

        try:
            local_files = [f for f in os.listdir('.') if stock_code.upper() in f.upper() and f.lower().endswith(('.pdf', '.png', '.jpg', '.jpeg'))]
            for file_name in local_files:
                if file_name.lower().endswith('.pdf'):
                    with open(file_name, "rb") as pdf_file:
                        doc_payloads.append({
                            "mime_type": "application/pdf",
                            "data": pdf_file.read()
                        })
                else:
                    doc_payloads.append(Image.open(file_name))
        except:
            pass

        latest_d = df_d.iloc[-1]
        close_price = round(latest_d['Close'], 2) if not is_jp else int(latest_d['Close'])
        vol_avg20 = df_d['Volume'].tail(20).mean()
        vol_ratio = round(latest_d['Volume'] / vol_avg20, 2) if vol_avg20 > 0 else 1.0

        try:
            info = stock.info
            if is_jp:
                fund_data = get_japanese_fundamentals(stock_code)
                per = fund_data['per'] if fund_data['per'] != 'N/A' else info.get('trailingPE', 'N/A')
                pbr = fund_data['pbr'] if fund_data['pbr'] != 'N/A' else info.get('priceToBook', 'N/A')
                div_yield_pct = fund_data['div'] if fund_data['div'] != 'N/A' else (round((info.get('dividendRate', 0) / close_price) * 100, 2) if info.get('dividendRate') else 'N/A')
                margin_ratio = fund_data['margin']
                market_cap_str = f"約{round(info.get('marketCap', 0) / 100000000, 1)}億円" if info.get('marketCap') else 'N/A'
                margin_str = f" | 信用倍率: {margin_ratio}倍" if margin_ratio != 'N/A' else ""
            else:
                per = round(info.get('trailingPE', 0), 2) if info.get('trailingPE') else 'N/A'
                pbr = round(info.get('priceToBook', 0), 2) if info.get('priceToBook') else 'N/A'
                if info.get('dividendRate') and close_price > 0:
                    div_yield_pct = round((info.get('dividendRate', 0) / close_price) * 100, 2)
                else:
                    div_yield_pct = 'N/A'
                if info.get('marketCap'):
                    market_cap_str = f"約{round(info.get('marketCap', 0) / 1000000000, 2)} Billion USD"
                else:
                    market_cap_str = 'N/A'
                margin_str = ""
        except:
            per, pbr, div_yield_pct, market_cap_str, margin_str = 'N/A', 'N/A', 'N/A', 'N/A', ""

        edinet_str = ""
        news_str = ""
        if is_jp:
            edinet_str, edinet_reasons = get_edinet_documents(stock_code, days=60)
            news_str = get_news(name, True, edinet_reasons)
            edinet_section = f"[EDINET 公式開示情報（直近2ヶ月）]\n{edinet_str}\n"
        else:
            news_str = get_news(name, False)
            edinet_section = ""

        market_data_text = (
            f"【対象銘柄詳細データ】\n"
            f"--- 【銘柄: {name} ({ticker})】 ---\n"
            f"[基本ファンダメンタルズ（※追加資料がある場合は資料内の数値を最優先すること）]\n"
            f"時価総額: {market_cap_str} | PER: {per} | PBR: {pbr} | 配当利回り: {div_yield_pct}%{margin_str}\n"
            f"[直近ニュース・話題・アナリスト動向（重要）]\n{news_str}\n"
            f"{edinet_section}"
            f"[日足（1年相当）最新テクニカル値]\n"
            f"終値: {close_price} {'円' if is_jp else 'ドル'} | 出来高20日平均比: {vol_ratio}倍\n"
            f"RSI: {round(latest_d['RSI'],1) if pd.notna(latest_d.get('RSI')) else 'N/A'} | MACD: {round(latest_d['MACD'],1) if pd.notna(latest_d.get('MACD')) else 'N/A'}\n\n"
        )
        
        prompt = f"""
あなたはプロの投資家チームです。以下の提供データおよびチャート画像を基に、極めて詳細で深掘りした多角的な銘柄分析レポートを作成してください。

【前提とする投資戦略】
私は投資を始めたばかりで、資金は500万円、利益はそのまま株取引資金として運用します。
NISAには毎月積み立て投資でオルカンを4万円、S&P500を3万円、TOPIXを3万円で合計10万円。成長投資枠は数年後移行の発展が見込めるような長期保有予定銘柄が見つかれば購入を予定しています。
現在は株取引そのものの知識と経験の増加を主軸に据え、ある程度の損は許容しますが、インデックス投資での年間平均利益を超える運用を目指します。

判断基準として、数日から数週間でのスイングトレードを中心とし、長くて半年程度での売買を検討します。
伸びていくと判断される銘柄は5～10年の長期保有を検討します。その場合はNISAの成長投資枠をできるだけ活用します。
売買は基本的に指値・逆指値での注文とし、IFDOCO注文やOCO注文も駆使します。リアルタイムでの売買は分析を行ったタイミング以外ではスケジュール上難しいと判断してください。
また、注文可能な期間は最大4週間とし、買いと売りは別の期間で指定が可能。いつまでに買えなければ見送りか、OCOはどこまで追いかけるかも検討してください。保持しておくべきであれば注文そのものを行わないものとします。
長期的に保有を検討する銘柄の場合に限り、最下限で購入したい価格については株価アラームをセットするものとします。
メモに使用する注文期間の表示は特定の日付を表示（月日と曜日のみ）し、文の最初に持ってきてください。

【トレード目標価格の厳格な設定基準】
スイングトレード（数日～数週間での決済）における「利益確定の売り指値」は、後述のアナリストGが提示する【①直近（数週間スパン）の上値抵抗線】を基準とし、その壁を越えない極めて現実的に到達可能な価格に設定してください。中長期的な高値を目標にしたり、目の前にある厚い抵抗帯を容易に上抜けるという希望的観測に基づいた非現実的な高値設定は厳禁です。

【特に注視すべきテーマ】
現在進行中の「TOPIX再編」に関するニュースや該当する可能性（流通株式時価総額の基準未達による段階的ウエイト低減、除外リスク、または新規組み入れの思惑など）があれば、機関投資家の需給変動（パッシブファンドの売り買い）に直結するカタリストとして最優先で分析に組み込んでください。

【グローバルマクロ参考値】
{macro_text}

【分析の深さについて】
今回は個別指定モードのため、添付された高解像度チャート画像を視覚的に分析してください。ボリンジャーバンドの収縮・拡散、サブパネルのRSI推移や出来高移動平均線との乖離、マルチタイムフレームでのトレンド整合性を詳細に読み解いてください。
さらに、四季報、個別銘柄詳細レポート(PDF)、ニュース等の追加資料が添付されている場合は、その内容（PER/PBR等の指標、業績推移、アナリスト評価など）を最優先で抽出し、各アナリストの分析（特にアナリストAの厳格なバリュー評価）の根拠としてフル活用してください。

【アナリストの役割分担】
アナリストA、あなたはウォーレンバフェットのような厳格なバリュー投資家です。定性的な夢物語は無視し、キャッシュフローとバランスシートの数字だけを信じてください。また、発表やニュース、目標株価等から適正株価を算出します。
アナリストB、あなたは市場の過熱感を察知するトレーダーです。ファンダメンタルズが良くても市場の関心がなければ価値無しと判断します。個別材料（決算や提携等）への市場の反応速度やモメンタムを重視してください。
アナリストC、あなたはマクロ・政治経済ストラテジストです。中央銀行の金利政策、選挙を含む政治イベント、地政学リスク、マクロ経済ニュースが市場全体および該当セクターに与える影響を分析し、現在値がマクロ要因で上下どちらに振れているかを判定します。
アナリストD、あなたは悲観的なリスクマネージャーです。他のエージェントの強気な意見に対し、最悪のシナリオ（ブラックスワン）を突き付け、論理的に反論してください。
アナリストE、あなたは世論・競合分析者です。ネット上の掲示板やSNSでの「製品やサービスの価値」を重要視します。提供されたニュースから同業他社との優位性や一時的なブームかを判断します。
アナリストF、あなたはZ世代の観測者です。将来的に社会のメインプレイヤーとなる世代の価値観から、中長期的に普遍化する事業かを評価します。
アナリストG、あなたは純粋テクニカルアナリストです。ニュースやファンダメンタルズなどの定性情報は一切無視し、チャート画像のみから客観的に判断してください。
 ① スイングトレードの目安となる「直近（数週間スパン）の目先の抵抗線・支持線」の具体的数値を算出
 ② 中長期的なトレンドの目安となる「より長いスパン（数ヶ月～半年）の抵抗線・支持線」の具体的数値を算出
 ③ 直近1週間および3ヶ月スパンでのチャートパターン（該当なければ「チャートパターンなし」）
 ④ Fear & Greed IndexおよびVIXから客観的な市場心理（欲望か恐怖か）を提示
アナリストH、あなたは機関投資家分析者です。大量保有報告書やForm 13Fの動向、チャート出来高推移、および「TOPIX再編」に伴うパッシブファンドの機械的な売買リスクを厳密に判定してください。

【出力形式】
各アナリストは詳細な考察を述べた後、末尾に必ず以下のフォーマットで1行要約を出力してください。
【アナリスト判定】判断: [買い / 打診買い / 見送り / 売り] | 最重要指標・数値: [具体的な数値やキーワード]

その後、ファンドマネージャーのIが最終判断を行います。
各アナリストの1行要約とアナリストGが算出した支持線・抵抗線数値を基に、以下の項目を簡潔かつ論理的に決定してください。
1. 売買判断（買い・見送り・売り）および推奨エントリー期間
2. 具体的なIFDOCO/OCO注文価格（Gの提示した直近抵抗線を超えない現実的な売り指値、支持線を考慮した買い指値・損切り逆指値）
3. 資金配分（投入ロット数）とリスクリワード比（想定利益額と想定損失額）
4. HYPER SBI 2入力用メモ（文頭に「MM/DD(曜日)」の形式で注文期限を記載した1文）
【出力時の厳守ルール】
・過度な肯定や装飾は不要です。
・確信が持てない情報は（推測）と追記してください。
・重要な主張は根拠を提示し、情報を推測して補完しないでください。
・各情報に「（出典：〇〇）」の形式で情報源を明示してください。
・2023年以降に変化している可能性が高い情報には（要最新確認）を付けてください。

対象データ：
{market_data_text}
"""
        model = genai.GenerativeModel('gemini-3-flash-preview') 
        chat = model.start_chat(history=[])
        
        response = chat.send_message([prompt] + image_payloads + doc_payloads)
        st.session_state.report_text = response.text

if st.session_state.chart_images:
    charts = st.session_state.chart_images
    st.subheader(f"📊 {charts['name']} のチャート")
    col1, col2, col3 = st.columns(3)
    if charts['w']:
        with col1: st.image(charts['w'], use_container_width=True)
    if charts['d']:
        with col2: st.image(charts['d'], use_container_width=True)
    if charts['h']:
        with col3: st.image(charts['h'], use_container_width=True)

if st.session_state.report_text:
    st.subheader("📑 AIアナリストチームの分析レポート")
    st.info(st.session_state.report_text)

if st.session_state.chat_session:
    st.subheader("💬 ファンドマネージャー（AI）への質問・対話")
    
    for msg in st.session_state.chat_history:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            
    with st.form("chat_form", clear_on_submit=True):
        st.markdown("**追加の質問や、スクショ画像の貼り付け（枠内をクリックしてCtrl+V）はこちら**")
        user_query = st.text_area("テキストを入力", height=100)
        chat_images = st.file_uploader("追加画像をドロップまたはペースト", type=['png', 'jpg', 'jpeg'], accept_multiple_files=True)
        submit_button = st.form_submit_button("送信")

    if submit_button and (user_query or chat_images):
        with st.chat_message("user"):
            st.markdown(user_query)
            if chat_images:
                for img in chat_images:
                    st.image(img, width=300)
        
        content_for_history = user_query
        if chat_images:
            content_for_history += f"\n（※画像 {len(chat_images)}枚を送信しました）"
            
        st.session_state.chat_history.append({"role": "user", "content": content_for_history})
        
        payload = []
        if user_query:
            payload.append(user_query)
        if chat_images:
            for img_file in chat_images:
                payload.append(Image.open(img_file))
        
        with st.chat_message("assistant"):
            with st.spinner("思考中..."):
                res = st.session_state.chat_session.send_message(payload if len(payload) > 1 else payload[0])
                st.markdown(res.text)
        st.session_state.chat_history.append({"role": "assistant", "content": res.text})
        
        st.rerun()