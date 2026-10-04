import streamlit as st
import yfinance as yf
import pandas as pd
import os
import re
import io
import zipfile

from data_fetcher import (
    get_macro_data, get_edinet_documents, get_news,
    get_japanese_name, get_us_stock_name, get_japanese_fundamentals,
    search_japanese_code_by_name, search_us_ticker_by_name
)
from chart_maker import add_indicators, generate_safe_chart_image

# --- ページ設定 ---
st.set_page_config(page_title="AI株式分析データジェネレーター", layout="wide")

# --- セッションステート ---
if "generated_data" not in st.session_state:
    st.session_state.generated_data = None

st.title("📈 AI株式分析データジェネレーター")
st.markdown("このアプリは、Gemini（Web版）に読み込ませるための**最新データとチャート画像**を瞬時に生成・パッケージ化します。")

with st.sidebar:
    st.header("銘柄設定")
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
    generate_button = st.button("データ収集＆画像生成スタート", type="primary", disabled=not bool(stock_code))

# ==========================================
# データ収集＆画像生成処理
# ==========================================
if generate_button and stock_code:
    st.session_state.generated_data = None
    ticker = f"{stock_code}.T" if is_jp else stock_code.upper()
    
    with st.spinner('市場データの取得とチャート画像を生成中...'):
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
            st.error("株価データが取得できませんでした。")
            st.stop()
            
        df_w = add_indicators(df_w)
        df_d = add_indicators(df_d)
        df_h = add_indicators(df_h)
        
        img_w = generate_safe_chart_image(df_w, "temp_weekly.png", f"{name} Weekly", 260, 'weekly')
        img_d = generate_safe_chart_image(df_d, "temp_daily.png", f"{name} Daily", 130, 'daily')
        img_h = generate_safe_chart_image(df_h, "temp_hourly.png", f"{name} Hourly", 130, 'hourly')
        
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
                market_cap_str = f"{fund_data.get('market_cap')}億円" if fund_data.get('market_cap') and fund_data.get('market_cap') != 'N/A' else (f"約{round(info.get('marketCap', 0) / 100000000, 1)}億円" if info.get('marketCap') else 'N/A')
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

        full_text_data = (
            f"【グローバルマクロ参考値】\n{macro_text}\n\n"
            f"【対象銘柄詳細データ】\n--- 【銘柄: {name} ({ticker})】 ---\n"
            f"[基本ファンダメンタルズ]\n時価総額: {market_cap_str} | PER: {per} | PBR: {pbr} | 配当利回り: {div_yield_pct}%{margin_str}\n"
            f"[直近ニュース・話題・アナリスト動向]\n{news_str}\n{edinet_section}"
            f"[テクニカル値]\n終値: {close_price} {'円' if is_jp else 'ドル'} | 出来高20日平均比: {vol_ratio}倍\n"
            f"RSI: {round(latest_d['RSI'],1) if pd.notna(latest_d.get('RSI')) else 'N/A'} | MACD: {round(latest_d['MACD'],1) if pd.notna(latest_d.get('MACD')) else 'N/A'}\n"
        )
        
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "a", zipfile.ZIP_DEFLATED, False) as zip_file:
            zip_file.writestr(f"{stock_code}_market_data.txt", full_text_data.encode('utf-8'))
            for img_path, img_name in [(img_w, f"{stock_code}_weekly.png"), (img_d, f"{stock_code}_daily.png"), (img_h, f"{stock_code}_hourly.png")]:
                if img_path and os.path.exists(img_path):
                    zip_file.write(img_path, img_name)

        st.session_state.generated_data = {
            "name": name,
            "text": full_text_data,
            "images": {"w": img_w, "d": img_d, "h": img_h},
            "zip_data": zip_buffer.getvalue(),
            "stock_code": stock_code
        }
        st.success("✅ データの準備が完了しました！")

# ==========================================
# 画面描画（データ出力UI）
# ==========================================
if st.session_state.generated_data:
    data = st.session_state.generated_data
    
    col1, col2 = st.columns([2, 1])
    with col1:
        st.subheader("📦 まとめてダウンロード（PC向け）")
        st.markdown("ZIPを展開し、中のテキストと画像3枚をGeminiにドラッグ＆ドロップしてください。")
    with col2:
        st.download_button(
            label="ZIPファイルをダウンロード",
            data=data["zip_data"],
            file_name=f"{data['stock_code']}_analysis_data.zip",
            mime="application/zip",
            type="primary",
            use_container_width=True
        )
        
    st.markdown("---")
    
    st.subheader("📱 ① Geminiチャット送信用の命令文")
    st.markdown("AIに検索を強制するための文章です。コピーしてGeminiのチャット入力欄に貼り付けてください。")
    chat_prompt = f"@Google 添付したデータとチャートをもとに分析をお願いします。データ内でN/Aとなっている「{data['name']}」のファンダメンタルズ（PER、PBR、時価総額、配当利回り）や、直近のニュース・決算の詳細は、必ずGoogle検索機能を用いて最新情報を調べて補完し、指示書の通りに分析を行ってください。"
    st.code(chat_prompt, language="text")

    st.subheader("📱 ② 個別データ（添付用テキスト）")
    st.markdown("以下のテキストをコピーして、上記の命令文と一緒にGeminiへ貼り付けて送信してください。")
    st.code(data["text"], language="text")
    
    charts = data["images"]
    st.markdown(f"**📊 {data['name']} のチャート**")
    img_col1, img_col2, img_col3 = st.columns(3)
    if charts['w']:
        with img_col1: st.image(charts['w'], use_container_width=True)
    if charts['d']:
        with img_col2: st.image(charts['d'], use_container_width=True)
    if charts['h']:
        with img_col3: st.image(charts['h'], use_container_width=True)