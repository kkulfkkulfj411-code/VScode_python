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
import platform # OS判定用に追加

import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.font_manager as fm

# PDF生成用ライブラリ
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# --- フォントのパスをOS（Windowsかクラウドか）で自動判定 ---
if platform.system() == "Windows":
    SYS_FONT_PATH = 'C:/Windows/Fonts/meiryo.ttc'
    SYS_FONT_NAME = 'Meiryo'
else:
    # Linux (GitHub Codespaces) 用
    SYS_FONT_PATH = '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'
    SYS_FONT_NAME = 'NotoSansCJK'
# -------------------------------------------------------------

# 1. APIキーの読み込みとGeminiの初期設定
load_dotenv()
api_key = os.getenv("GEMINI_API_KEY")
edinet_key = os.getenv("EDINET_API_KEY")

if not api_key:
    print("エラー: .envファイルからGEMINI_API_KEYが読み込めません。")
    exit()
if not edinet_key:
    print("注意: EDINET_API_KEYが設定されていません。EDINETの情報取得はスキップされます。")

genai.configure(api_key=api_key)

# 2. マクロ指標（VIX & Fear and Greed Index）の自動取得
print("--- グローバルマクロ指標（米国VIX・市場心理）を取得中... ---")
try:
    vix_ticker = yf.Ticker("^VIX")
    vix_df = vix_ticker.history(period="5d")
    current_vix = round(vix_df['Close'].iloc[-1], 2)
except Exception as e:
    current_vix = "取得失敗"

try:
    fg_data = fear_greed.get()
    fg_score = round(fg_data['score'], 1)
    fg_rating = fg_data['rating']
    market_sentiment = f"Fear & Greed Index: {fg_score} ({fg_rating}), VIX: {current_vix}"
except Exception as e:
    market_sentiment = f"Fear & Greed Index: 取得エラー, VIX: {current_vix}"

print(f"マクロ指標取得完了: {market_sentiment}\n")

# --- EDINET API連携関数（直近60日分） ---
def fetch_edinet_for_date(target_date, edinet_code, api_key):
    url = "https://api.edinet-fsa.go.jp/api/v2/documents.json"
    params = {"date": target_date, "type": 2, "Subscription-Key": api_key}
    found_docs = []
    reasons = []
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

def get_edinet_documents(stock_code_4digit, days=60):
    if not edinet_key:
        return "EDINET APIキー未設定", []
    try:
        df_code = pd.read_csv('EdinetcodeDlInfo.csv', encoding='cp932', skiprows=1)
        target_sec_code = float(stock_code_4digit) * 10
        match_row = df_code[df_code['証券コード'] == target_sec_code]
        if match_row.empty:
            return "EDINETコードリストに該当銘柄がありません", []
        edinet_code = match_row['ＥＤＩＮＥＴコード'].values[0]
    except Exception as e:
        return "EDINETコード変換用CSVの読み込みエラー", []

    date_list = [(datetime.date.today() - datetime.timedelta(days=i)).strftime('%Y-%m-%d') for i in range(days)]
    all_found = []
    all_reasons = []
    
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(fetch_edinet_for_date, d, edinet_code, edinet_key): d for d in date_list}
        for future in as_completed(futures):
            docs, reasons = future.result()
            if docs: all_found.extend(docs)
            if reasons: all_reasons.extend(reasons)
    
    all_found.sort(reverse=True)
    unique_reasons = list(set(all_reasons))
    res_str = "\n".join(all_found) if all_found else f"直近{days}日間のEDINET公式開示書類はありません。"
    return res_str, unique_reasons

# --- 日本語ニュース自動取得関数（EDINET連動型） ---
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
                    title = entry.title.split(' - ')[0]
                    news_list.append(f"[EDINET連動深掘り] {title}")
            except:
                pass
                
    unique_news = list(dict.fromkeys(news_list))[:9]
    if unique_news:
        return " / ".join(unique_news)
    else:
        return "直近の重要な関連ニュースは見つかりませんでした。"

# 3. 分析対象の指定モード選択
print("--- 分析対象の指定 ---")
print("1: CSVファイルからリストを読み込む（複数銘柄の一括分析・テキストベース）")
print("2: 個別の銘柄コードを直接入力する（週足5年・日足1年・1時間足3か月のマルチタイムフレーム＆高解像度チャート分析）")

while True:
    mode_choice = input("番号を入力してください (1 または 2): ")
    if mode_choice in ['1', '2']:
        break
    print("1 か 2 を入力してください。")

tickers = []
names = []
csv_filename = "個別指定"
image_payloads = [] 
saved_image_files = [] 
pure_codes = [] 

def add_indicators(df):
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

def save_high_res_chart(df_full, filename, title, tail_count, timeframe_type):
    if df_full.empty:
        return False
        
    df_full.index = pd.to_datetime(df_full.index)
    if df_full.index.tz is not None:
        df_full.index = df_full.index.tz_convert(None)
        
    df_plot = df_full.tail(tail_count).copy()
    if df_plot.empty:
        return False

    aps = []
    if 'SMA25' in df_plot.columns and df_plot['SMA25'].notna().sum() > 0:
        aps.append(mpf.make_addplot(df_plot['SMA25'], color='blue', width=1.2))
    if 'SMA75' in df_plot.columns and df_plot['SMA75'].notna().sum() > 0:
        aps.append(mpf.make_addplot(df_plot['SMA75'], color='orange', width=1.2))
    if 'SMA200' in df_plot.columns and df_plot['SMA200'].notna().sum() > 0:
        aps.append(mpf.make_addplot(df_plot['SMA200'], color='red', width=1.2))
    
    if 'BB_UP' in df_plot.columns:
        aps.append(mpf.make_addplot(df_plot['BB_UP'], color='gray', width=0.8, alpha=0.6))
        aps.append(mpf.make_addplot(df_plot['BB_MID'], color='purple', width=0.8, alpha=0.6))
        aps.append(mpf.make_addplot(df_plot['BB_LOW'], color='gray', width=0.8, alpha=0.6))

    if 'Vol_SMA20' in df_plot.columns:
        aps.append(mpf.make_addplot(df_plot['Vol_SMA20'], color='darkgreen', width=1.0, panel=1, ylabel='Volume'))

    if 'RSI' in df_plot.columns:
        aps.append(mpf.make_addplot(df_plot['RSI'], color='purple', width=1.2, panel=2, ylabel='RSI'))
        df_plot['RSI_70'] = 70
        df_plot['RSI_30'] = 30
        aps.append(mpf.make_addplot(df_plot['RSI_70'], color='gray', linestyle='--', width=0.8, panel=2))
        aps.append(mpf.make_addplot(df_plot['RSI_30'], color='gray', linestyle='--', width=0.8, panel=2))

    try:
        meiryo_prop = fm.FontProperties(fname=SYS_FONT_PATH)
        my_style = mpf.make_mpf_style(base_mpf_style='yahoo', rc={'font.family': meiryo_prop.get_name()})
    except:
        my_style = 'yahoo'

    fig, axes = mpf.plot(
        df_plot, 
        type='candle', 
        style=my_style, 
        addplot=aps, 
        volume=True, 
        panel_ratios=(6, 2, 2), 
        figratio=(12, 8),
        figscale=1.5,
        title=title, 
        returnfig=True,
        savefig=dict(fname=filename, dpi=300, bbox_inches='tight')
    )
    
    ax_main = axes[0]
    
    if timeframe_type == 'weekly':
        ax_main.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
        ax_main.xaxis.set_major_formatter(mdates.DateFormatter('%y/%m'))
    elif timeframe_type == 'daily':
        ax_main.xaxis.set_major_locator(mdates.MonthLocator(interval=1))
        ax_main.xaxis.set_major_formatter(mdates.DateFormatter('%y/%m'))
    elif timeframe_type == 'hourly':
        ax_main.xaxis.set_major_locator(mdates.MonthLocator(interval=1))
        ax_main.xaxis.set_major_formatter(mdates.DateFormatter('%y/%m/%d'))

    fig.savefig(filename, dpi=300, bbox_inches='tight')
    plt.close(fig)
    return True

# ---------------------------------------------------------
# モード別のデータ取得処理
# ---------------------------------------------------------
market_data_text = f"【マクロ環境】\n{market_sentiment}\n\n"

if mode_choice == '1':
    csv_folder = 'sbi_csv'
    if not os.path.exists(csv_folder):
        os.makedirs(csv_folder)
        print(f"「{csv_folder}」フォルダを作成しました。CSVファイルを配置して再実行してください。")
        exit()
    csv_files = [f for f in os.listdir(csv_folder) if f.endswith('.csv')]
    if not csv_files:
        print(f"エラー: 「{csv_folder}」内にCSVファイルが見つかりません。")
        exit()

    print(f"\n--- 【{csv_folder}】内のCSVファイル一覧 ---")
    for i, file in enumerate(csv_files):
        print(f"{i + 1}: {file}")
    print("-" * 40)

    while True:
        try:
            choice = input("分析するCSVファイルの番号を入力してください: ")
            index = int(choice) - 1
            if 0 <= index < len(csv_files):
                csv_filename = csv_files[index]
                break
        except ValueError:
            pass

    csv_filepath = os.path.join(csv_folder, csv_filename)
    market_data_text += f"【対象銘柄詳細データ（対象リスト: {csv_filename}）】\n"
    
    try:
        df_csv = pd.read_csv(csv_filepath, encoding='utf-8-sig', header=None)
        pure_codes = df_csv[0].tolist()
        tickers = [str(code) + '.T' for code in pure_codes]
        names = df_csv[1].tolist()
    except Exception as e:
        print(f"読み込みエラーが発生しました: {e}")
        exit()

    for ticker, pure_code, name in zip(tickers, pure_codes, names):
        try:
            stock = yf.Ticker(ticker)
            df = stock.history(period="6mo")
            if df.empty or len(df) < 75: continue
            df = df.ffill()
            if pd.isna(df.iloc[-1]['Close']): continue

            df = add_indicators(df)
            latest = df.iloc[-1]
            close_price = int(latest['Close'])
            vol_avg20 = df['Volume'].tail(20).mean()
            vol_ratio = round(latest['Volume'] / vol_avg20, 2) if vol_avg20 > 0 else 1.0

            try:
                info_dict = stock.info
                per = info_dict.get('trailingPE', 'N/A')
                pbr = info_dict.get('priceToBook', 'N/A')
                div_rate = info_dict.get('dividendRate', 0)
                div_yield_pct = round((div_rate / close_price) * 100, 2) if pd.notna(div_rate) and div_rate > 0 and close_price > 0 else 'N/A'
                market_cap_oku = round(info_dict.get('marketCap', 0) / 100000000, 1) if info_dict.get('marketCap') else 'N/A'
                sector = info_dict.get('sector', 'N/A')
                industry = info_dict.get('industry', 'N/A')
                business_summary = info_dict.get('longBusinessSummary', '企業概要データなし')
                target_mean = info_dict.get('targetMeanPrice', 'N/A')
                recommendation_key = info_dict.get('recommendationKey', 'N/A')
                analyst_count = info_dict.get('numberOfAnalystOpinions', 'N/A')
            except:
                per, pbr, div_yield_pct, market_cap_oku = 'N/A', 'N/A', 'N/A', 'N/A'
                sector, industry, business_summary = 'N/A', 'N/A', '企業概要データなし'
                target_mean, recommendation_key, analyst_count = 'N/A', 'N/A', 'N/A'

            edinet_str, edinet_reasons = get_edinet_documents(pure_code, days=60)
            news_str = get_domestic_news(name, edinet_reasons)
            
            market_data_text += (
                f"--- 【銘柄: {name} ({ticker})】 ---\n"
                f"[企業概要]\n"
                f"セクター: {sector} | 業種: {industry}\n"
                f"事業内容: {business_summary}\n"
                f"[市場コンセンサス（プロのアナリスト評価）]\n"
                f"アナリスト人数: {analyst_count}人 | 平均目標株価: {target_mean}円 | レーティング傾向: {recommendation_key}\n"
                f"[ファンダメンタルズ]\n"
                f"時価総額: 約{market_cap_oku}億円 | PER: {per} | PBR: {pbr} | 配当利回り: {div_yield_pct}%\n"
                f"[直近ニュース・話題]\n{news_str}\n"
                f"[EDINET 公式開示情報（直近2ヶ月）]\n{edinet_str}\n"
                f"[株価・テクニカル]\n"
                f"終値: {close_price} 円 | 出来高: {int(latest['Volume']):,} (20日平均比: {vol_ratio}倍)\n"
                f"RSI: {round(latest['RSI'],1)} | MACD: {round(latest['MACD'],1)}\n\n"
            )
        except Exception as e:
            continue

elif mode_choice == '2':
    print("\n【個別指定モード】")
    user_input = input("深く分析したい銘柄コードを入力してください（複数指定はカンマ区切り）: ")
    pure_codes = [code.strip() for code in user_input.replace('、', ',').split(',') if code.strip()]
    tickers = [str(code) + '.T' for code in pure_codes]
    
    market_data_text += f"【対象銘柄詳細データ（個別指定マルチタイムフレーム分析）】\n"

    for ticker, pure_code in zip(tickers, pure_codes):
        try:
            stock = yf.Ticker(ticker)
            name = stock.info.get('longName') or stock.info.get('shortName') or ticker.replace('.T', '')
            print(f"【{name}】の高解像度チャート用データを取得中...")

            df_w_full = stock.history(period="max", interval="1wk").ffill()
            df_d_full = stock.history(period="2y", interval="1d").ffill()
            df_h_full = stock.history(period="6mo", interval="1h").ffill()

            if df_w_full.empty or df_d_full.empty or df_h_full.empty:
                print(f"【{name}】: データの取得に失敗したためスキップします。")
                continue

            df_w_full = add_indicators(df_w_full)
            df_d_full = add_indicators(df_d_full)
            df_h_full = add_indicators(df_h_full)

            latest_w = df_w_full.iloc[-1]
            latest_d = df_d_full.iloc[-1]
            latest_h = df_h_full.iloc[-1]
            close_price = int(latest_d['Close'])

            file_w = f"chart_{ticker}_weekly.png"
            file_d = f"chart_{ticker}_daily.png"
            file_h = f"chart_{ticker}_hourly.png"
            
            if save_high_res_chart(df_w_full, file_w, f"{name} 週足 (5年)", tail_count=260, timeframe_type='weekly'): 
                image_payloads.append(Image.open(file_w))
                saved_image_files.append(file_w)
            if save_high_res_chart(df_d_full, file_d, f"{name} 日足 (1年)", tail_count=250, timeframe_type='daily'): 
                image_payloads.append(Image.open(file_d))
                saved_image_files.append(file_d)
            if save_high_res_chart(df_h_full, file_h, f"{name} 1時間足 (3ヶ月)", tail_count=500, timeframe_type='hourly'): 
                image_payloads.append(Image.open(file_h))
                saved_image_files.append(file_h)

            vol_avg20 = df_d_full['Volume'].tail(20).mean()
            vol_ratio = round(latest_d['Volume'] / vol_avg20, 2) if vol_avg20 > 0 else 1.0

            try:
                info_dict = stock.info
                per = info_dict.get('trailingPE', 'N/A')
                pbr = info_dict.get('priceToBook', 'N/A')
                div_rate = info_dict.get('dividendRate', 0)
                div_yield_pct = round((div_rate / close_price) * 100, 2) if pd.notna(div_rate) and div_rate > 0 and close_price > 0 else 'N/A'
                market_cap_oku = round(info_dict.get('marketCap', 0) / 100000000, 1) if info_dict.get('marketCap') else 'N/A'
                sector = info_dict.get('sector', 'N/A')
                industry = info_dict.get('industry', 'N/A')
                business_summary = info_dict.get('longBusinessSummary', '企業概要データなし')
                target_mean = info_dict.get('targetMeanPrice', 'N/A')
                recommendation_key = info_dict.get('recommendationKey', 'N/A')
                analyst_count = info_dict.get('numberOfAnalystOpinions', 'N/A')
            except:
                per, pbr, div_yield_pct, market_cap_oku = 'N/A', 'N/A', 'N/A', 'N/A'
                sector, industry, business_summary = 'N/A', 'N/A', '企業概要データなし'
                target_mean, recommendation_key, analyst_count = 'N/A', 'N/A', 'N/A'

            print(f"EDINET情報を取得中（直近60日）...")
            edinet_str, edinet_reasons = get_edinet_documents(pure_code, days=60)
            news_str = get_domestic_news(name, edinet_reasons)

            market_data_text += (
                f"--- 【銘柄: {name} ({ticker})】 ---\n"
                f"[企業概要]\n"
                f"セクター: {sector} | 業種: {industry}\n"
                f"事業内容: {business_summary}\n"
                f"[市場コンセンサス（プロのアナリスト評価）]\n"
                f"アナリスト人数: {analyst_count}人 | 平均目標株価: {target_mean}円 | レーティング傾向: {recommendation_key}\n"
                f"[ファンダメンタルズ]\n"
                f"時価総額: 約{market_cap_oku}億円 | PER: {per} | PBR: {pbr} | 配当利回り: {div_yield_pct}%\n"
                f"[直近ニュース・話題・アナリスト動向（重要）]\n"
                f"{news_str}\n"
                f"[EDINET 公式開示情報（直近2ヶ月）]\n"
                f"{edinet_str}\n"
                f"[週足（5年相当）最新テクニカル値]\n"
                f"SMA25: {int(latest_w['SMA25']) if pd.notna(latest_w['SMA25']) else 'N/A'} | SMA75: {int(latest_w['SMA75']) if pd.notna(latest_w['SMA75']) else 'N/A'} | SMA200: {int(latest_w['SMA200']) if pd.notna(latest_w['SMA200']) else 'N/A'}\n"
                f"RSI: {round(latest_w['RSI'],1)} | MACD: {round(latest_w['MACD'],1)}\n"
                f"[日足（1年相当）最新テクニカル値]\n"
                f"終値: {close_price} 円 | 出来高20日平均比: {vol_ratio}倍\n"
                f"SMA25: {int(latest_d['SMA25']) if pd.notna(latest_d['SMA25']) else 'N/A'} | SMA75: {int(latest_d['SMA75']) if pd.notna(latest_d['SMA75']) else 'N/A'} | SMA200: {int(latest_d['SMA200']) if pd.notna(latest_d['SMA200']) else 'N/A'}\n"
                f"RSI: {round(latest_d['RSI'],1)} | MACD: {round(latest_d['MACD'],1)}\n"
                f"[1時間足（3ヶ月相当）最新テクニカル値]\n"
                f"SMA25: {int(latest_h['SMA25']) if pd.notna(latest_h['SMA25']) else 'N/A'} | SMA75: {int(latest_h['SMA75']) if pd.notna(latest_h['SMA75']) else 'N/A'} | SMA200: {int(latest_h['SMA200']) if pd.notna(latest_h['SMA200']) else 'N/A'}\n"
                f"RSI: {round(latest_h['RSI'],1)} | MACD: {round(latest_h['MACD'],1)}\n\n"
            )
        except Exception as e:
            print(f"【{name}】エラー: {e}")

# 5. マルチアナリストプロンプトによるGemini分析の実行
print("\n--- 高解像度チャートおよび全データの準備完了。AIアナリストチームが分析中です... ---")

model = genai.GenerativeModel('gemini-3-flash-preview')

if mode_choice == '2':
    depth_instruction = "今回は個別指定モードのため、添付された高解像度【週足（5年）】【日足（1年）】【1時間足（3か月）】のチャート画像を視覚的に分析してください。ボリンジャーバンド（±2σ）の収縮・拡散（スクイーズ・エクスパンション）、サブパネルのRSI推移や出来高移動平均線との乖離、マルチタイムフレームでのトレンド整合性を詳細に読み解いてください。"
else:
    depth_instruction = "複数銘柄を分析するため、各アナリストは要点を絞って各銘柄を評価してください。"

system_prompt = f"""
あなたは投資家（資金500万円、利益は再投資、年間平均インデックス利益超えを目指す、数日〜数週間のスイングトレード主体、最大4週間の注文期限、指値・逆指値・IFDOCO・OCO活用）のポートフォリオを支えるマルチエージェント分析システムです。

【グローバルマクロ参考値】
{market_sentiment}

【分析の深さについて】
{depth_instruction}

【アナリストの役割分担】
- アナリストA（バリュー投資家）：数字（キャッシュフロー・バランスシート等）を重視し、適正株価を算出。
- アナリストB（過熱感トレーダー）：ボリンジャーバンドのバンドウォークやRSIの過熱感などモメンタムを重視。
- アナリストC（グローバルマクロ）：中央銀行政策や情勢を踏まえ、上下の振れを判定。
- アナリストD（リスクマネージャー）：悲観的視点。提供された企業概要（事業内容）や最悪シナリオを指摘。
- アナリストE（事業・競合・市場コンセンサス分析）：【最重要】提供された[市場コンセンサス]および[直近ニュース・アナリスト動向]を読み込み、世間のプロのアナリストの評価と自社分析のギャップを検証。同業他社比較も行う。
- アナリストF（Z世代の観測者）：将来のメインプレイヤーの価値観に合致するかを重視。
- アナリストG（ニュース・チャート分析）：提供された数値、直近材料、および高解像度チャート画像（ボリンジャーバンド、RSI、出来高平均線含む）から、具体的なチャートパターンを詳細に分析。
- アナリストH（機関投資家動向）：出来高の急増（出来高移動平均線との比較）から大口の売買が行われた可能性があるかを示唆。

【ファンドマネージャーIの最終判断】
- 上記A〜Hの意見を踏まえ、現在「今すぐ買うべきか」「見送るべきか」、全力買いか打診買いかを判断。
- リスクリワードを金額とともに表記。
- HYPER SBI 2に入力するためのメモを一文で作成。先頭には必ず「月日(曜日)」を配置（例: 09/28(月) などの形式）。

【出力時の厳守ルール】
・過度な肯定や装飾は不要です。
・確信が持てない情報は（推測）と追記してください。
・重要な主張は根拠を提示し、情報を推測して補完しないでください。
・各情報に「（出典：〇〇）」の形式で情報源を明示してください。
・2023年以降に変化している可能性が高い情報には（要最新確認）を付けてください。

対象データ：
{market_data_text}
"""

try:
    chat = model.start_chat(history=[])
    payload = [system_prompt] + image_payloads
    response = chat.send_message(payload)
    report_text = response.text
    print("\nAIマルチ分析完了。PDFレポートを生成しています...")
except Exception as e:
    err_str = str(e)
    if "429" in err_str or "quota" in err_str.lower():
        print("\n【エラー】Gemini APIの利用制限（クォータ超過 / 429エラー）に達しました。")
        print("無料枠の上限回数または短時間の連続リクエスト制限を超過したため、しばらく時間を置いてから再度実行してください。")
    else:
        print(f"\nAI分析エラーが発生しました: {e}")
    exit()

# 6. PDFレポート出力処理
pdf_filename = "analysis_report.pdf"
try:
    pdfmetrics.registerFont(TTFont(SYS_FONT_NAME, SYS_FONT_PATH))
    font_name = SYS_FONT_NAME
except:
    font_name = 'Helvetica'

doc = SimpleDocTemplate(pdf_filename, pagesize=A4, rightMargin=40, leftMargin=40, topMargin=40, bottomMargin=40)
styles = getSampleStyleSheet()
normal_style = ParagraphStyle('SystemNormal', parent=styles['Normal'], fontName=font_name, fontSize=9, leading=14, textColor=colors.HexColor('#333333'))
title_style = ParagraphStyle('SystemTitle', parent=styles['Heading1'], fontName=font_name, fontSize=14, leading=20, textColor=colors.HexColor('#1a365d'), spaceAfter=12)

story = []
today_str = datetime.date.today().strftime('%Y年%m月%d日')
story.append(Paragraph(f"マルチエージェント株式分析レポート ({today_str})", title_style))
story.append(Spacer(1, 10))

for line in report_text.split('\n'):
    if line.strip() == "":
        story.append(Spacer(1, 6))
    else:
        clean_line = line.replace('**', '')
        story.append(Paragraph(clean_line, normal_style))

doc.build(story)
print(f"[完了] PDFレポートが [{pdf_filename}] として保存されました。")

for img_file in saved_image_files:
    try:
        os.remove(img_file)
    except:
        pass

# 7. チャット対話ループ
print("\n" + "="*60)
print("【ファンドマネージャー＆アナリストチームとの対話モード】")
print("出力されたレポートへの質問、反論、追加の条件などを入力してください。")
print("（終了する場合は 'exit' または 'quit' と入力してEnter）")
print("="*60)

while True:
    user_input = input("\nあなた: ")
    if user_input.lower() in ['exit', 'quit']:
        print("対話を終了します。お疲れ様でした。")
        break
    
    if not user_input.strip():
        continue
    
    print("\nAIチームが回答を協議中...")
    try:
        chat_response = chat.send_message(user_input)
        print(f"\nAIチーム:\n{chat_response.text}")
    except Exception as e:
        err_str = str(e)
        if "429" in err_str or "quota" in err_str.lower():
            print("\n【エラー】Gemini APIの利用制限（クォータ超過 / 429エラー）に達しました。しばらく時間を置いてから再度お試しください。")
        else:
            print(f"\nエラーが発生しました: {e}")