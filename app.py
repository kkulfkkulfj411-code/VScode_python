import streamlit as st
import yfinance as yf
import pandas as pd
import os
import re
import io
import zipfile
import requests
import unicodedata
import calendar
from datetime import datetime, timedelta, timezone, time as dt_time

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
if "cache_cleared_time" not in st.session_state:
    st.session_state.cache_cleared_time = None


# --- サイドバー表示用の軽量な銘柄名取得関数（キャッシュ付き） ---
@st.cache_data(ttl=3600, show_spinner=False)
def resolve_stock_display_name(code, is_jp_flag):
    if not code:
        return ""
    try:
        if is_jp_flag:
            name = get_japanese_name(code)
            return name if name else ""
        else:
            name = get_us_stock_name(code)
            return name if name else ""
    except Exception:
        return ""


# --- HYPER SBI 2 期間指定カレンダー自動算出関数（JST固定・東証祝日・権利最終日考慮） ---
def get_sbi_order_limit_dates():
    """
    日本標準時(JST)の現在時刻（15:30前後）と東証の祝日・年末年始休場日を考慮し、
    HYPER SBI 2の期間指定における「起算日(1営業日目)」「3/5/10営業日目」「最長上限(15営業日目)」
    および「当月・翌月の月末権利付き最終日」を正確に算出する。
    """
    jp_holidays = {
        "2026-01-01", "2026-01-02", "2026-01-12", "2026-02-11", "2026-02-23",
        "2026-03-20", "2026-04-29", "2026-05-04", "2026-05-05", "2026-05-06",
        "2026-07-20", "2026-08-11", "2026-09-21", "2026-09-22", "2026-09-23",
        "2026-10-12", "2026-11-03", "2026-11-23", "2026-12-31",
        "2027-01-01", "2027-01-04", "2027-01-11", "2027-02-11", "2027-02-23",
        "2027-03-22", "2027-04-29", "2027-05-03", "2027-05-04", "2027-05-05",
        "2027-07-19", "2027-08-11", "2027-09-20", "2027-09-23", "2027-10-11",
        "2027-11-03", "2027-11-23", "2027-12-31"
    }
    weekdays_jp = ["月", "火", "水", "木", "金", "土", "日"]
    jst = timezone(timedelta(hours=9))
    now = datetime.now(jst)
    curr_date = now.date()

    def is_tse_open(d):
        return d.weekday() < 5 and d.strftime("%Y-%m-%d") not in jp_holidays

    def fmt_d(d):
        return f"{d.strftime('%m/%d')}({weekdays_jp[d.weekday()]})"

    # 平日の15:30より前なら当日が1営業日目、15:30以降または休場日なら翌営業日が1営業日目
    if is_tse_open(curr_date) and now.time() < dt_time(15, 30):
        start_date = curr_date
    else:
        start_date = curr_date + timedelta(days=1)
        while not is_tse_open(start_date):
            start_date += timedelta(days=1)

    biz_days = []
    d_cursor = start_date
    while len(biz_days) < 15:
        if is_tse_open(d_cursor):
            biz_days.append(d_cursor)
        d_cursor += timedelta(days=1)

    # 月末基準の権利付き最終日（月末最終営業日の2営業日前）の算出
    def get_month_end_kenri_date(year, month):
        last_day = calendar.monthrange(year, month)[1]
        d = datetime(year, month, last_day).date()
        open_days = []
        while len(open_days) < 3:
            if is_tse_open(d):
                open_days.append(d)
            d -= timedelta(days=1)
        return open_days[2]  # 月末最終営業日から2営業日前

    y1, m1 = start_date.year, start_date.month
    y2, m2 = (y1 + 1, 1) if m1 == 12 else (y1, m1 + 1)
    kenri_curr = get_month_end_kenri_date(y1, m1)
    kenri_next = get_month_end_kenri_date(y2, m2)

    return (
        f"[HYPER SBI 2 期間指定カレンダー（JST {now.strftime('%m/%d %H:%M')} 時点・東証祝日考慮済み）]\n"
        f"・初回有効営業日(1営業日目): {fmt_d(biz_days[0])}\n"
        f"・第1注文(新規買い)推奨期限目安: 3営業日目={fmt_d(biz_days[2])} | 5営業日目={fmt_d(biz_days[4])} | 10営業日目={fmt_d(biz_days[9])}\n"
        f"・第2注文(決済OCO)システム最長上限(15営業日目): {fmt_d(biz_days[14])}\n"
        f"・月末銘柄の権利付き最終日目安: {m1}月末基準={fmt_d(kenri_curr)} / {m2}月末基準={fmt_d(kenri_next)} （※当該銘柄の権利確定月である場合、権利付き最終日をまたぐ期間指定は不可のためその日が上限となる）\n"
    )


# --- キャッシュ回避＆異常値補正対応のデータ取得関数 ---
def get_fresh_ticker_and_history(ticker_symbol, use_repair=True):
    """
    新規セッションでキャッシュを回避してデータを取得する。
    yfinanceの仕様変更等でカスタムSessionやrepairがエラーを吐いた場合は安全にフォールバックする。
    """
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Cache-Control': 'no-cache, no-store, must-revalidate',
        'Pragma': 'no-cache'
    })

    try:
        stock = yf.Ticker(ticker_symbol, session=session)
        df_d = stock.history(period="2y", interval="1d", repair=use_repair)
        if df_d.empty:
            raise ValueError("Custom session returned empty DataFrame")
    except Exception:
        stock = yf.Ticker(ticker_symbol)
        try:
            df_d = stock.history(period="2y", interval="1d", repair=use_repair)
        except Exception:
            df_d = stock.history(period="2y", interval="1d")

    try:
        df_w = stock.history(period="10y", interval="1wk", repair=use_repair)
    except Exception:
        df_w = stock.history(period="10y", interval="1wk")

    try:
        df_h = stock.history(period="1y", interval="1h", repair=use_repair)
    except Exception:
        df_h = stock.history(period="1y", interval="1h")

    return stock, df_w, df_d, df_h


# --- 安全な数値フォーマット＆時系列テキスト生成関数 ---
def format_price(val, is_jp):
    if pd.isna(val):
        return "N/A"
    return str(int(round(val))) if is_jp else f"{val:.2f}"


def build_us_extra_fundamentals(stock, info, close_price_num):
    """
    米国株向けにyfinanceのstock.infoとstock.newsから予想PER、成長性、空売り需給（Short Interest）、
    機関投資家保有比率、アナリスト目標株価、英語一次ニュースを安全に抽出してテキスト化する。
    """
    def fmt_pct(key):
        val = info.get(key)
        return f"{round(val * 100, 2)}%" if isinstance(val, (int, float)) and pd.notna(val) else "N/A"

    def fmt_num(key, digits=2):
        val = info.get(key)
        return f"{round(val, digits)}" if isinstance(val, (int, float)) and pd.notna(val) else "N/A"

    fwd_pe = fmt_num('forwardPE')
    peg = fmt_num('pegRatio')
    roe = fmt_pct('returnOnEquity')
    rev_growth = fmt_pct('revenueGrowth')
    op_margin = fmt_pct('operatingMargins')
    beta = fmt_num('beta')

    short_pct = fmt_pct('shortPercentOfFloat')
    short_ratio = fmt_num('shortRatio')
    inst_held = fmt_pct('heldPercentInstitutions')
    insider_held = fmt_pct('heldPercentInsiders')

    rec_key = info.get('recommendationKey', 'N/A')
    target_mean = info.get('targetMeanPrice')
    target_high = info.get('targetHighPrice')
    target_low = info.get('targetLowPrice')

    if isinstance(target_mean, (int, float)) and pd.notna(target_mean) and close_price_num > 0:
        upside = round(((target_mean - close_price_num) / close_price_num) * 100, 2)
        upside_str = f"{'+' if upside > 0 else ''}{upside}%"
        target_str = f"平均 {target_mean}ドル (現在値比 {upside_str} | 上限 {target_high} / 下限 {target_low})"
    else:
        target_str = "N/A"

    yf_news_lines = []
    try:
        raw_news = getattr(stock, 'news', None)
        if raw_news and isinstance(raw_news, list):
            for item in raw_news[:5]:
                content = item.get('content', {}) if isinstance(item, dict) else {}
                title = item.get('title') or content.get('title')
                if title:
                    yf_news_lines.append(f"・{title}")
    except Exception:
        pass
    yf_news_str = "\n".join(yf_news_lines) if yf_news_lines else "（yfinance英語ニュース取得なし）"

    return (
        f"[米国株 詳細指標・成長性]\n"
        f"予想PER(Forward): {fwd_pe} | PEGレシオ: {peg} | ROE: {roe} | 売上成長率: {rev_growth} | 営業利益率: {op_margin} | Beta(感応度): {beta}\n"
        f"[米国株 需給・空売り動向・コンセンサス（※空売り残高は月2回公表のためタイムラグに注意）]\n"
        f"浮動株空売り比率(Short % of Float): {short_pct} | 空売りカバー日数(Days to Cover): {short_ratio}日\n"
        f"機関投資家保有比率: {inst_held} | インサイダー保有比率: {insider_held}\n"
        f"アナリスト評価: {rec_key} | 目標株価コンセンサス: {target_str}\n"
        f"[米国現地ニュースヘッドライン (yfinance)]\n{yf_news_str}\n"
        f"[米国株 注文仕様上の厳守事項]\n"
        f"SBI証券の米国株取引はIFD/OCO/IFDOCO注文および執行条件（不成・引成等）に非対応であり、単一注文（指値・成行・逆指値、期間指定は当日中または最長90現地営業日）のみ可能。また値幅制限（ストップ高安）が存在せず1株単位で売買可能である点を前提に戦略を組むこと。\n"
    )


def build_technical_summary_text(df_d, is_jp):
    """
    日足データから移動平均線の状態（現在値・5日前比の傾き・乖離率）と
    直近10営業日の時系列テーブルをエラーなく安全にテキスト化する。
    """
    unit = "円" if is_jp else "ドル"
    close_series = df_d['Close']
    vol_avg20_series = df_d['Volume'].rolling(20).mean()

    sma_lines = []
    for window in [25, 75, 200]:
        if len(close_series) >= window:
            sma_series = close_series.rolling(window).mean()
            curr_sma = sma_series.iloc[-1]
            prev_sma = sma_series.iloc[-6] if len(sma_series) >= window + 5 else pd.NA

            if pd.notna(curr_sma):
                curr_close = close_series.iloc[-1]
                dev_pct = round(((curr_close - curr_sma) / curr_sma) * 100, 2)
                dev_str = f"{'+' if dev_pct > 0 else ''}{dev_pct}%"

                if pd.notna(prev_sma):
                    diff = curr_sma - prev_sma
                    diff_fmt = int(round(diff)) if is_jp else round(diff, 2)
                    if diff > 0:
                        slope_str = f"上昇中(5日前比 +{diff_fmt}{unit})"
                    elif diff < 0:
                        slope_str = f"下降中(5日前比 {diff_fmt}{unit})"
                    else:
                        slope_str = "横ばい(5日前比 ±0)"
                else:
                    slope_str = "傾き算出不可"

                sma_lines.append(
                    f"SMA{window}: {format_price(curr_sma, is_jp)}{unit} [現在値乖離: {dev_str} | 傾き: {slope_str}]"
                )
            else:
                sma_lines.append(f"SMA{window}: N/A")
        else:
            sma_lines.append(f"SMA{window}: N/A (データ期間不足)")

    sma_text = "\n".join(sma_lines)

    recent_df = df_d.tail(10)
    recent_vol_avg = vol_avg20_series.tail(10)

    table_rows = [
        "日付 | 始値 | 高値(上ヒゲ) | 安値(下ヒゲ) | 終値 | 出来高比(20日平均比) | RSI | MACD"
    ]
    for idx, row in recent_df.iterrows():
        date_str = idx.strftime('%m/%d') if hasattr(idx, 'strftime') else str(idx)[:10]
        o_str = format_price(row.get('Open'), is_jp)
        h_str = format_price(row.get('High'), is_jp)
        l_str = format_price(row.get('Low'), is_jp)
        c_str = format_price(row.get('Close'), is_jp)

        v_avg = recent_vol_avg.loc[idx] if idx in recent_vol_avg.index else pd.NA
        v_ratio = f"{round(row['Volume'] / v_avg, 2)}倍" if pd.notna(v_avg) and v_avg > 0 else "N/A"

        rsi_val = f"{round(row['RSI'], 1)}" if 'RSI' in row and pd.notna(row['RSI']) else "N/A"
        macd_val = f"{round(row['MACD'], 1)}" if 'MACD' in row and pd.notna(row['MACD']) else "N/A"

        table_rows.append(
            f"{date_str} | {o_str} | {h_str} | {l_str} | {c_str} | {v_ratio} | {rsi_val} | {macd_val}"
        )

    table_text = "\n".join(table_rows)
    return sma_text, table_text


st.title("📈 AI株式分析データジェネレーター")
st.markdown("このアプリは、Gemini（Web版）に読み込ませるための**最新データとチャート画像**を瞬時に生成・パッケージ化します。")

with st.sidebar:
    st.header("銘柄設定")
    market_filter = st.radio(
        "検索対象市場",
        ["日本株のみ", "すべて（日米）", "米国株のみ"],
        index=0,
        horizontal=True,
        help="日本株の英字社名（MTGなど）が米国株と混同されるのを防ぎます。"
    )
    raw_input = st.text_input("銘柄コード または 企業名（日米対応・一部でも可）", value="6844").strip()
    
    # 日本株の証券コード判定パターン（数字4桁 または 数字3桁+英字1文字）
    jp_code_pattern = r'^[0-9]{3}[0-9A-Za-z]$'
    
    stock_code = ""
    is_jp = False
    selected_label_name = ""
    
    if not raw_input:
        stock_code = ""
    elif re.match(jp_code_pattern, raw_input):
        stock_code = raw_input.upper()
        is_jp = True
    else:
        jp_matches = []
        us_matches = []
        
        if market_filter in ["日本株のみ", "すべて（日米）"]:
            half_upper = unicodedata.normalize('NFKC', raw_input).upper()
            full_upper = half_upper.translate(
                str.maketrans(
                    '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ',
                    '０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ'
                )
            )
            for q in dict.fromkeys([raw_input, half_upper, full_upper]):
                res = search_japanese_code_by_name(q)
                if res:
                    for item in res:
                        if item not in jp_matches:
                            jp_matches.append(item)
                            
        if market_filter in ["すべて（日米）", "米国株のみ"] and re.search(r'[A-Za-z]', raw_input):
            us_matches = search_us_ticker_by_name(raw_input)
            
        all_matches = jp_matches + us_matches
        
        if all_matches:
            selected = st.selectbox("候補が見つかりました。対象を選択してください:", all_matches)
            parts = selected.split(" - ", 1)
            stock_code = parts[0].strip().upper()
            selected_label_name = parts[1].strip() if len(parts) > 1 else ""
            is_jp = bool(re.match(jp_code_pattern, stock_code))
        else:
            if market_filter != "日本株のみ" and re.match(r'^[A-Za-z.\-]+$', raw_input):
                stock_code = raw_input.upper()
                is_jp = False
            else:
                st.warning("該当する銘柄が見つかりませんでした。市場切替や証券コード直接入力をお試しください。")
                stock_code = ""

    # 確定している対象銘柄の即時視認表示（日本株・米国株共通）
    if stock_code:
        display_name = selected_label_name or resolve_stock_display_name(stock_code, is_jp)
        market_badge = "日本株" if is_jp else "米国株"
        if display_name and display_name != stock_code:
            st.success(f"🎯 **対象確定**: [{stock_code}] **{display_name}** ({market_badge})")
        else:
            st.info(f"🎯 **対象コード確定**: [{stock_code}] ({market_badge}としてデータ取得を実行します)")
    
    st.markdown("---")
    st.subheader("⚙️ データ取得・キャッシュ設定")
    use_repair = st.checkbox(
        "株式分割バグ・異常値を自動補正 (repair=True)",
        value=True,
        help="株式分割直後のスパイクノイズ（異常なヒゲ）を補正します。取得が重い場合はオフにしてください。"
    )
    if st.button("🔄 キャッシュをクリアして初期化", use_container_width=True):
        st.cache_data.clear()
        st.session_state.generated_data = None
        jst = timezone(timedelta(hours=9))
        st.session_state.cache_cleared_time = datetime.now(jst).strftime("%H:%M:%S")
        st.rerun()

    # キャッシュクリア完了の視覚表示
    if st.session_state.cache_cleared_time:
        st.toast(f"キャッシュをクリアしました ({st.session_state.cache_cleared_time})", icon="✅")
        st.success(f"✅ キャッシュと生成データをクリアしました（{st.session_state.cache_cleared_time}）")

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
        finviz_url = f"https://finviz.com/quote.ashx?t={stock_code.upper()}"
        st.markdown(f"""
        * [TradingView（チャート分析）]({tv_url})
        * [Yahoo! Finance (US)]({yh_url})
        * [Finviz（需給・空売り・インサイダー確認）]({finviz_url})
        * [SBI証券（メインサイト）](https://www.sbisec.co.jp/)
        """)
    
    st.markdown("---")
    generate_button = st.button("データ収集＆画像生成スタート", type="primary", disabled=not bool(stock_code))

# ==========================================
# データ収集＆画像生成処理
# ==========================================
if generate_button and stock_code:
    st.cache_data.clear()
    st.session_state.generated_data = None
    st.session_state.cache_cleared_time = None
    ticker = f"{stock_code}.T" if is_jp else stock_code.upper()
    
    with st.spinner('市場データの取得とチャート画像を生成中...'):
        macro_text = get_macro_data()
        
        stock, df_w, df_d, df_h = get_fresh_ticker_and_history(ticker, use_repair=use_repair)
        
        if is_jp:
            jp_name = get_japanese_name(stock_code)
            name = jp_name if jp_name else stock_code
        else:
            name = get_us_stock_name(stock_code)
        
        if df_d.empty:
            st.error("株価データが取得できませんでした。")
            st.stop()
            
        df_w = df_w.ffill().bfill()
        df_d = df_d.ffill().bfill()
        df_h = df_h.ffill().bfill()

        # 東証クロージング・オークション等による日足と1時間足の最終終値ズレを同期補正
        if not df_h.empty and not df_d.empty:
            try:
                if df_h.index[-1].date() == df_d.index[-1].date():
                    d_close = df_d['Close'].iloc[-1]
                    df_h.iloc[-1, df_h.columns.get_loc('Close')] = d_close
                    if d_close > df_h['High'].iloc[-1]:
                        df_h.iloc[-1, df_h.columns.get_loc('High')] = d_close
                    if d_close < df_h['Low'].iloc[-1]:
                        df_h.iloc[-1, df_h.columns.get_loc('Low')] = d_close
            except Exception:
                pass
            
        df_w = add_indicators(df_w)
        df_d = add_indicators(df_d)
        df_h = add_indicators(df_h)
        
        # is_jpフラグを明示的に渡して日本株の小数点表示バグを防止
        img_w = generate_safe_chart_image(df_w, "temp_weekly.png", f"{name} Weekly", 260, 'weekly', is_jp=is_jp)
        img_d = generate_safe_chart_image(df_d, "temp_daily.png", f"{name} Daily", 130, 'daily', is_jp=is_jp)
        img_h = generate_safe_chart_image(df_h, "temp_hourly.png", f"{name} Hourly", 130, 'hourly', is_jp=is_jp)
        
        latest_d = df_d.iloc[-1]
        unit_str = '円' if is_jp else 'ドル'
        
        open_price = format_price(latest_d.get('Open'), is_jp)
        high_price = format_price(latest_d.get('High'), is_jp)
        low_price = format_price(latest_d.get('Low'), is_jp)
        close_price_num = round(latest_d['Close'], 2) if not is_jp else int(round(latest_d['Close']))
        close_price = format_price(latest_d.get('Close'), is_jp)
        
        recent_high_5d = format_price(df_d['High'].tail(5).max(), is_jp)
        recent_low_5d = format_price(df_d['Low'].tail(5).min(), is_jp)
        recent_high_20d = format_price(df_d['High'].tail(20).max(), is_jp)
        recent_low_20d = format_price(df_d['Low'].tail(20).min(), is_jp)
        recent_high_60d = format_price(df_d['High'].tail(60).max(), is_jp)
        recent_low_60d = format_price(df_d['Low'].tail(60).min(), is_jp)
        
        vol_avg20 = df_d['Volume'].tail(20).mean()
        vol_ratio = round(latest_d['Volume'] / vol_avg20, 2) if pd.notna(vol_avg20) and vol_avg20 > 0 else 1.0

        sma_summary_text, recent_10d_table_text = build_technical_summary_text(df_d, is_jp)

        us_extra_section = ""
        try:
            info = stock.info
            if is_jp:
                fund_data = get_japanese_fundamentals(stock_code)
                per = fund_data['per'] if fund_data['per'] != 'N/A' else info.get('trailingPE', 'N/A')
                pbr = fund_data['pbr'] if fund_data['pbr'] != 'N/A' else info.get('priceToBook', 'N/A')
                div_yield_pct = fund_data['div'] if fund_data['div'] != 'N/A' else (round((info.get('dividendRate', 0) / close_price_num) * 100, 2) if info.get('dividendRate') and close_price_num > 0 else 'N/A')
                margin_ratio = fund_data['margin']
                market_cap_str = f"{fund_data.get('market_cap')}億円" if fund_data.get('market_cap') and fund_data.get('market_cap') != 'N/A' else (f"約{round(info.get('marketCap', 0) / 100000000, 1)}億円" if info.get('marketCap') else 'N/A')
                margin_str = f" | 信用倍率: {margin_ratio}倍" if margin_ratio != 'N/A' else ""
            else:
                per = round(info.get('trailingPE', 0), 2) if info.get('trailingPE') else 'N/A'
                pbr = round(info.get('priceToBook', 0), 2) if info.get('priceToBook') else 'N/A'
                div_yield_pct = round((info.get('dividendRate', 0) / close_price_num) * 100, 2) if info.get('dividendRate') and close_price_num > 0 else 'N/A'
                market_cap_str = f"約{round(info.get('marketCap', 0) / 1000000000, 2)} Billion USD" if info.get('marketCap') else 'N/A'
                margin_str = ""
                us_extra_section = build_us_extra_fundamentals(stock, info, close_price_num)
        except Exception:
            per, pbr, div_yield_pct, market_cap_str, margin_str = 'N/A', 'N/A', 'N/A', 'N/A', ""

        edinet_str, edinet_reasons = ("", [])
        news_str = ""
        if is_jp:
            edinet_str, edinet_reasons = get_edinet_documents(stock_code, days=60)
            news_str = get_news(name, True, edinet_reasons)
            edinet_section = f"[EDINET 公式開示情報（直近2ヶ月）]\n{edinet_str}\n"
            order_calendar_section = get_sbi_order_limit_dates()
            market_specific_section = (
                f"{order_calendar_section}"
                f"[TOPIX指数・需給イベント確認事項]\n"
                f"対象銘柄（{name} / {stock_code}）のTOPIX定期入替区分（新規採用35銘柄・移行措置683銘柄[段階的ウエイト低減]・継続採用951銘柄・非構成銘柄）をGoogle検索で確認し、"
                f"四半期リバランス日（1月・4月・7月・10月の各最終営業日）に向けたパッシブ需給の影響を分析に織り込むこと（検索で確定情報がない場合は『区分不明』と明記し推測しないこと）。\n"
            )
        else:
            news_str = get_news(name, False)
            edinet_section = ""
            market_specific_section = us_extra_section

        full_text_data = (
            f"【グローバルマクロ参考値】\n{macro_text}\n\n"
            f"【対象銘柄詳細データ】\n--- 【銘柄: {name} ({ticker}) | 市場区分: {'日本株' if is_jp else '米国株'}】 ---\n"
            f"[基本ファンダメンタルズ]\n時価総額: {market_cap_str} | PER(実績): {per} | PBR: {pbr} | 配当利回り: {div_yield_pct}%{margin_str}\n"
            f"{market_specific_section}"
            f"[直近ニュース・話題・アナリスト動向]\n{news_str}\n{edinet_section}"
            f"[テクニカル基本値]\n最新営業日の四本値: 始値 {open_price} | 高値 {high_price} | 安値 {low_price} | 終値 {close_price} {unit_str}\n"
            f"期間別高安値: [直近5日] 高値 {recent_high_5d} / 安値 {recent_low_5d} | [直近20日] 高値 {recent_high_20d} / 安値 {recent_low_20d} | [直近60日] 高値 {recent_high_60d} / 安値 {recent_low_60d}\n"
            f"出来高20日平均比: {vol_ratio}倍 | 最新RSI: {round(latest_d['RSI'],1) if pd.notna(latest_d.get('RSI')) else 'N/A'} | 最新MACD: {round(latest_d['MACD'],1) if pd.notna(latest_d.get('MACD')) else 'N/A'}\n\n"
            f"[日足 移動平均線（現在値・乖離率・5日前比の傾き）]\n{sma_summary_text}\n\n"
            f"[直近10営業日の時系列推移（日足ベース）]\n{recent_10d_table_text}\n"
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
            "stock_code": stock_code,
            "is_jp": is_jp
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
    
    if data.get("is_jp", True):
        demand_items = (
            f"・直近の業績推移（売上高成長率、営業利益率、ROE）と財務健全性（自己資本比率）\n"
            f"・決算の市場見通し（コンセンサス予想との乖離、上方/下方修正の有無、次回決算発表日、配当権利確定月）\n"
            f"・大口の需給動向（日次公表の信用買い残・売り残の推移、機関投資家の空売り残高、大量保有報告書の有無）\n"
            f"・TOPIX定期入替の区分（「{data['name']}（{data['stock_code']}）」が新規採用35銘柄・移行措置683銘柄[段階的ウエイト低減]・継続採用のいずれに該当するか）と、四半期リバランス日（1・4・7・10月の各最終営業日）に伴うパッシブ需給影響（※検索で確定情報がない場合は推測せず『区分不明』とすること）\n"
        )
    else:
        demand_items = (
            f"・直近の四半期決算（EPSおよび売上高の市場コンセンサスビート/ミス状況、Non-GAAPベースの数値、ガイダンス引き上げの有無、次回Earnings Date[決算発表日]）\n"
            f"・SEC開示および大口需給動向（Form 13Fによる機関投資家の増減、Form 4によるインサイダー売買、最新のShort Interest[空売り比率]の推移）\n"
            f"・米国マクロ指標（FOMC金利見通し、米10年債利回り、セクターローテーション）および主要指数（S&P500、NASDAQ100等）のリバランス影響\n"
            f"・※米国株の最新決算・ガイダンス・時間外株価の変動要因については、日本語記事だけでなく必ずティッカーシンボルを用いた英語クエリ（例：`{data['stock_code']} earnings release guidance`）でも検索して一次情報を確認すること\n"
        )

    chat_prompt = (
        f"@Google 添付したデータとチャートをもとに分析をお願いします。\n"
        f"データ内でN/Aとなっている「{data['name']}（{data['stock_code']}）」の基本指標に加え、"
        f"多角的な分析を行うため、以下の重要情報についても必ずGoogle検索機能を用いて最新の数値を調べ、情報を補完した上で指示書の通りに分析を行ってください。\n\n"
        f"【追加検索・補完項目】\n"
        f"{demand_items}"
        f"・直近のニュース、適時開示（またはSEC Filing）、アナリストのレーティング変更の具体的な詳細内容"
    )
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