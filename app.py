import streamlit as st
import yfinance as yf
import pandas as pd
import ta
import os
import datetime
import fear_greed
import google.generativeai as genai
import mplfinance as mpf
from PIL import Image
from dotenv import load_dotenv
import feedparser
import urllib.parse
import requests
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
import platform
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.font_manager as fm

# --- ページ設定（スマホ・PC両対応のレスポンシブ配置） ---
st.set_page_config(page_title="AI株式分析ダッシュボード", layout="wide")

# --- フォントのパス判定 ---
if platform.system() == "Windows":
    SYS_FONT_PATH = 'C:/Windows/Fonts/meiryo.ttc'
else:
    SYS_FONT_PATH = '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'

# --- API初期設定 ---
load_dotenv()
api_key = os.getenv("GEMINI_API_KEY")
edinet_key = os.getenv("EDINET_API_KEY")

if not api_key:
    st.error("エラー: GEMINI_API_KEYが読み込めません。")
    st.stop()
genai.configure(api_key=api_key)

# --- セッションステート（対話履歴や分析結果の保持） ---
if "chat_session" not in st.session_state:
    st.session_state.chat_session = None
if "chat_history" not in st.session_state:
    st.session_state.chat_history = []
if "report_text" not in st.session_state:
    st.session_state.report_text = ""

# --- 各種関数（test.pyから流用・一部UI向けに調整） ---
@st.cache_data(ttl=3600) # 1時間はキャッシュしてAPI節約
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

def get_domestic_news(company_name):
    news_list = []
    q1 = urllib.parse.quote(f"{company_name} (アナリスト OR レーティング OR 株探 OR 決算)")
    url1 = f"https://news.google.com/rss/search?q={q1}&hl=ja&gl=JP&ceid=JP:ja"
    try:
        feed = feedparser.parse(url1)
        for entry in feed.entries[:5]:
            title = entry.title.split(' - ')[0]
            news_list.append(title)
    except:
        pass
    return " / ".join(list(dict.fromkeys(news_list))) if news_list else "ニュースなし"

def add_indicators(df):
    df['SMA25'] = ta.trend.sma_indicator(df['Close'], window=25)
    df['SMA75'] = ta.trend.sma_indicator(df['Close'], window=75)
    df['SMA200'] = ta.trend.sma_indicator(df['Close'], window=200)
    bb = ta.volatility.BollingerBands(close=df['Close'], window=20, window_dev=2)
    df['BB_UP'] = bb.bollinger_hband()
    df['BB_LOW'] = bb.bollinger_lband()
    df['RSI'] = ta.momentum.rsi(df['Close'], window=14)
    return df

def generate_chart_image(df_full, title, tail_count, timeframe_type):
    if df_full.empty: return None
    df_plot = df_full.tail(tail_count).copy()
    if df_plot.index.tz is not None: df_plot.index = df_plot.index.tz_convert(None)
    
    aps = []
    if 'SMA25' in df_plot.columns: aps.append(mpf.make_addplot(df_plot['SMA25'], color='blue'))
    if 'SMA75' in df_plot.columns: aps.append(mpf.make_addplot(df_plot['SMA75'], color='orange'))
    if 'BB_UP' in df_plot.columns: aps.append(mpf.make_addplot(df_plot['BB_UP'], color='gray', alpha=0.5))
    if 'BB_LOW' in df_plot.columns: aps.append(mpf.make_addplot(df_plot['BB_LOW'], color='gray', alpha=0.5))
    if 'RSI' in df_plot.columns: aps.append(mpf.make_addplot(df_plot['RSI'], color='purple', panel=2, ylabel='RSI'))

    try:
        meiryo_prop = fm.FontProperties(fname=SYS_FONT_PATH)
        my_style = mpf.make_mpf_style(base_mpf_style='yahoo', rc={'font.family': meiryo_prop.get_name()})
    except:
        my_style = 'yahoo'

    filename = f"temp_{timeframe_type}.png"
    mpf.plot(df_plot, type='candle', style=my_style, addplot=aps, volume=True, panel_ratios=(6,2,2), figratio=(12,8), title=title, savefig=dict(fname=filename, dpi=150, bbox_inches='tight'))
    return filename

# --- UI構築 ---
st.title("📈 AI株式分析ダッシュボード")

# サイドバー入力
with st.sidebar:
    st.header("分析設定")
    stock_code = st.text_input("銘柄コード（4桁）を入力", value="6191")
    analyze_button = st.button("AI分析スタート", type="primary")

# 分析実行ロジック
if analyze_button and stock_code:
    st.session_state.chat_history = [] # 初期化
    st.session_state.report_text = ""
    ticker = f"{stock_code}.T"
    
    with st.spinner('市場データとAIによる分析を取得中...（約30秒〜1分）'):
        # マクロ指標取得
        macro_text = get_macro_data()
        
        # 株価データ取得
        stock = yf.Ticker(ticker)
        name = stock.info.get('longName') or stock.info.get('shortName') or stock_code
        df_w = stock.history(period="max", interval="1wk").ffill()
        df_d = stock.history(period="2y", interval="1d").ffill()
        
        if df_d.empty:
            st.error("株価データが取得できませんでした。コードを確認してください。")
            st.stop()
            
        df_w = add_indicators(df_w)
        df_d = add_indicators(df_d)
        
        # チャート生成
        img_w = generate_chart_image(df_w, f"{name} 週足", 260, 'weekly')
        img_d = generate_chart_image(df_d, f"{name} 日足", 250, 'daily')
        image_payloads = [Image.open(img_w), Image.open(img_d)]
        
        # 基礎データ抽出
        latest_d = df_d.iloc[-1]
        news_str = get_domestic_news(name)
        
        market_data_text = f"""
        【対象銘柄】{name} ({ticker})
        【マクロ環境】{macro_text}
        【最新日足データ】終値: {int(latest_d['Close'])}円 | RSI: {round(latest_d['RSI'],1)}
        【直近ニュース】{news_str}
        """
        
        # AI分析の実行
        system_prompt = f"""
        あなたはプロの投資家チームです。添付した週足・日足の高解像度チャートを視覚的に読み解き、以下のデータも踏まえてスイングトレード目線で分析してください。
        最終的に「今すぐ買うべきか」「見送るべきか」を明確にし、SBI証券などでの注文に使えるメモを提案してください。
        
        対象データ: {market_data_text}
        """
        
        model = genai.GenerativeModel('gemini-3-flash-preview')
        chat = model.start_chat(history=[])
        st.session_state.chat_session = chat
        
        response = chat.send_message([system_prompt] + image_payloads)
        st.session_state.report_text = response.text
        
        # 画像表示
        st.subheader(f"📊 {name} のチャート")
        col1, col2 = st.columns(2)
        with col1: st.image(img_w, use_container_width=True)
        with col2: st.image(img_d, use_container_width=True)

# --- 分析レポートの表示 ---
if st.session_state.report_text:
    st.subheader("📑 AIアナリストチームの分析レポート")
    st.info(st.session_state.report_text)

# --- チャットインターフェース ---
if st.session_state.chat_session:
    st.subheader("💬 ファンドマネージャー（AI）への質問・対話")
    
    # 過去の会話を表示
    for msg in st.session_state.chat_history:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            
    # 入力フォーム
    user_query = st.chat_input("レポートへの反論や追加の質問を入力...")
    if user_query:
        # ユーザーの入力を画面に表示
        with st.chat_message("user"):
            st.markdown(user_query)
        st.session_state.chat_history.append({"role": "user", "content": user_query})
        
        # AIの回答を生成して表示
        with st.chat_message("assistant"):
            with st.spinner("思考中..."):
                res = st.session_state.chat_session.send_message(user_query)
                st.markdown(res.text)
        st.session_state.chat_history.append({"role": "assistant", "content": res.text})