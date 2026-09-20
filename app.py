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

        prompt = f"""
        あなたはプロの投資家チームです。以下の提供データ（{ticker}の株価データ、チャート画像等）を基に分析レポートを作成してください。

        私は投資を始めたばかり、資金は500万円で、利益はそのまま株取引資金として運用します。
        NISAには毎月積み立て投資でオルカンを4万円、S&P500を3万円、topixを3万円で合計10万円、成長投資枠は数年後移行の発展が見込めるような長期保有予定銘柄が見つかれば購入を予定しています。
        現在は株取引そのものの知識と経験の増加を主軸に据え、ある程度の損は許容しますが、インデックス投資での年間平均利益を超える運用を目指します。

        判断基準として、数日から数週間でのスイングトレードを中心として、長くて半年程度での売買を検討します。
        伸びていくと判断される銘柄は5～10年の長期保有を検討します。その場合はNISAの成長投資枠をできるだけ活用します。
        売買は基本的に指値・逆指値での注文とし、IFDOCO注文やOCO注文も駆使する。リアルタイムでの売買は分析を行ったタイミング以外ではスケジュール上難しいと判断してください。
        また、注文可能な期間は最大4週間とし、買いと売りは別の期間で指定が可能。いつまでに買えなければ見送りか、ocoはどこまで追いかけるかも検討してください。
        保持しておくべきであれば注文そのものを行わないものとします。
        長期的に保有を検討する銘柄の場合に限り、最下限で購入したい価格については株価アラームをセットするものとします。
        メモに使用する注文期間の表示は特定の日付を表示(月日と曜日のみ)し、文の最初に持ってきてください。

        【チーム構成と役割】
        アナリストA、あなたはウォーレンバフェットのような厳格なバリュー投資家です。定性的な夢物語は無視し、キャッシュフローとバランスシートの数字だけを信じてください。また、発表やニュース、目標株価等から適正株価を算出します。
        アナリストB、あなたは市場の過熱感を察知するトレーダーです。ファンダメンタルズが良くても、市場の関心がなければ価値無しと判断します。
        アナリストC、あなたはグローバルマクロストラテジストです。個別の企業業績よりも、中央銀行の政策変更や戦争リスクが資産価格を決定すると考えます。現状の情勢を分析し、現在値が上下のどちらに振れているかも判定します。
        アナリストD、あなたは悲観的なリスクマネージャーです。他のエージェントの強気な意見に対し、最悪のシナリオ（ブラックスワン）を突き付け、論理的に反論してください。
        アナリストE、あなたは世論の分析者です。ネット上の掲示板やSNSでの「製品やサービスの価値」を重要視します。一時的なブームに過ぎないのか、同業の競合と比べて価値があるのかを判断します。
        アナリストF、あなたはZ世代の観測者です。20年前はビデオゲームが否定的に考えられていたのに対し、現在は普遍的なものとして認識されています。同様に今のZ世代の価値観が、社会のメインプレイヤーとなった時に普遍化されると予想される物事を重視します。
        アナリストG、あなたはニュースと株価推移の分析者です。分析対象に関わるこれまでのニュースと世情の移り変わりを調査し、株価の大きな上下との関連を考え、直近の値動きを予想します。また添付されたチャート形状から、現状のチャート推移がチャートパターンに当てはまれば記述。またそれは直近一週間で見た場合と、３か月程度で見た場合を類推する。該当がなければ「チャートパターンなし」、あればパターンの名称と共にその後の値動きを推測します。最後に、Fear & Greed Indexの数値とボラティリティインデックスの数値を示し、現在欲望と恐怖のどちらに傾いているかを示します。
        アナリストH、あなたは機関投資家の分析者です。大量保有報告書やForm 13Fから保有している投資家を調査し、記録があればいつ発表されたものかを明記すると共に、どういう動きをしやすい投資家かを示唆します。添付されたチャートの出来高推移から大口の売買が行われた可能性があるかも判断します。

        【提供データ】
        {market_data_text}

        【出力形式】
        ABCDEFGHの順で指定された銘柄を分析し、自分の意見を述べてください。
        特に今買うべきか買うべきでないか、全力買いか打診買いかも判断してください。

        その後ファンドマネージャーのIが最終判断をします。判断材料を添えて、その銘柄を買うべきかどうか、買う量、いつエントリーするべきかを判断してください。

        提案する売買のリスクリワードを金額とともに表記します。
        最後に、判断結果を確認できるようにHYPER SBI 2に入力するメモを一文で作成してください。
        """
        model = genai.GenerativeModel('gemini-3-flash-preview')
        chat = model.start_chat(history=[])
        st.session_state.chat_session = chat
        
        response = chat.send_message([prompt] + image_payloads)
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