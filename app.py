import streamlit as st
import yfinance as yf
import pandas as pd
import google.generativeai as genai
from PIL import Image
import os
import re
import datetime

# ★修正点1: スプレッドシート連携用の関数をインポートに追加
from data_fetcher import (
    get_macro_data, get_edinet_documents, get_news,
    get_japanese_name, get_us_stock_name, get_japanese_fundamentals,
    search_japanese_code_by_name, search_us_ticker_by_name,
    save_analysis_to_sheet, load_history_from_sheet 
)
from chart_maker import add_indicators, generate_safe_chart_image
from ai_agent import generate_system_prompt

# --- ページ設定 ---
st.set_page_config(page_title="AI株式分析ダッシュボード", layout="wide")

# --- API初期設定 ---
try:
    api_key = st.secrets["GEMINI_API_KEY"]
    genai.configure(api_key=api_key)
    for m in genai.list_models():
        if 'generateContent' in m.supported_generation_methods:
            st.write(m.name)
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

st.title("📈 AI株式分析ダッシュボード")

# ★修正点2: 新規分析と過去の履歴を分けるタブを作成
tab_analyze, tab_history = st.tabs(["📊 新規分析を実行", "📚 過去の分析履歴（データベース）"])

# ==========================================
# タブ1：新規分析画面
# ==========================================
with tab_analyze:
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
            st.caption("※四季報概況や信用残はYahoo!ファイナンス、公式IR速報は株探（適時開示）から素早く確認できます。")
        elif not is_jp and stock_code:
            tv_url = f"https://jp.tradingview.com/chart/?symbol={stock_code.upper()}"
            yh_url = f"https://finance.yahoo.com/quote/{stock_code.upper()}"
            st.markdown(f"""
            * [TradingView（チャート分析）]({tv_url})
            * [Yahoo! Finance (US)]({yh_url})
            """)

        st.markdown("---")
        st.subheader("📁 追加資料（ドラッグ＆ドロップ）")
        st.caption("PDFレポートや、画面切り取りで画像を直接AIに渡せます。")
        uploaded_files = st.file_uploader("ファイルをここにドロップ", accept_multiple_files=True, type=['png', 'jpg', 'jpeg', 'pdf'])
        
        st.markdown("---")
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
                        doc_payloads.append({"mime_type": "application/pdf", "data": f.getvalue()})
                    else:
                        doc_payloads.append(Image.open(f))

            try:
                local_files = [f for f in os.listdir('.') if stock_code.upper() in f.upper() and f.lower().endswith(('.pdf', '.png', '.jpg', '.jpeg'))]
                for file_name in local_files:
                    if file_name.lower().endswith('.pdf'):
                        with open(file_name, "rb") as pdf_file:
                            doc_payloads.append({"mime_type": "application/pdf", "data": pdf_file.read()})
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
            
            prompt = generate_system_prompt(macro_text, market_data_text)
            
            # ★修正点3: エラーで落ちていた原因のモデル名を修正
            model = genai.GenerativeModel('gemini-2.5-pro') 
            chat = model.start_chat(history=[])
            
            try:
                response = chat.send_message([prompt] + image_payloads + doc_payloads)
                st.session_state.report_text = response.text
                st.session_state.chat_session = chat
                
                # ★修正点4: スプレッドシートへの保存処理をここに追加
                judgement_match = re.search(r'判断:\s*\[?(買い|打診買い|見送り|売り)\]?', response.text)
                judgement = judgement_match.group(1) if judgement_match else "不明"
                date_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
                
                if save_analysis_to_sheet(date_str, stock_code, name, judgement, "詳細は本文", "詳細は本文", response.text):
                    st.toast('✅ 分析結果をスプレッドシートに自動保存しました')
            except Exception as e:
                st.error(f"AI通信エラー: {e}")

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

# ==========================================
# タブ2：過去の分析履歴閲覧画面
# ==========================================
with tab_history:
    st.subheader("📚 過去の分析履歴（スプレッドシート連動）")
    st.write("過去に分析したデータの一覧です。クリックして当時の詳細レポートを確認できます。")
    
    if st.button("🔄 最新の履歴データを取得する"):
        df_history = load_history_from_sheet()
        
        if not df_history.empty:
            if "日時" in df_history.columns:
                df_history = df_history.sort_values(by="日時", ascending=False).reset_index(drop=True)
            
            st.dataframe(
                df_history[['日時', '銘柄コード', '企業名', '売買判断']] if all(k in df_history.columns for k in ['日時', '銘柄コード', '企業名', '売買判断']) else df_history, 
                use_container_width=True, 
                hide_index=True
            )
            
            st.markdown("---")
            st.subheader("🔍 レポート全文の確認")
            if '日時' in df_history.columns and '企業名' in df_history.columns and '売買判断' in df_history.columns:
                options = df_history['日時'].astype(str) + " : " + df_history['企業名'].astype(str) + " (" + df_history['売買判断'].astype(str) + ")"
                selected_idx = st.selectbox("詳細を確認したい履歴を選択してください", range(len(options)), format_func=lambda x: options[x])
                
                if selected_idx is not None:
                    selected_row = df_history.iloc[selected_idx]
                    st.markdown(f"**【分析日時】** {selected_row.get('日時', '')}  |  **【銘柄】** {selected_row.get('企業名', '')} ({selected_row.get('銘柄コード', '')})")
                    
                    with st.expander("当時の分析レポート全文を読む", expanded=True):
                        st.write(selected_row.get('レポート全文', ''))
        else:
            st.warning("まだ履歴データがありません、またはスプレッドシートからのデータ取得に失敗しました。")