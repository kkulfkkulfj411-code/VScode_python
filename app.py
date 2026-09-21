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
import tempfile

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
        q1 = urllib.parse.quote(f"{company_name} (アナリスト OR レーティング OR 目標株価 OR 株探 OR 四季報 OR 日経 OR 決算 OR 増配)")
        url1 = f"https://news.google.com/rss/search?q={q1}&hl=ja&gl=JP&ceid=JP:ja"
    else:
        q1 = urllib.parse.quote(f"{company_name} stock (earnings OR upgrade OR target OR guidance)")
        url1 = f"https://news.google.com/rss/search?q={q1}&hl=en-US&gl=US&ceid=US:en"
        
    try:
        feed = feedparser.parse(url1)
        for entry in feed.entries[:8]:
            title = entry.title.split(' - ')[0]
            if is_jp:
                if company_name in title or any(kw in title for kw in ['決算', '配当', '株', '業績', 'アナリスト', 'レーティング', '目標']):
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
        
        return {
            'per': per.group(1) if per else 'N/A',
            'pbr': pbr.group(1) if pbr else 'N/A',
            'div': div.group(1) if div else 'N/A'
        }
    except Exception:
        return {'per': 'N/A', 'pbr': 'N/A', 'div': 'N/A'}

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
    
    # 🌟修正1：KOPNなど中小型株の欠損データによるクラッシュを完全に防ぐ
    df_plot = df_plot.dropna(subset=['Open', 'High', 'Low', 'Close'])
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
            aps.append(mpf.make_addplot(df_plot['Vol_SMA20'], color='darkgreen', width=1.0, panel=panels_count, ylabel='Volume' if timeframe_type != 'daily' else '出来高'))

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
        ylabel='Price',
        ylabel_lower='Volume',
        returnfig=True
    )
    if aps:
        plot_kwargs['addplot'] = aps
    
    # 🌟修正2：エラー発生時でもアプリ全体が止まらないように保護する
    try:
        if len(panel_ratios) > 1:
            plot_kwargs['panel_ratios'] = tuple(panel_ratios)
    
        fig, axes = mpf.plot(df_plot, **plot_kwargs)
        
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
    except Exception as e:
        return None

# ==========================================
# UI構築・メインロジック
# ==========================================
st.title("📈 AI株式分析ダッシュボード")

with st.sidebar:
    st.header("分析設定")
    stock_code = st.text_input("銘柄コード（日本株は4桁、米国株はティッカーを入力）", value="NVDA")
    
    is_jp = bool(re.match(r'^\d{4}[A-Za-z]?$', stock_code))
    
    st.markdown("---")
    st.subheader("🔗 調査サイトへ一発アクセス")
    
    if is_jp:
        sbi_search_url = f"https://site0.sbisec.co.jp/ETGate/?_ControlID=WPLETmgR001Control&_PageID=WPLETmgR001Mdtl20&_DataStoreID=DSWPLETmgR001Control&_ActionID=DefaultMACON&getFlg=on&burl=search_domestic&cat1=domestic&cat2=none&dir=info&pass=%2Fdomestic%2Fstock%2Fsearch%2F&StockSecId_3={stock_code}"
        kabutan_url = f"https://kabutan.jp/stock/finance?code={stock_code}"
        tv_url = f"https://jp.tradingview.com/chart/?symbol=TSE%3A{stock_code}"
        st.markdown(f"""
        * [SBI証券（個別銘柄トップ）]({sbi_search_url})
        * [株探（業績・四季報タブ）]({kabutan_url})
        * [TradingView（チャート分析）]({tv_url})
        """)
        st.caption("※SBIのリンクを開いたら、画面中央の「分析」タブを1回クリックしてください。")
    else:
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
    analyze_button = st.button("AI分析スタート", type="primary")

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
        
        # 🌟修正3：取得段階での欠損穴埋めを強化
        df_w = stock.history(period="5y", interval="1wk").ffill().bfill()
        df_d = stock.history(period="1y", interval="1d").ffill().bfill()
        df_h = stock.history(period="3mo", interval="1h").ffill().bfill()
        
        if df_d.empty:
            st.error("株価データが取得できませんでした。ティッカーコードを確認してください。")
            st.stop()
            
        df_w = add_indicators(df_w)
        df_d = add_indicators(df_d)
        df_h = add_indicators(df_h)
        
        img_w = generate_safe_chart_image(df_w, "temp_weekly.png", f"{name} Weekly", 260, 'weekly')
        img_d = generate_safe_chart_image(df_d, "temp_daily.png", f"{name} Daily", 250, 'daily')
        img_h = generate_safe_chart_image(df_h, "temp_hourly.png", f"{name} Hourly", 500, 'hourly')
        
        # 🌟修正4：生成された画像をSession Stateに保存し、会話しても消えないようにする
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
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
                        tmp.write(f.getvalue())
                        tmp_path = tmp.name
                    uploaded_pdf = genai.upload_file(tmp_path)
                    doc_payloads.append(uploaded_pdf)
                else:
                    doc_payloads.append(Image.open(f))

        try:
            local_files = [f for f in os.listdir('.') if stock_code.upper() in f.upper() and f.lower().endswith(('.pdf', '.png', '.jpg', '.jpeg'))]
            for file_name in local_files:
                if file_name.lower().endswith('.pdf'):
                    uploaded_pdf = genai.upload_file(file_name)
                    doc_payloads.append(uploaded_pdf)
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
                market_cap_str = f"約{round(info.get('marketCap', 0) / 100000000, 1)}億円" if info.get('marketCap') else 'N/A'
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
                    
        except:
            per, pbr, div_yield_pct, market_cap_str = 'N/A', 'N/A', 'N/A', 'N/A'

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
            f"時価総額: {market_cap_str} | PER: {per} | PBR: {pbr} | 配当利回り: {div_yield_pct}%\n"
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

【グローバルマクロ参考値】
{macro_text}

【分析の深さについて】
今回は個別指定モードのため、添付された高解像度チャート画像を視覚的に分析してください。ボリンジャーバンドの収縮・拡散、サブパネルのRSI推移や出来高移動平均線との乖離、マルチタイムフレームでのトレンド整合性を詳細に読み解いてください。
さらに、四季報、個別銘柄詳細レポート(PDF)、ニュース等の追加資料が添付されている場合は、その内容（PER/PBR等の指標、業績推移、アナリスト評価など）を最優先で抽出し、各アナリストの分析（特にアナリストAの厳格なバリュー評価）の根拠としてフル活用してください。

【アナリストの役割分担】
アナリストA、あなたはウォーレンバフェットのような厳格なバリュー投資家です。定性的な夢物語は無視し、キャッシュフローとバランスシートの数字だけを信じてください。また、発表やニュース、目標株価等から適正株価を算出します。
アナリストB、あなたは市場の過熱感を察知するトレーダーです。ファンダメンタルズが良くても、市場の関心がなければ価値無しと判断します。提供されたチャートのモメンタムも重視してください。
アナリストC、あなたはグローバルマクロストラテジストです。個別の企業業績よりも、中央銀行の政策変更や戦争リスクが資産価格を決定すると考えます。現状の情勢を分析し、現在値が上下のどちらに振れているかも判定します。
アナリストD、あなたは悲観的なリスクマネージャーです。他のエージェントの強気な意見に対し、最悪のシナリオ（ブラックスワン）を突き付け、論理的に反論してください。
アナリストE、あなたは世論の分析者です。ネット上の掲示板やSNSでの「製品やサービスの価値」を重要視します。提供された[直近ニュース・話題・アナリスト動向]を深掘りし、一時的なブームに過ぎないのか、同業の競合と比べて価値があるのかを判断します。
アナリストF、あなたはZ世代の観測者です。20年前はビデオゲームが否定的に考えられていたのに対し、現在は普遍的なものとして認識されています。同様に今のZ世代の価値観が、社会のメインプレイヤーとなった時に普遍化されると予想される物事を重視します。
アナリストG、あなたはニュースと株価推移の分析者です。分析対象に関わるこれまでのニュースと世情の移り変わりを調査し、株価の大きな上下との関連を考え、直近の値動きを予想します。また、添付されたチャート画像から、現状の推移がチャートパターンに当てはまれば記述してください。それは直近一週間で見た場合と、3か月程度で見た場合を類推してください。該当がなければ「チャートパターンなし」、あればパターンの名称と共にその後の値動きを推測します。最後に、Fear & Greed Indexの数値とVIXを示し、現在欲望と恐怖のどちらに傾いているかを示します。
アナリストH、あなたは機関投資家の分析者です。大量保有報告書やForm 13Fから保有している投資家を調査し、記録があればいつ発表されたものかを明記すると共に、どういう動きをしやすい投資家かを示唆します。また添付されたチャートの出来高推移から大口の売買が行われた可能性があるかも判断します。

【出力形式】
ABCDEFGHの順で指定された銘柄を分析し、それぞれ十分な文字数を使って深く自分の意見を述べてください。
特に今買うべきか買うべきでないか、全力買いか打診買いかも判断してください。

その後ファンドマネージャーのIが最終判断をします。各アナリストの判断材料を総合し、その銘柄を買うべきかどうか、買う量、いつエントリーするべきかを詳細に判断してください。
提案する売買のリスクリワードを金額とともに表記します。
最後に、判断結果を確認できるようにHYPER SBI 2に入力するメモを一文で作成してください。

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
        
        response = chat.send_message([prompt] + image_payloads + doc_payloads)
        st.session_state.report_text = response.text

# 🌟修正5：画面を更新・チャットしてもチャートが消えないようにブロック外に移動
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
            
    if user_query := st.chat_input("レポートへの反論や追加の質問を入力..."):
        with st.chat_message("user"):
            st.markdown(user_query)
        st.session_state.chat_history.append({"role": "user", "content": user_query})
        
        with st.chat_message("assistant"):
            with st.spinner("思考中..."):
                res = st.session_state.chat_session.send_message(user_query)
                st.markdown(res.text)
        st.session_state.chat_history.append({"role": "assistant", "content": res.text})