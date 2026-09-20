import streamlit as st
import yfinance as yf
import pandas as pd
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

# --- ページ設定 ---
st.set_page_config(page_title="AI株式分析ダッシュボード", layout="wide")

# --- フォントのパス設定（Streamlit Cloud環境を優先） ---
SYS_FONT_PATH = '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'
if not os.path.exists(SYS_FONT_PATH):
    SYS_FONT_PATH = 'C:/Windows/Fonts/meiryo.ttc' # ローカルテスト用

# --- API初期設定（Streamlit Secretsから取得） ---
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
def get_domestic_news(company_name, edinet_reasons=None):
    news_list = []
    q1 = urllib.parse.quote(f"{company_name} (アナリスト OR レーティング OR 目標株価 OR 株探 OR 四季報 OR 日経 OR 決算 OR 増配)")
    url1 = f"https://news.google.com/rss/search?q={q1}&hl=ja&gl=JP&ceid=JP:ja"
    try:
        feed = feedparser.parse(url1)
        for entry in feed.entries[:8]:
            title = entry.title.split(' - ')[0]
            if company_name in title or any(kw in title for kw in ['決算', '配当', '株', '業績', 'アナリスト', 'レーティング', '目標']):
                news_list.append(title)
    except:
        pass

    if edinet_reasons:
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
        # まずwindows標準の文字コードで読み込みを試す
        try:
            df_code = pd.read_csv('EdinetcodeDlInfo.csv', encoding='cp932', skiprows=1)
        except UnicodeDecodeError:
            # クラウド環境等でUTF-8に変換されている場合はこちらで救済
            df_code = pd.read_csv('EdinetcodeDlInfo.csv', encoding='utf-8', skiprows=1)
            
        target_sec_code = float(stock_code) * 10
        
        # 🌟修正箇所：CSVの証券コード列を強制的に「数値」に変換する（エラーは無視して空欄にする）
        df_code['証券コード'] = pd.to_numeric(df_code['証券コード'], errors='coerce')
        
        # 数値同士になったので、これで確実にヒットします
        match_row = df_code[df_code['証券コード'] == target_sec_code]
        
        if not match_row.empty:
            raw_name = match_row['提出者名'].values[0]
            # 会社名の不要な部分を削ってスッキリさせる
            for rm in ['株式会社', 'ホールディングス', 'グループ本社', 'グループ']:
                raw_name = str(raw_name).replace(rm, '')
            return raw_name.strip()
    except Exception:
        pass
    return None

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
    df['MACD'] = ta.trend.macd_diff(df['Close'])
    return df

def generate_safe_chart_image(df_full, filename, title, tail_count, timeframe_type):
    if df_full.empty: return None
    df_full.index = pd.to_datetime(df_full.index)
    if df_full.index.tz is not None: df_full.index = df_full.index.tz_convert(None)
    
    df_plot = df_full.tail(tail_count).copy()
    if df_plot.empty: return None

    aps = []
    
    if 'SMA25' in df_plot.columns and df_plot['SMA25'].notna().any():
        aps.append(mpf.make_addplot(df_plot['SMA25'], color='blue', width=1.2))
    if 'SMA75' in df_plot.columns and df_plot['SMA75'].notna().any():
        aps.append(mpf.make_addplot(df_plot['SMA75'], color='orange', width=1.2))
    if 'SMA200' in df_plot.columns and df_plot['SMA200'].notna().any():
        aps.append(mpf.make_addplot(df_plot['SMA200'], color='red', width=1.2))
    
    if 'BB_UP' in df_plot.columns and df_plot['BB_UP'].notna().any():
        aps.append(mpf.make_addplot(df_plot['BB_UP'], color='gray', width=0.8, alpha=0.6))
        aps.append(mpf.make_addplot(df_plot['BB_MID'], color='purple', width=0.8, alpha=0.6))
        aps.append(mpf.make_addplot(df_plot['BB_LOW'], color='gray', width=0.8, alpha=0.6))

    panel_ratios = [6]
    panels_count = 0
    
    has_volume = bool('Volume' in df_plot.columns and df_plot['Volume'].notna().any())
    if has_volume:
        panels_count += 1
        panel_ratios.append(2)
        if 'Vol_SMA20' in df_plot.columns and df_plot['Vol_SMA20'].notna().any():
            aps.append(mpf.make_addplot(df_plot['Vol_SMA20'], color='darkgreen', width=1.0, panel=panels_count, ylabel='出来高'))

    has_rsi = bool('RSI' in df_plot.columns and df_plot['RSI'].notna().any())
    if has_rsi:
        panels_count += 1
        panel_ratios.append(2)
        aps.append(mpf.make_addplot(df_plot['RSI'], color='purple', width=1.2, panel=panels_count, ylabel='RSI'))
        df_plot['RSI_70'] = 70
        df_plot['RSI_30'] = 30
        aps.append(mpf.make_addplot(df_plot['RSI_70'], color='gray', linestyle='--', width=0.8, panel=panels_count))
        aps.append(mpf.make_addplot(df_plot['RSI_30'], color='gray', linestyle='--', width=0.8, panel=panels_count))

    try:
        meiryo_prop = fm.FontProperties(fname=SYS_FONT_PATH)
        my_style = mpf.make_mpf_style(base_mpf_style='yahoo', rc={'font.family': meiryo_prop.get_name()})
    except:
        my_style = 'yahoo'

    plot_kwargs = dict(
        type='candle',
        style=my_style,
        volume=has_volume,
        figratio=(12, 8),
        title=title,
        ylabel='価格(円)',
        ylabel_lower='出来高',
        returnfig=True
    )
    if aps:
        plot_kwargs['addplot'] = aps
    
    if len(panel_ratios) > 1:
        plot_kwargs['panel_ratios'] = tuple(panel_ratios)

    fig, axes = mpf.plot(df_plot, **plot_kwargs)
    
    # X軸の年月日を数字2桁に強制書き換え（1970年問題対策版）
    ax_main = axes[0]
    tick_indices = []
    tick_labels = []
    
    if timeframe_type == 'weekly':
        last_month = None
        count = 0
        for i, dt in enumerate(df_plot.index):
            if dt.month != last_month:
                if count % 2 == 0:
                    tick_indices.append(i)
                    tick_labels.append(dt.strftime('%y/%m'))
                count += 1
                last_month = dt.month

    elif timeframe_type == 'daily':
        last_month = None
        for i, dt in enumerate(df_plot.index):
            if dt.month != last_month:
                tick_indices.append(i)
                tick_labels.append(dt.strftime('%y/%m'))
                last_month = dt.month

    elif timeframe_type == 'hourly':
        last_date = None
        days_counted = 0
        last_printed_month = None
        
        for i, dt in enumerate(df_plot.index):
            current_date = dt.date()
            if current_date != last_date:
                if days_counted % 7 == 0:
                    tick_indices.append(i)
                    if dt.month != last_printed_month:
                        tick_labels.append(dt.strftime('%y/%m/%d'))
                        last_printed_month = dt.month
                    else:
                        tick_labels.append(dt.strftime('%d'))
                days_counted += 1
                last_date = current_date

    ax_main.set_xticks(tick_indices)
    ax_main.set_xticklabels(tick_labels, rotation=45)
        
    fig.savefig(filename, dpi=150, bbox_inches='tight')
    plt.close(fig)
    return filename

# ==========================================
# UI構築・メインロジック
# ==========================================
st.title("📈 AI株式分析ダッシュボード")

with st.sidebar:
    st.header("分析設定")
    stock_code = st.text_input("銘柄コード（4桁）を入力", value="6191")
    analyze_button = st.button("AI分析スタート", type="primary")

if analyze_button and stock_code:
    st.session_state.chat_history = [] 
    st.session_state.report_text = ""
    ticker = f"{stock_code}.T"
    
    with st.spinner('市場データとAIによる分析を取得中...（約1〜2分）'):
        macro_text = get_macro_data()
        
        # 🌟専用関数で確実に日本語名を取得する
        jp_name = get_japanese_name(stock_code)
            
        stock = yf.Ticker(ticker)
        yf_name = stock.info.get('longName') or stock.info.get('shortName') or stock_code
        
        # 日本語名があれば採用、なければYahooの英語名
        name = jp_name if jp_name else yf_name
        
        # マルチタイムフレームデータの取得
        df_w = stock.history(period="5y", interval="1wk").ffill()
        df_d = stock.history(period="1y", interval="1d").ffill()
        df_h = stock.history(period="3mo", interval="1h").ffill()
        
        if df_d.empty:
            st.error("株価データが取得できませんでした。コードを確認してください。")
            st.stop()
            
        df_w = add_indicators(df_w)
        df_d = add_indicators(df_d)
        df_h = add_indicators(df_h)
        
        img_w = generate_safe_chart_image(df_w, "temp_weekly.png", f"{name} 週足", 260, 'weekly')
        img_d = generate_safe_chart_image(df_d, "temp_daily.png", f"{name} 日足", 250, 'daily')
        img_h = generate_safe_chart_image(df_h, "temp_hourly.png", f"{name} 1時間足", 500, 'hourly')
        
        image_payloads = []
        if img_w: image_payloads.append(Image.open(img_w))
        if img_d: image_payloads.append(Image.open(img_d))
        if img_h: image_payloads.append(Image.open(img_h))
        
        latest_d = df_d.iloc[-1]
        close_price = int(latest_d['Close'])
        vol_avg20 = df_d['Volume'].tail(20).mean()
        vol_ratio = round(latest_d['Volume'] / vol_avg20, 2) if vol_avg20 > 0 else 1.0

        try:
            info = stock.info
            per = info.get('trailingPE', 'N/A')
            pbr = info.get('priceToBook', 'N/A')
            div_yield_pct = round((info.get('dividendRate', 0) / close_price) * 100, 2) if info.get('dividendRate') else 'N/A'
            market_cap_oku = round(info.get('marketCap', 0) / 100000000, 1) if info.get('marketCap') else 'N/A'
        except:
            per, pbr, div_yield_pct, market_cap_oku = 'N/A', 'N/A', 'N/A', 'N/A'

        edinet_str, edinet_reasons = get_edinet_documents(stock_code, days=60)
        news_str = get_domestic_news(name, edinet_reasons)
        
        market_data_text = (
            f"【対象銘柄詳細データ】\n"
            f"--- 【銘柄: {name} ({ticker})】 ---\n"
            f"[ファンダメンタルズ]\n"
            f"時価総額: 約{market_cap_oku}億円 | PER: {per} | PBR: {pbr} | 配当利回り: {div_yield_pct}%\n"
            f"[直近ニュース・話題・アナリスト動向（重要）]\n{news_str}\n"
            f"[EDINET 公式開示情報（直近2ヶ月）]\n{edinet_str}\n"
            f"[日足（1年相当）最新テクニカル値]\n"
            f"終値: {close_price} 円 | 出来高20日平均比: {vol_ratio}倍\n"
            f"RSI: {round(latest_d['RSI'],1) if pd.notna(latest_d.get('RSI')) else 'N/A'} | MACD: {round(latest_d['MACD'],1) if pd.notna(latest_d.get('MACD')) else 'N/A'}\n\n"
        )
        
        prompt = f"""
あなたは投資家（資金500万円、利益は再投資、年間平均インデックス利益超えを目指す、数日〜数週間のスイングトレード主体、最大4週間の注文期限、指値・逆指値・IFDOCO・OCO活用）のポートフォリオを支えるマルチエージェント分析システムです。

【グローバルマクロ参考値】
{macro_text}

【分析の深さについて】
今回は個別指定モードのため、添付された高解像度チャート画像を視覚的に分析してください。ボリンジャーバンドの収縮・拡散、サブパネルのRSI推移や出来高移動平均線との乖離、マルチタイムフレームでのトレンド整合性を詳細に読み解いてください。

【アナリストの役割分担】
- アナリストA（バリュー投資家）：数字（キャッシュフロー・バランスシート等）を重視し、適正株価を算出。
- アナリストB（過熱感トレーダー）：ボリンジャーバンドのバンドウォークやRSIの過熱感などモメンタムを重視。
- アナリストC（グローバルマクロ）：中央銀行政策や情勢を踏まえ、上下の振れを判定。
- アナリストD（リスクマネージャー）：悲観的視点。最悪シナリオを指摘。
- アナリストE（事業・市場コンセンサス分析）：【最重要】提供された[直近ニュース・アナリスト動向]を読み込み、同業他社比較も行う。
- アナリストF（Z世代の観測者）：将来のメインプレイヤーの価値観に合致するかを重視。
- アナリストG（ニュース・チャート分析）：提供された数値、直近材料、および高解像度チャート画像（ボリンジャーバンド、RSI、出来高平均線含む）から、具体的なチャートパターンを詳細に分析。
- アナリストH（機関投資家動向）：出来高の急増（出来高移動平均線との比較）から大口の売買が行われた可能性があるかを示唆。

【ファンドマネージャーIの最終判断】
- 上記A〜Hの意見を踏まえ、現在「今すぐ買うべきか」「見送るべきか」、全力買いか打診買いかを判断。
- リスクリワードを金額とともに表記。
- HYPER SBI 2に入力するためのメモを一文で作成。先頭には必ず「月日(曜日)」を配置。

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
        st.session_state.chat_session = chat
        
        response = chat.send_message([prompt] + image_payloads)
        st.session_state.report_text = response.text
        
        st.subheader(f"📊 {name} のチャート")
        col1, col2, col3 = st.columns(3)
        if img_w:
            with col1: st.image(img_w, use_container_width=True)
        if img_d:
            with col2: st.image(img_d, use_container_width=True)
        if img_h:
            with col3: st.image(img_h, use_container_width=True)

if st.session_state.report_text:
    st.subheader("📑 AIアナリストチームの分析レポート")
    st.info(st.session_state.report_text)

if st.session_state.chat_session:
    st.subheader("💬 ファンドマネージャー（AI）への質問・対話")
    for msg in st.session_state.chat_history:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            
    if user_query := st.chat_input("レポートへの反論や追加の質問を入力..."):
        with st.chat_message("user"):
            st.markdown(user_query)
        st.session_state.chat_history.append({"role": "user", "content": user_query})
        
        with st.chat_message("assistant"):
            with st.spinner("思考中..."):
                res = st.session_state.chat_session.send_message(user_query)
                st.markdown(res.text)
        st.session_state.chat_history.append({"role": "assistant", "content": res.text})