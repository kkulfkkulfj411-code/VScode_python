import streamlit as st
import yfinance as yf
import pandas as pd
import os
import re
import io
import zipfile
import unicodedata
from datetime import datetime, timedelta, timezone

from data_fetcher import (
    get_macro_data, get_edinet_documents, get_news,
    get_japanese_name, get_us_stock_name, get_japanese_fundamentals,
    search_japanese_code_by_name, search_us_ticker_by_name,
    get_sbi_order_limit_dates
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


# --- yfinance 1.7.0 対応のデータ取得関数（現地タイムゾーン正規化・実勢四本値維持） ---
def get_fresh_ticker_and_history(ticker_symbol, is_jp=True, use_repair=False):
    """
    yfinance 1.x系の内部curl_cffiセッション管理に任せ、
    TradingView/SBI2と過去高安値を一致させるため auto_adjust=False を明示して取得する。
    取得直後に現地タイムゾーン（JST / EST）へ変換してからtzを除去し日付ズレを防止する。
    """
    stock = yf.Ticker(ticker_symbol)
    target_tz = 'Asia/Tokyo' if is_jp else 'America/New_York'

    def _safe_history(period_str, interval_str):
        try:
            df = stock.history(period=period_str, interval=interval_str, auto_adjust=False, repair=use_repair)
        except Exception:
            try:
                df = stock.history(period=period_str, interval=interval_str, auto_adjust=False, repair=False)
            except Exception:
                df = pd.DataFrame()
        if not df.empty:
            if "Repaired?" in df.columns:
                df = df.drop(columns=["Repaired?"])
            df.index = pd.to_datetime(df.index)
            if df.index.tz is not None:
                try:
                    df.index = df.index.tz_convert(target_tz).tz_localize(None)
                except Exception:
                    df.index = df.index.tz_localize(None)
        return df

    df_d = _safe_history("2y", "1d")
    df_w = _safe_history("10y", "1wk")
    df_h = _safe_history("1y", "1h")

    return stock, df_w, df_d, df_h


# --- 安全な数値フォーマット＆時系列テキスト生成関数 ---
def format_price(val, is_jp):
    if pd.isna(val):
        return "N/A"
    return str(int(round(val))) if is_jp else f"{val:.2f}"


def build_us_extra_fundamentals(stock, info, close_price_num):
    """
    米国株向けにyfinanceのstock.info、calendar、newsから予想PER、成長性、空売り需給、
    機関投資家保有比率、アナリスト目標株価、次回決算日、英語一次ニュースを抽出する。
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
                if not isinstance(item, dict):
                    continue
                raw_content = item.get('content')
                content = raw_content if isinstance(raw_content, dict) else {}
                title = content.get('title') or item.get('title')
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

# ==========================================
# サイドバー（スクロール不要のコンパクト設計）
# ==========================================
with st.sidebar:
    st.subheader("🎯 銘柄選択・実行")
    market_filter = st.radio(
        "検索対象市場",
        ["日本株のみ", "すべて（日米）", "米国株のみ"],
        index=0,
        horizontal=True,
        help="日本株の英字社名（MTGなど）が米国株と混同されるのを防ぎます。"
    )
    raw_input = st.text_input("銘柄コード または 企業名（日米対応）", value="6844").strip()

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
            selected = st.selectbox("候補を選択:", all_matches)
            parts = selected.split(" - ", 1)
            stock_code = parts[0].strip().upper()
            selected_label_name = parts[1].strip() if len(parts) > 1 else ""
            is_jp = bool(re.match(jp_code_pattern, stock_code))
        else:
            if market_filter != "日本株のみ" and re.match(r'^[A-Za-z.\-]+$', raw_input):
                stock_code = raw_input.upper()
                is_jp = False
            else:
                st.warning("該当する銘柄が見つかりませんでした。")
                stock_code = ""

    if stock_code:
        display_name = selected_label_name or resolve_stock_display_name(stock_code, is_jp)
        market_badge = "日本株" if is_jp else "米国株"
        if display_name and display_name != stock_code:
            st.success(f"🎯 **[{stock_code}] {display_name}** ({market_badge})")
        else:
            st.info(f"🎯 **[{stock_code}]** ({market_badge})")

    # 最重要の実行ボタンを最上部に配置（スクロールゼロで即実行可能）
    generate_button = st.button(
        "🚀 データ収集＆画像生成スタート",
        type="primary",
        use_container_width=True,
        disabled=not bool(stock_code)
    )

    # 外部調査サイトへのリンクを1ブロックに圧縮
    if is_jp and stock_code:
        yahoo_url = f"https://finance.yahoo.co.jp/quote/{stock_code}.T"
        kabutan_disclose_url = f"https://kabutan.jp/stock/news?code={stock_code}&b=k"
        kabutan_finance_url = f"https://kabutan.jp/stock/finance?code={stock_code}"
        tv_url = f"https://jp.tradingview.com/chart/?symbol=TSE%3A{stock_code}"
        st.caption(
            f"🔗 **外部調査**: [Yahoo]({yahoo_url}) ｜ [株探(開示)]({kabutan_disclose_url}) ｜ "
            f"[株探(財務)]({kabutan_finance_url}) ｜ [TradingView]({tv_url}) ｜ [SBI証券](https://www.sbisec.co.jp/)"
        )
    elif not is_jp and stock_code:
        tv_url = f"https://jp.tradingview.com/chart/?symbol={stock_code.upper()}"
        yh_url = f"https://finance.yahoo.com/quote/{stock_code.upper()}"
        finviz_url = f"https://finviz.com/quote.ashx?t={stock_code.upper()}"
        st.caption(
            f"🔗 **外部調査**: [TradingView]({tv_url}) ｜ [Yahoo(US)]({yh_url}) ｜ "
            f"[Finviz]({finviz_url}) ｜ [SBI証券](https://www.sbisec.co.jp/)"
        )

    # 低頻度の設定は折りたたみメニューに収納
    with st.expander("⚙️ 取得補正・キャッシュ設定", expanded=False):
        use_repair = st.checkbox(
            "株式分割バグ・異常値を自動補正 (repair=True)",
            value=False,
            help="株式分割直後のスパイクノイズがある場合のみオンにしてください。通常時はオフの方が高速かつ安定します。"
        )
        if st.button("🔄 キャッシュをクリアして初期化", use_container_width=True):
            st.cache_data.clear()
            st.session_state.generated_data = None
            jst = timezone(timedelta(hours=9))
            st.session_state.cache_cleared_time = datetime.now(jst).strftime("%H:%M:%S")
            st.rerun()

    if st.session_state.cache_cleared_time:
        st.caption(f"✅ キャッシュクリア済 ({st.session_state.cache_cleared_time})")

# ==========================================
# データ収集＆画像生成処理
# ==========================================
if generate_button and stock_code:
    st.session_state.generated_data = None
    st.session_state.cache_cleared_time = None
    ticker = f"{stock_code}.T" if is_jp else stock_code.upper()

    with st.spinner('市場データの取得とチャート画像を生成中...'):
        macro_text = get_macro_data()

        stock, df_w, df_d, df_h = get_fresh_ticker_and_history(ticker, is_jp=is_jp, use_repair=use_repair)

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

        # 52週（直近250営業日）高値・安値と現在値からの乖離率
        high_52w_val = df_d['High'].tail(250).max()
        low_52w_val = df_d['Low'].tail(250).min()
        high_52w_str = format_price(high_52w_val, is_jp)
        low_52w_str = format_price(low_52w_val, is_jp)
        if pd.notna(high_52w_val) and high_52w_val > 0:
            from_52w_high_pct = round(((close_price_num - high_52w_val) / high_52w_val) * 100, 2)
            from_52w_str = f" (52週高値比 {from_52w_high_pct}%)"
        else:
            from_52w_str = ""

        # 日足ATR(14)のフォーマット（値幅と対終値比率）
        atr_val = latest_d.get('ATR')
        if pd.notna(atr_val) and close_price_num > 0:
            atr_fmt = format_price(atr_val, is_jp)
            atr_pct = round((atr_val / close_price_num) * 100, 2)
            atr_str = f"{atr_fmt}{unit_str} ({atr_pct}%)"
        else:
            atr_str = "N/A"

        vol_avg20 = df_d['Volume'].tail(20).mean()
        vol_ratio = round(latest_d['Volume'] / vol_avg20, 2) if pd.notna(vol_avg20) and vol_avg20 > 0 else 1.0

        sma_summary_text, recent_10d_table_text = build_technical_summary_text(df_d, is_jp)

        info = {}
        try:
            raw_info = stock.info
            if isinstance(raw_info, dict):
                info = raw_info
        except Exception:
            info = {}

        us_extra_section = ""
        if is_jp:
            fund_data = get_japanese_fundamentals(stock_code)

            if fund_data.get('per') and fund_data['per'] != 'N/A':
                per_str = f"{fund_data['per']}倍" if fund_data['per'][0].isdigit() else fund_data['per']
            else:
                yf_pe = info.get('trailingPE') or info.get('forwardPE')
                per_str = f"{round(yf_pe, 2)}倍" if isinstance(yf_pe, (int, float)) and pd.notna(yf_pe) else "N/A"

            if fund_data.get('pbr') and fund_data['pbr'] != 'N/A':
                pbr_str = f"{fund_data['pbr']}倍"
            else:
                yf_pbr = info.get('priceToBook')
                pbr_str = f"{round(yf_pbr, 2)}倍" if isinstance(yf_pbr, (int, float)) and pd.notna(yf_pbr) else "N/A"

            if fund_data.get('div') and fund_data['div'] != 'N/A':
                div_str = f"{fund_data['div']}%"
            else:
                div_rate = info.get('dividendRate') or info.get('trailingAnnualDividendRate')
                if isinstance(div_rate, (int, float)) and div_rate > 0 and close_price_num > 0:
                    div_str = f"{round((div_rate / close_price_num) * 100, 2)}%"
                else:
                    div_y = info.get('dividendYield') or info.get('trailingAnnualDividendYield')
                    div_str = f"{round(div_y * 100, 2)}%" if isinstance(div_y, (int, float)) and div_y > 0 else "N/A"

            if fund_data.get('market_cap') and fund_data['market_cap'] != 'N/A':
                market_cap_str = f"{fund_data['market_cap']}億円"
            else:
                yf_mcap = info.get('marketCap')
                market_cap_str = f"約{round(yf_mcap / 100000000, 1)}億円" if isinstance(yf_mcap, (int, float)) and yf_mcap > 0 else "N/A"

            margin_ratio = fund_data.get('margin', 'N/A')
            margin_str = f" | 信用倍率: {margin_ratio}倍" if margin_ratio and margin_ratio != 'N/A' else ""
            earnings_info_str = f" | 決算情報: {fund_data.get('earnings_date', '要検索確認')}"
        else:
            yf_pe = info.get('trailingPE')
            per_str = f"{round(yf_pe, 2)}倍" if isinstance(yf_pe, (int, float)) and pd.notna(yf_pe) else "N/A"
            yf_pbr = info.get('priceToBook')
            pbr_str = f"{round(yf_pbr, 2)}倍" if isinstance(yf_pbr, (int, float)) and pd.notna(yf_pbr) else "N/A"
            div_rate = info.get('dividendRate')
            div_str = f"{round((div_rate / close_price_num) * 100, 2)}%" if isinstance(div_rate, (int, float)) and div_rate > 0 and close_price_num > 0 else "N/A"
            yf_mcap = info.get('marketCap')
            market_cap_str = f"約{round(yf_mcap / 1000000000, 2)} Billion USD" if isinstance(yf_mcap, (int, float)) and yf_mcap > 0 else "N/A"
            margin_str = ""
            earnings_info_str = ""
            us_extra_section = build_us_extra_fundamentals(stock, info, close_price_num)

        edinet_str, edinet_reasons = ("", [])
        news_str = ""
        if is_jp:
            edinet_str, edinet_reasons = get_edinet_documents(stock_code, days=60)
            news_str = get_news(name, True, edinet_reasons)
            edinet_section = f"[EDINET 公式開示情報（直近2ヶ月）]\n{edinet_str}\n"
            order_calendar_section = get_sbi_order_limit_dates()
            market_specific_section = f"{order_calendar_section}"
        else:
            news_str = get_news(name, False)
            edinet_section = ""
            market_specific_section = us_extra_section

        full_text_data = (
            f"【分析対象銘柄：{name}（{stock_code} / {ticker}）】\n\n"
            f"【グローバルマクロ参考値】\n{macro_text}\n\n"
            f"【対象銘柄詳細データ】\n--- 【銘柄: {name} ({ticker}) | 市場区分: {'日本株' if is_jp else '米国株'}】 ---\n"
            f"[基本ファンダメンタルズ]\n時価総額: {market_cap_str} | PER: {per_str} | PBR: {pbr_str} | 配当利回り: {div_str}{margin_str}{earnings_info_str}\n"
            f"{market_specific_section}"
            f"[直近ニュース・話題・アナリスト動向]\n{news_str}\n{edinet_section}"
            f"[テクニカル基本値]\n最新営業日の四本値: 始値 {open_price} | 高値 {high_price} | 安値 {low_price} | 終値 {close_price} {unit_str}\n"
            f"期間別高安値: [直近5日] 高値 {recent_high_5d} / 安値 {recent_low_5d} | [直近20日] 高値 {recent_high_20d} / 安値 {recent_low_20d} | [直近60日] 高値 {recent_high_60d} / 安値 {recent_low_60d} | [52週(250日)] 高値 {high_52w_str} / 安値 {low_52w_str}{from_52w_str}\n"
            f"日足ATR(14日平均値幅): {atr_str} | 出来高20日平均比: {vol_ratio}倍 | 最新RSI: {round(latest_d['RSI'],1) if pd.notna(latest_d.get('RSI')) else 'N/A'} | 最新MACD: {round(latest_d['MACD'],1) if pd.notna(latest_d.get('MACD')) else 'N/A'}\n\n"
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

    st.subheader("📱 ① Geminiチャット送信用の命令文（スキル自動発動＆タイトル反映用）")
    st.markdown("通常のGeminiチャット入力欄にコピー＆ペーストしてください（登録済みスキル `stock-swing-analyzer` が自動発動します）。")

    if data.get("is_jp", True):
        search_focus = f"・重点検索：「{data['name']}（{data['stock_code']}）」の最新決算・業績推移、次回決算日、配当権利月、日次信用残・機関空売り残高、TOPIX定期入替区分（不明時は『区分不明』と記載）"
    else:
        search_focus = f"・重点検索：「{data['name']}（{data['stock_code']}）」の最新四半期決算（英語クエリ `{data['stock_code']} earnings release guidance` 必須）、次回Earnings Date、Form 13F/Form 4、Short Interest推移"

    chat_prompt = (
        f"【株式スイング分析リクエスト：{data['name']}（{data['stock_code']}）】\n"
        "@Google 添付したデータとチャート画像をもとに、スキルの【Step 0】〜【7】に従って分析をお願いします。\n"
        f"{search_focus}\n"
        f"・出力厳守：回答の1行目には必ず `# 📊 【銘柄分析】{data['name']}（{data['stock_code']}）` を出力し、末尾の「7. 登録銘柄用メモ」はHYPER SBI 2仕様上限（全角100文字以内、推奨60〜80文字の1行）で「銘柄名」「証券コード」「IFDOCO」を一切含めずに出力してください。"
    )
    st.code(chat_prompt, language="text")

    st.subheader("📱 ② 個別データ（添付用テキスト）")
    st.markdown("以下のテキストをコピーして、上記の命令文と一緒にGeminiへ貼り付けて送信してください。")
    st.code(data["text"], language="text")

    charts = data["images"]
    st.markdown(f"**📊 {data['name']} のチャート**")
    img_col1, img_col2, img_col3 = st.columns(3)
    if charts['w']:
        with img_col1:
            st.image(charts['w'], use_container_width=True)
    if charts['d']:
        with img_col2:
            st.image(charts['d'], use_container_width=True)
    if charts['h']:
        with img_col3:
            st.image(charts['h'], use_container_width=True)