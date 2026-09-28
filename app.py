import streamlit as st
import yfinance as yf
import pandas as pd
import google.generativeai as genai
from PIL import Image
import os
import re
from concurrent.futures import ThreadPoolExecutor

# 保存用関数は一旦除外
from data_fetcher import (
    get_macro_data, get_edinet_documents, get_news,
    get_japanese_name, get_us_stock_name, get_japanese_fundamentals,
    search_japanese_code_by_name, search_us_ticker_by_name
)
from chart_maker import add_indicators, generate_safe_chart_image
from ai_agent import (
    generate_research_prompt, generate_bull_prompt, 
    generate_bear_prompt, generate_manager_prompt
)

# --- ページ設定 ---
st.set_page_config(page_title="AI株式分析ダッシュボード", layout="wide")

# --- API初期設定 ---
try:
    api_key = st.secrets["GEMINI_API_KEY"]
    genai.configure(api_key=api_key)
except KeyError:
    st.error("エラー: Streamlit CloudのSecretsに 'GEMINI_API_KEY' が設定されていません。")
    st.stop()

# --- セッションステート ---
if "chat_session" not in st.session_state:
    st.session_state.chat_session = None
if "chat_history" not in st.session_state:
    st.session_state.chat_history = []
if "chart_images" not in st.session_state:
    st.session_state.chart_images = None

# 各エージェントのレポート保存用
if "research_report" not in st.session_state:
    st.session_state.research_report = ""
if "bull_report" not in st.session_state:
    st.session_state.bull_report = ""
if "bear_report" not in st.session_state:
    st.session_state.bear_report = ""
if "manager_report" not in st.session_state:
    st.session_state.manager_report = ""

st.title("📈 AI株式分析ダッシュボード")

with st.sidebar:
    st.header("分析設定")
    raw_input = st.text_input("銘柄コード または 企業名（日米対応・一部でも可）", value="6844")
    
    stock_code = raw_input
    is_jp = False
    
    if re.match(r'^\d{4}[A-Za-z]?$', raw_input):
        stock_code = raw_input
        is_jp = True
    else:
        jp_matches = search_japanese_code_by_name(raw_input)
        us_matches = []
        if re.search(r'[A-Za-z]', raw_input):
            us_matches = search_us_ticker_by_name(raw_input)
            
        all_matches = jp_matches + us_matches
        
        if all_matches:
            selected = st.selectbox("複数の候補が見つかりました。対象を選択してください:", all_matches)
            stock_code = selected.split(" - ")[0]
            is_jp = bool(re.match(r'^\d{4}', stock_code))
        else:
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
    elif not is_jp and stock_code:
        tv_url = f"https://jp.tradingview.com/chart/?symbol={stock_code.upper()}"
        yh_url = f"https://finance.yahoo.com/quote/{stock_code.upper()}"
        st.markdown(f"""
        * [TradingView（チャート分析）]({tv_url})
        * [Yahoo! Finance (US)]({yh_url})
        """)

    st.markdown("---")
    st.subheader("📁 追加資料（ドラッグ＆ドロップ）")
    uploaded_files = st.file_uploader("ファイルをここにドロップ", accept_multiple_files=True, type=['png', 'jpg', 'jpeg', 'pdf'])
    
    st.markdown("---")
    analyze_button = st.button("マルチエージェント分析スタート", type="primary", disabled=not bool(stock_code))

if analyze_button and stock_code:
    # 状態の初期化
    st.session_state.chat_history = [] 
    st.session_state.chart_images = None
    st.session_state.research_report = ""
    st.session_state.bull_report = ""
    st.session_state.bear_report = ""
    st.session_state.manager_report = ""
    
    ticker = f"{stock_code}.T" if is_jp else stock_code.upper()
    
    with st.spinner('1/4: 市場データの取得とチャート生成中...'):
        macro_text = get_macro_data()
        stock = yf.Ticker(ticker)
        
        if is_jp:
            jp_name = get_japanese_name(stock_code)
            name = jp_name if jp_name else stock_code
        else:
            name = get_us_stock_name(stock_code)
        
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
        
        st.session_state.chart_images = {"name": name, "w": img_w, "d": img_d, "h": img_h}
        
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
                div_yield_pct = round((info.get('dividendRate', 0) / close_price) * 100, 2) if info.get('dividendRate') and close_price > 0 else 'N/A'
                market_cap_str = f"約{round(info.get('marketCap', 0) / 1000000000, 2)} Billion USD" if info.get('marketCap') else 'N/A'
                margin_str = ""
        except:
            per, pbr, div_yield_pct, market_cap_str, margin_str = 'N/A', 'N/A', 'N/A', 'N/A', ""

        edinet_str, edinet_reasons = ("", [])
        news_str = ""
        if is_jp:
            edinet_str, edinet_reasons = get_edinet_documents(stock_code, days=60)
            news_str = get_news(name, True, edinet_reasons)
            edinet_section = f"[EDINET 公式開示情報（直近2ヶ月）]\n{edinet_str}\n"
        else:
            news_str = get_news(name, False)
            edinet_section = ""

        market_data_text = (
            f"【対象銘柄詳細データ】\n--- 【銘柄: {name} ({ticker})】 ---\n"
            f"[基本ファンダメンタルズ]\n時価総額: {market_cap_str} | PER: {per} | PBR: {pbr} | 配当利回り: {div_yield_pct}%{margin_str}\n"
            f"[直近ニュース・話題・アナリスト動向]\n{news_str}\n{edinet_section}"
            f"[テクニカル値]\n終値: {close_price} {'円' if is_jp else 'ドル'} | 出来高20日平均比: {vol_ratio}倍\n"
            f"RSI: {round(latest_d['RSI'],1) if pd.notna(latest_d.get('RSI')) else 'N/A'} | MACD: {round(latest_d['MACD'],1) if pd.notna(latest_d.get('MACD')) else 'N/A'}\n"
        )

    # ---------------------------------------------------------
    # Files API を使った画像のアップロード処理（1回だけ）
    # ---------------------------------------------------------
    with st.spinner('2/4: Google Files APIへ画像を一時アップロード中...'):
        uploaded_uris = []
        
        # 1. チャート画像をアップロード
        for img_path in [img_w, img_d, img_h]:
            if img_path and os.path.exists(img_path):
                uploaded_file = genai.upload_file(img_path)
                uploaded_uris.append(uploaded_file)
                
        # 2. ドラッグ＆ドロップされたファイルのアップロード
        if uploaded_files:
            for f in uploaded_files:
                temp_file_path = f"temp_{f.name}"
                with open(temp_file_path, "wb") as temp_f:
                    temp_f.write(f.getvalue())
                uploaded_file = genai.upload_file(temp_file_path)
                uploaded_uris.append(uploaded_file)
                os.remove(temp_file_path)

        # 3. ★修正追加箇所★: フォルダ内のローカルファイル（PDF/画像）を自動アップロード
        try:
            local_files = [f for f in os.listdir('.') if stock_code.upper() in f.upper() and f.lower().endswith(('.pdf', '.png', '.jpg', '.jpeg'))]
            for file_name in local_files:
                uploaded_file = genai.upload_file(file_name)
                uploaded_uris.append(uploaded_file)
        except Exception as e:
            st.warning(f"ローカルファイルの読み込みに失敗しました: {e}")

        # モデルの設定（無料枠で最高精度の1.5 Proを使用）
        model_name = 'gemini-1.5-pro'
        text_model = genai.GenerativeModel(model_name)
        vision_model = genai.GenerativeModel(model_name)

    # ---------------------------------------------------------
    # エージェント1: リサーチエージェント（テキストのみ）
    # ---------------------------------------------------------
    with st.spinner('3/4: リサーチエージェントが情報整理中...'):
        research_prompt = generate_research_prompt(macro_text, market_data_text)
        try:
            res_research = text_model.generate_content(
                research_prompt, 
                request_options={"timeout": 600}
            )
            st.session_state.research_report = res_research.text
        except Exception as e:
            st.error(f"リサーチ処理エラー: {e}")
            st.stop()

    # ---------------------------------------------------------
    # エージェント2 & 3: 強気派・弱気派（並列処理）
    # ---------------------------------------------------------
    with st.spinner('4/4: 強気派と弱気派が白熱した議論を展開中...'):
        bull_prompt = generate_bull_prompt(st.session_state.research_report)
        bear_prompt = generate_bear_prompt(st.session_state.research_report)
        
        # Files APIのURIリストとテキストプロンプトを結合して渡す
        bull_payload = uploaded_uris + [bull_prompt]
        bear_payload = uploaded_uris + [bear_prompt]

        # 並列処理で2つのAPIを同時に叩く
        def call_bull():
            return vision_model.generate_content(bull_payload, request_options={"timeout": 600}).text
            
        def call_bear():
            return vision_model.generate_content(bear_payload, request_options={"timeout": 600}).text

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                future_bull = executor.submit(call_bull)
                future_bear = executor.submit(call_bear)
                st.session_state.bull_report = future_bull.result()
                st.session_state.bear_report = future_bear.result()
        except Exception as e:
            st.error(f"強気/弱気分析エラー: {e}")
            st.stop()

    # ---------------------------------------------------------
    # エージェント4: ファンドマネージャー（テキストのみ）
    # ---------------------------------------------------------
    with st.spinner('最終ステップ: ファンドマネージャーが裁定を下しています...'):
        manager_prompt = generate_manager_prompt(st.session_state.bull_report, st.session_state.bear_report)
        try:
            # 対話用（質問用）にチャットセッションとして開始する
            chat = text_model.start_chat(history=[])
            res_manager = chat.send_message(manager_prompt, request_options={"timeout": 600})
            st.session_state.manager_report = res_manager.text
            st.session_state.chat_session = chat
            st.success("✅ マルチエージェントによる会議・分析が完了しました！")
        except Exception as e:
            st.error(f"マネージャー裁定エラー: {e}")

# ==========================================
# 画面描画（分析結果の表示UI）
# ==========================================
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

if st.session_state.manager_report:
    # --- エージェントたちの議論プロセス（折りたたみ） ---
    st.subheader("👥 アナリストチームの議論プロセス")
    
    with st.expander("🔍 1. リサーチ結果（ファクト・将来性整理）"):
        st.write(st.session_state.research_report)
        
    with st.expander("🐂 2. 強気派（ブル派）の分析レポート"):
        st.write(st.session_state.bull_report)
        
    with st.expander("🐻 3. 弱気派（ベア派）の分析レポート"):
        st.write(st.session_state.bear_report)
        
    # --- ファンドマネージャーの最終結論（常に表示） ---
    st.subheader("📑 最終投資判断（ファンドマネージャー）")
    st.info(st.session_state.manager_report)

# ==========================================
# チャット機能
# ==========================================
if st.session_state.chat_session:
    st.markdown("---")
    st.subheader("💬 ファンドマネージャーへの追加質問・対話")
    
    for msg in st.session_state.chat_history:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            
    with st.form("chat_form", clear_on_submit=True):
        user_query = st.text_area("テキストを入力（例：弱気派の意見が気になるので、損切りラインをもっと浅くできない？）", height=100)
        submit_button = st.form_submit_button("送信")

    if submit_button and user_query:
        with st.chat_message("user"):
            st.markdown(user_query)
            
        st.session_state.chat_history.append({"role": "user", "content": user_query})
        
        with st.chat_message("assistant"):
            with st.spinner("思考中..."):
                try:
                    res = st.session_state.chat_session.send_message(
                        user_query,
                        request_options={"timeout": 600}
                    )
                    st.markdown(res.text)
                    st.session_state.chat_history.append({"role": "assistant", "content": res.text})
                except Exception as e:
                    st.error(f"チャット送信エラー: {e}")
        st.rerun()