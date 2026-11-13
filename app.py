# ==============================================================================
# AI株式分析データジェネレーター - app.py (完全版 v2026.11 Rev.9.7 高密度スリム版)
# 実装済仕様：起動時EDINET API自動監視＆案内バナー / OHLC dropna＆分割未調整ヒゲ自動除去 /
#   期間別高安値の到達日付記(浮動小数点安全化) / ザラ場暫定出来高＆前日確定足比併記 /
#   日本株yfinance決算予定日併記・米国株PEG/アナリスト補完 / fast_info & Dividends最終補完 /
#   LSEGレポート併用プロンプト対応 / マクロ13指標表＆マクロ5段チャート(11指標)連動 /
#   世界情勢・日米金融政策・日本の国策RSS(4カテゴリ独立・出典付)統合＆15営業日イベント検索指示 /
#   重要ニュース1〜2件のピンポイント本文深掘り検索指示（好悪材料混在時の実質理由特定） /
#   勝率・第1抵抗線手前利確(1.2〜3.0ATR)・5営業日約定実現性(1.2ATR以内)最優先＆無理な数値合わせ指値禁止 /
#   損切り1.0〜1.6ATR(0.8ATR未満禁止)・高勝率時RR1:1.1以上(標準1:1.2以上、1:1.0未満強制見送り)・15営業日超OCO延長指示 /
#   上場市場抽出＆JPX公式TOPIXウエイトCSV自動追尾によるパッシブ需給区分5段階定量判定(stock_code連携) /
#   週足・日足・1時間足＋15分足クロスリペア＆全営業日四本値双方向完全同期(Step4巻戻し防止＆Step5異常値逆流防止) /
#   分析基準日時(生成日時＋確定/未確定足)刻印・見出し末尾日付・SBIメモ先頭日付・ZIP日付付与・個別チャート日本語タイトル化
# ==============================================================================
from datetime import datetime, time as dt_time, timedelta, timezone
import io, os, re, unicodedata, zipfile
import numpy as np
import pandas as pd
import streamlit as st
import yfinance as yf

from chart_maker import add_indicators, generate_macro_chart_image, generate_safe_chart_image
from data_fetcher import (
    check_edinet_api_status, evaluate_topix_passive_status, get_edinet_documents,
    get_japanese_fundamentals, get_japanese_name, get_macro_data_and_history,
    get_macro_policy_news, get_news, get_sbi_order_limit_dates, get_us_stock_name,
    search_japanese_code_by_name, search_us_ticker_by_name, verify_edinet_api_key,
)

st.set_page_config(page_title="AI株式分析データジェネレーター", layout="wide")

if "generated_data" not in st.session_state:
    st.session_state.generated_data = None
if "cache_cleared_time" not in st.session_state:
    st.session_state.cache_cleared_time = None


@st.cache_data(ttl=3600, show_spinner=False)
def resolve_stock_display_name(code, is_jp_flag):
    if not code:
        return ""
    try:
        return (get_japanese_name(code) if is_jp_flag else get_us_stock_name(code)) or ""
    except Exception:
        return ""


def _normalize_yf_df(df: pd.DataFrame, target_tz: str) -> pd.DataFrame:
    """yfinanceから取得したDataFrameのカラムとタイムゾーンを正規化する（tz-naive現地時間へ統一）。"""
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [col[0] if isinstance(col, tuple) else col for col in df.columns]
    if "Repaired?" in df.columns:
        df = df.drop(columns=["Repaired?"])
    df.index = pd.to_datetime(df.index)
    if df.index.tz is not None:
        try:
            df.index = df.index.tz_convert(target_tz).tz_localize(None)
        except Exception:
            df.index = df.index.tz_localize(None)
    return df[~df.index.duplicated(keep="last")].sort_index()


def _fetch_safe_history(stock, period_str: str, interval_str: str, target_tz: str, use_repair: bool = False) -> pd.DataFrame:
    df = pd.DataFrame()
    for rep in dict.fromkeys([use_repair, False]):
        try:
            df = stock.history(period=period_str, interval=interval_str, auto_adjust=False, repair=rep)
            if df is not None and not df.empty:
                break
        except Exception:
            df = pd.DataFrame()
    return _normalize_yf_df(df, target_tz)


def get_fresh_ticker_and_history(ticker_symbol, is_jp=True, use_repair=False):
    stock = yf.Ticker(ticker_symbol)
    target_tz = "Asia/Tokyo" if is_jp else "America/New_York"
    df_w = _fetch_safe_history(stock, "10y", "1wk", target_tz, use_repair=use_repair)
    df_d = _fetch_safe_history(stock, "2y", "1d", target_tz, use_repair=use_repair)
    df_h = _fetch_safe_history(stock, "1y", "1h", target_tz, use_repair=use_repair)
    return stock, df_w, df_d, df_h


def _extract_yf_earnings_date(stock) -> str:
    try:
        cal = getattr(stock, "calendar", None)
        if isinstance(cal, dict):
            ed = cal.get("Earnings Date")
            target = ed[0] if isinstance(ed, list) and ed else ed
            if target is not None:
                dt_s = target.strftime("%Y-%m-%d") if hasattr(target, "strftime") else str(target)
                return f"{dt_s}頃(yfinance予定)"
    except Exception:
        pass
    return ""


def format_price(val, is_jp):
    if pd.isna(val):
        return "N/A"
    return str(int(round(float(val)))) if is_jp else f"{float(val):.2f}"


def _is_intraday_unconfirmed(df_d, is_jp: bool) -> bool:
    """最新の日足バーが取引時間中（未確定足）かどうかを判定する。"""
    if df_d is None or df_d.empty:
        return False
    try:
        last_bar_date = df_d.index[-1].date()
        tz_name = "Asia/Tokyo" if is_jp else "America/New_York"
        try:
            now_local = pd.Timestamp.now(tz=tz_name)
        except Exception:
            now_local = datetime.now(timezone(timedelta(hours=9 if is_jp else -5)))
        open_t, close_t = (dt_time(9, 0), dt_time(15, 30)) if is_jp else (dt_time(9, 30), dt_time(16, 0))
        return last_bar_date == now_local.date() and open_t <= now_local.time() < close_t
    except Exception:
        return False


def _clean_ohlcv_df(df):
    """OHLCV欠損除去・正値フィルタに加え、株式分割境界の未調整ヒゲ・孤立バッドティックを自動修復する。"""
    if df is None or df.empty:
        return pd.DataFrame()
    cleaned = df.copy()
    req_cols = ["Open", "High", "Low", "Close"]
    for c in req_cols:
        if c not in cleaned.columns:
            return pd.DataFrame()
        cleaned[c] = pd.to_numeric(cleaned[c], errors="coerce")
    cleaned = cleaned.dropna(subset=req_cols)
    cleaned = cleaned[(cleaned["Open"] > 0) & (cleaned["High"] > 0) & (cleaned["Low"] > 0) & (cleaned["Close"] > 0)]
    cleaned["Volume"] = pd.to_numeric(cleaned["Volume"], errors="coerce").fillna(0) if "Volume" in cleaned.columns else 0.0
    if not cleaned.empty:
        cleaned = cleaned[~cleaned.index.duplicated(keep="last")].sort_index()
        if len(cleaned) >= 3:
            # 1) 株式分割未調整等で1バー丸ごと前後中央値から+45%超/-35%超乖離した孤立バーを補正
            med_c = cleaned["Close"].rolling(7, center=True, min_periods=1).median().bfill().ffill()
            whole_spike = ((cleaned["Open"] > med_c * 1.45) & (cleaned["Close"] > med_c * 1.45)) | (
                (cleaned["Open"] < med_c * 0.65) & (cleaned["Close"] < med_c * 0.65)
            )
            if whole_spike.any():
                for c in req_cols:
                    cleaned.loc[whole_spike, c] = med_c.loc[whole_spike]

            # 2) 実体(Open/Close)は正常だがHighまたはLowのみ未調整(例: 1:2分割日のHigh=2倍)の異常ヒゲをクランプ
            body_high = np.maximum(cleaned["Open"], cleaned["Close"])
            body_low = np.minimum(cleaned["Open"], cleaned["Close"])
            med_h = cleaned["High"].rolling(7, center=True, min_periods=1).median().bfill().ffill()
            med_l = cleaned["Low"].rolling(7, center=True, min_periods=1).median().bfill().ffill()

            bad_high = (cleaned["High"] > body_high * 1.25) & (cleaned["High"] > med_h * 1.25)
            if bad_high.any():
                cleaned.loc[bad_high, "High"] = np.maximum(body_high.loc[bad_high], np.minimum(cleaned.loc[bad_high, "High"], med_h.loc[bad_high] * 1.05))

            bad_low = (cleaned["Low"] < body_low * 0.78) & (cleaned["Low"] < med_l * 0.78)
            if bad_low.any():
                cleaned.loc[bad_low, "Low"] = np.minimum(body_low.loc[bad_low], np.maximum(cleaned.loc[bad_low, "Low"], med_l.loc[bad_low] * 0.95))

        cleaned["High"] = cleaned[req_cols].max(axis=1)
        cleaned["Low"] = cleaned[req_cols].min(axis=1)
    return cleaned


def _is_flat_zero_bar(df: pd.DataFrame) -> pd.Series:
    """Volume <= 0 かつ Open == High == Low == Close のダミー横線バーを判定する。"""
    if df is None or df.empty:
        return pd.Series(dtype=bool)
    return (
        (df["Volume"] <= 0)
        & np.isclose(df["Open"], df["High"], rtol=1e-7, atol=1e-6)
        & np.isclose(df["Low"], df["Close"], rtol=1e-7, atol=1e-6)
        & np.isclose(df["Open"], df["Close"], rtol=1e-7, atol=1e-6)
    )


def _resample_subhourly_to_hourly(df_sub: pd.DataFrame, is_jp: bool) -> pd.DataFrame:
    """15分足/5分足から1時間足バケット（floor('1h')）を再構築する。"""
    if df_sub is None or df_sub.empty:
        return pd.DataFrame()
    df_valid = df_sub[df_sub["Volume"] > 0].copy()
    if df_valid.empty:
        df_valid = df_sub.copy()
    if is_jp:
        t_vals = df_valid.index.time
        df_valid = df_valid[(t_vals >= dt_time(9, 0)) & (t_vals <= dt_time(15, 30))]
    if df_valid.empty:
        return pd.DataFrame()

    res = df_valid.groupby(df_valid.index.floor("1h")).agg(
        Open=("Open", "first"), High=("High", "max"), Low=("Low", "min"),
        Close=("Close", "last"), Volume=("Volume", "sum"),
    )
    res = _clean_ohlcv_df(res)
    if is_jp and not res.empty:
        res = res[res.index.hour.isin([9, 10, 11, 12, 13, 14, 15])]
    return res


def repair_and_sync_multi_timeframe_ohlcv(
    stock, df_w: pd.DataFrame, df_d: pd.DataFrame, df_h: pd.DataFrame, is_jp: bool = True, use_repair: bool = False
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    【週足・日足・1時間足の3時間軸クロスリペア＆双方向完全同期エンジン】
    1) 日足(2y)の最新営業日欠落に対し、短期日足(5d/1mo)からの補完を試行。
    2) 1時間足の歯抜け（Volume=0 かつ O=H=L=C の横線ダミー足）を15分足/5分足からリサンプリング修復。
    3) 日足に欠落している営業日があれば、1時間足から日足を合成・追加。
    4) 週足の確定終値・高安値（東証15:30クロージング・オークション反映値）と最新日足・1時間足を同期。
    5) 全共通営業日において、分割未調整スパイクの逆流を防止しつつ日足と1時間足の四本値を双方向で完全一致させる。
    """
    target_tz = "Asia/Tokyo" if is_jp else "America/New_York"
    df_w, df_d, df_h = _clean_ohlcv_df(df_w), _clean_ohlcv_df(df_d), _clean_ohlcv_df(df_h)

    # --- Step 1: 短期日足(5d / 1mo)による最新日足欠落パッチ ---
    latest_known_date = max((c.index[-1].date() for c in (df_h, df_w) if not c.empty), default=None)
    if not df_d.empty and latest_known_date is not None and df_d.index[-1].date() < latest_known_date:
        for short_p in ("5d", "1mo"):
            df_d_short = _clean_ohlcv_df(_fetch_safe_history(stock, short_p, "1d", target_tz, use_repair=use_repair))
            if not df_d_short.empty and df_d_short.index[-1].date() > df_d.index[-1].date():
                for col in df_d.columns:
                    if col not in df_d_short.columns:
                        df_d_short[col] = 0.0
                df_d = pd.concat([df_d, df_d_short[df_d.columns]]).sort_index()
                df_d = _clean_ohlcv_df(df_d[~df_d.index.duplicated(keep="last")])
                break

    # --- Step 2: 1時間足の Volume=0 フラット横線ダミー足の修復（15分足/5分足リサンプリング） ---
    if not df_h.empty:
        flat_mask = _is_flat_zero_bar(df_h)
        day_counts = df_h.groupby(df_h.index.date).size()
        has_short_days = (day_counts.iloc[:-1] < 4).any() if len(day_counts) > 1 else False

        if flat_mask.any() or has_short_days:
            df_sub_resampled = pd.DataFrame()
            for sub_interval in ("15m", "5m", "30m"):
                df_sub_raw = _clean_ohlcv_df(_fetch_safe_history(stock, "60d", sub_interval, target_tz, use_repair=False))
                if not df_sub_raw.empty:
                    df_sub_resampled = _resample_subhourly_to_hourly(df_sub_raw, is_jp=is_jp)
                    if not df_sub_resampled.empty:
                        break

            if not df_sub_resampled.empty:
                combined_idx = df_h.index.union(df_sub_resampled.index).sort_values()
                df_h_merged = df_h.reindex(combined_idx)
                for col in df_h.columns:
                    if col not in ["Open", "High", "Low", "Close", "Volume"]:
                        df_h_merged[col] = df_h_merged[col].fillna(0)

                for ts in df_sub_resampled.index:
                    r_sub = df_sub_resampled.loc[ts]
                    if ts not in df_h.index:
                        for c in ["Open", "High", "Low", "Close", "Volume"]:
                            df_h_merged.loc[ts, c] = r_sub[c]
                    else:
                        r_h = df_h.loc[ts]
                        is_h_flat = r_h["Volume"] <= 0 and np.isclose(r_h["Open"], r_h["High"]) and np.isclose(r_h["Open"], r_h["Low"])
                        if is_h_flat and r_sub["Volume"] > 0:
                            for c in ["Open", "High", "Low", "Close", "Volume"]:
                                df_h_merged.loc[ts, c] = r_sub[c]
                        elif r_sub["Volume"] > 0:
                            body_top = max(float(r_h["Open"]), float(r_h["Close"]))
                            body_bot = min(float(r_h["Open"]), float(r_h["Close"]))
                            if float(r_sub["High"]) <= body_top * 1.20:
                                df_h_merged.loc[ts, "High"] = max(float(r_h["High"]), float(r_sub["High"]))
                            if float(r_sub["Low"]) >= body_bot * 0.82:
                                df_h_merged.loc[ts, "Low"] = min(float(r_h["Low"]), float(r_sub["Low"]))
                            df_h_merged.loc[ts, "Volume"] = max(float(r_h["Volume"]), float(r_sub["Volume"]))
                df_h = _clean_ohlcv_df(df_h_merged)

        if not df_h.empty:
            drop_ts_list = []
            for _, grp in df_h.groupby(df_h.index.date):
                if len(grp) > 1:
                    f_mask = _is_flat_zero_bar(grp)
                    if (~f_mask).any() and f_mask.any():
                        drop_ts_list.extend(list(grp[f_mask].index))
            if drop_ts_list:
                df_h = df_h.drop(index=drop_ts_list)

    # --- Step 3: 1時間足に存在するが日足に存在しない営業日を日足へ合成・追加 ---
    synthesized_dates = set()
    if not df_h.empty:
        existing_d_dates = set(df_d.index.date) if not df_d.empty else set()
        new_d_rows = []
        for h_date, grp in df_h.groupby(df_h.index.date):
            if h_date not in existing_d_dates and not grp.empty:
                row_data = {
                    "Open": float(grp["Open"].iloc[0]), "High": float(grp["High"].max()),
                    "Low": float(grp["Low"].min()), "Close": float(grp["Close"].iloc[-1]),
                    "Volume": float(grp["Volume"].sum()),
                }
                if not df_d.empty:
                    for col in df_d.columns:
                        if col not in row_data:
                            row_data[col] = 0.0
                new_d_rows.append((pd.Timestamp(h_date), row_data))
                synthesized_dates.add(h_date)

        if new_d_rows:
            add_d_df = pd.DataFrame([r[1] for r in new_d_rows], index=[r[0] for r in new_d_rows])
            df_d = pd.concat([df_d, add_d_df]).sort_index()
            df_d = _clean_ohlcv_df(df_d[~df_d.index.duplicated(keep="last")])

    # --- Step 4: 週足(df_w)との大引け確定値双方向同期 ---
    if not df_d.empty and not df_w.empty:
        last_d_ts = df_d.index[-1]
        last_d_date = last_d_ts.date()
        is_intraday = _is_intraday_unconfirmed(df_d, is_jp)
        last_week_monday = last_d_date - timedelta(days=last_d_date.weekday())
        w_last_ts = df_w.index[-1]

        if w_last_ts.date() >= last_week_monday:
            w_close = float(df_w.loc[w_last_ts, "Close"])
            w_high = float(df_w.loc[w_last_ts, "High"])
            w_low = float(df_w.loc[w_last_ts, "Low"])

            # 最新日足が1時間足からの新規合成ではなく、かつ確定状態で微小差(1.5%未満)の場合のみ週足確定終値を採用
            if not is_intraday and last_d_date not in synthesized_dates and w_close > 0:
                d_close = float(df_d.loc[last_d_ts, "Close"])
                if not np.isclose(d_close, w_close) and abs(d_close - w_close) / max(w_close, 1.0) < 0.015:
                    df_d.loc[last_d_ts, "Close"] = w_close
                    df_d.loc[last_d_ts, "High"] = max(float(df_d.loc[last_d_ts, "High"]), w_close)
                    df_d.loc[last_d_ts, "Low"] = min(float(df_d.loc[last_d_ts, "Low"]), w_close)

            week_mask = df_d.index.date >= last_week_monday
            if week_mask.any():
                week_sub = df_d.loc[week_mask]
                w_sub_max, w_sub_min = float(week_sub["High"].max()), float(week_sub["Low"].min())
                df_w.loc[w_last_ts, "High"] = w_sub_max if w_high > w_sub_max * 1.22 else max(w_high, w_sub_max)
                df_w.loc[w_last_ts, "Low"] = w_sub_min if w_low < w_sub_min * 0.80 else min(w_low, w_sub_min)
                df_w.loc[w_last_ts, "Close"] = float(week_sub["Close"].iloc[-1])

    # --- Step 5: 全共通営業日における 日足(df_d) ⇔ 1時間足(df_h) の双方向四本値同期（異常値逆流ブロック付） ---
    if not df_d.empty and not df_h.empty:
        d_date_to_ts = {ts.date(): ts for ts in df_d.index}
        last_d_ts = df_d.index[-1]
        is_intraday = _is_intraday_unconfirmed(df_d, is_jp)

        for h_date, grp in df_h.groupby(df_h.index.date):
            if h_date not in d_date_to_ts or grp.empty:
                continue
            d_ts = d_date_to_ts[h_date]
            d_open, d_high = float(df_d.loc[d_ts, "Open"]), float(df_d.loc[d_ts, "High"])
            d_low, d_close = float(df_d.loc[d_ts, "Low"]), float(df_d.loc[d_ts, "Close"])
            d_vol = float(df_d.loc[d_ts, "Volume"])
            d_body_top, d_body_bot = max(d_open, d_close), min(d_open, d_close)

            h_first_ts, h_last_ts = grp.index[0], grp.index[-1]
            h_max_ts, h_min_ts = grp["High"].idxmax(), grp["Low"].idxmin()
            h_high, h_low, h_vol_sum = float(grp["High"].max()), float(grp["Low"].min()), float(grp["Volume"].sum())

            # 一方の時間軸のみ実体上限から+22%超突出している場合は分割未調整ノイズとみなし正常側を採用
            if d_high > d_body_top * 1.22 and h_high <= d_body_top * 1.22:
                true_high = max(h_high, d_body_top)
            elif h_high > d_body_top * 1.22 and d_high <= d_body_top * 1.22:
                true_high = max(d_high, d_body_top)
                df_h.loc[grp.index, "High"] = np.minimum(df_h.loc[grp.index, "High"], true_high)
            else:
                true_high = max(d_high, h_high, d_body_top)

            if d_low < d_body_bot * 0.80 and h_low >= d_body_bot * 0.80:
                true_low = min(h_low, d_body_bot)
            elif h_low < d_body_bot * 0.80 and d_low >= d_body_bot * 0.80:
                true_low = min(d_low, d_body_bot)
                df_h.loc[grp.index, "Low"] = np.maximum(df_h.loc[grp.index, "Low"], true_low)
            else:
                true_low = min(d_low, h_low, d_body_bot)

            df_d.loc[d_ts, "High"] = true_high
            df_d.loc[d_ts, "Low"] = true_low

            df_h.loc[h_first_ts, "Open"] = d_open
            df_h.loc[h_first_ts, "High"] = max(float(df_h.loc[h_first_ts, "High"]), d_open)
            df_h.loc[h_first_ts, "Low"] = min(float(df_h.loc[h_first_ts, "Low"]), d_open)

            if d_ts == last_d_ts and is_intraday:
                df_d.loc[d_ts, "Close"] = float(df_h.loc[h_last_ts, "Close"])
            else:
                df_h.loc[h_last_ts, "Close"] = d_close
                df_h.loc[h_last_ts, "High"] = max(float(df_h.loc[h_last_ts, "High"]), d_close)
                df_h.loc[h_last_ts, "Low"] = min(float(df_h.loc[h_last_ts, "Low"]), d_close)

            if true_high > float(df_h.loc[grp.index, "High"].max()):
                df_h.loc[h_max_ts, "High"] = true_high
            if true_low < float(df_h.loc[grp.index, "Low"].min()):
                if abs(d_close - true_low) < abs(float(df_h.loc[h_min_ts, "Low"]) - true_low):
                    df_h.loc[h_last_ts, "Low"] = min(float(df_h.loc[h_last_ts, "Low"]), true_low)
                else:
                    df_h.loc[h_min_ts, "Low"] = true_low

            if d_vol <= 0 and h_vol_sum > 0:
                df_d.loc[d_ts, "Volume"] = h_vol_sum

    return _clean_ohlcv_df(df_w), _clean_ohlcv_df(df_d), _clean_ohlcv_df(df_h)


def _get_period_extreme_with_date(series: pd.Series, window: int, is_high: bool, is_jp: bool, include_year: bool = False) -> tuple:
    """指定期間における最高値/最安値の数値、価格文字列、直近到達日付きラベルを返す。"""
    sub = series.tail(window).dropna()
    if sub.empty:
        return pd.NA, "N/A", "N/A"
    target_val = float(sub.max()) if is_high else float(sub.min())
    matched = sub[np.isclose(sub.astype(float), target_val, rtol=1e-9, atol=1e-9)]
    dt_idx = matched.index[-1] if not matched.empty else (sub.idxmax() if is_high else sub.idxmin())
    dt_str = dt_idx.strftime("%y/%m/%d" if include_year else "%m/%d") if hasattr(dt_idx, "strftime") else str(dt_idx)[:10]
    price_str = format_price(target_val, is_jp)
    return target_val, price_str, f"{price_str}({dt_str})"


def _fallback_dividend_yield(df_d: pd.DataFrame, close_price_num: float) -> str:
    """df_d['Dividends'] の過去365日実績合計から配当利回りを算出する。"""
    if "Dividends" in df_d.columns and close_price_num > 0:
        try:
            one_yr_ago = df_d.index[-1] - pd.Timedelta(days=365)
            recent_divs = df_d.loc[df_d.index >= one_yr_ago, "Dividends"]
            div_sum = float(recent_divs[recent_divs > 0].sum())
            if div_sum > 0:
                return f"{round((div_sum / close_price_num) * 100, 2)}%(実績)"
        except Exception:
            pass
    return "N/A"


def _fallback_fast_mcap(stock, is_jp: bool) -> str:
    """stock.fast_info から時価総額を補完取得する。"""
    try:
        f_mcap = stock.fast_info.get("market_cap") or getattr(stock.fast_info, "market_cap", None)
        if isinstance(f_mcap, (int, float)) and f_mcap > 0:
            return f"{round(f_mcap / 1e8, 1):,}億円" if is_jp else f"{round(f_mcap / 1e9, 2)} Billion USD"
    except Exception:
        pass
    return "N/A"


def _build_relative_performance_line(df_d: pd.DataFrame, macro_histories: dict, is_jp: bool) -> str:
    """対象銘柄の5D/20D/60D騰落率と、主要ベンチマークに対する超過リターンを算出する。"""
    if df_d is None or df_d.empty or "Close" not in df_d.columns:
        return ""
    s_c = df_d["Close"].dropna()
    if len(s_c) < 6:
        return ""

    def _calc_ret(series, days):
        if series is not None and len(series) > days:
            prev, curr = float(series.iloc[-(days + 1)]), float(series.iloc[-1])
            if prev > 0:
                return ((curr - prev) / prev) * 100.0
        return None

    def _idx_ret20(key):
        m_df = macro_histories.get(key, pd.DataFrame()) if isinstance(macro_histories, dict) else pd.DataFrame()
        return _calc_ret(m_df["Close"].dropna(), 20) if isinstance(m_df, pd.DataFrame) and "Close" in m_df.columns else None

    r5, r20, r60 = _calc_ret(s_c, 5), _calc_ret(s_c, 20), _calc_ret(s_c, min(59, len(s_c) - 1))
    _fmt_r = lambda v: f"{v:+.2f}%" if v is not None else "N/A"
    base_str = f"銘柄騰落率: 5D={_fmt_r(r5)} / 20D={_fmt_r(r20)} / 60D={_fmt_r(r60)}"
    if not isinstance(macro_histories, dict):
        return base_str + "\n"

    if is_jp:
        n20, t20 = _idx_ret20("^N225"), _idx_ret20("1306.T")
        vs_parts = []
        if r20 is not None and n20 is not None:
            vs_parts.append(f"対日経225差(20D): {r20 - n20:+.2f}%")
        if r20 is not None and t20 is not None:
            vs_parts.append(f"対TOPIX[1306.T]差(20D): {r20 - t20:+.2f}%")
        if vs_parts:
            return f"{base_str} | {' / '.join(vs_parts)}\n"
    else:
        sox20 = _idx_ret20("^SOX")
        if r20 is not None and sox20 is not None:
            return f"{base_str} | 対SOX指数差(20D): {r20 - sox20:+.2f}%\n"

    return base_str + "\n"


def build_us_extra_fundamentals(stock, info, close_price_num):
    def fmt_pct(key):
        val = info.get(key)
        return f"{round(val * 100, 2)}%" if isinstance(val, (int, float)) and pd.notna(val) else "N/A"

    def fmt_num(key, digits=2):
        val = info.get(key)
        return f"{round(val, digits)}" if isinstance(val, (int, float)) and pd.notna(val) else "N/A"

    peg_str = fmt_num("pegRatio")
    if peg_str == "N/A":
        peg_str = fmt_num("trailingPegRatio")

    rec_key = str(info.get("recommendationKey") or "").strip()
    if not rec_key or rec_key.lower() == "none":
        n_analysts = info.get("numberOfAnalystOpinions")
        rec_key = f"評価なし(目標株価算出:{n_analysts}名)" if n_analysts else "N/A"

    target_mean = info.get("targetMeanPrice")
    if isinstance(target_mean, (int, float)) and pd.notna(target_mean) and close_price_num > 0:
        upside = round(((target_mean - close_price_num) / close_price_num) * 100, 2)
        target_str = (
            f"平均 {target_mean}ドル (現在値比 {'+' if upside > 0 else ''}{upside}%"
            f" | 上限 {info.get('targetHighPrice')} / 下限 {info.get('targetLowPrice')})"
        )
    else:
        target_str = "N/A"

    yf_news_lines = []
    try:
        raw_news = getattr(stock, "news", None)
        if isinstance(raw_news, list):
            for item in raw_news[:5]:
                if isinstance(item, dict):
                    content = item.get("content") if isinstance(item.get("content"), dict) else {}
                    title = content.get("title") or item.get("title")
                    if title:
                        yf_news_lines.append(f"・{title}")
    except Exception:
        pass
    yf_news_block = "[米国現地ニュースヘッドライン (yfinance)]\n" + "\n".join(yf_news_lines) + "\n" if yf_news_lines else ""

    return (
        "[米国株 詳細指標・成長性]\n"
        f"予想PER(Forward): {fmt_num('forwardPE')} | PEGレシオ: {peg_str}"
        f" | ROE: {fmt_pct('returnOnEquity')} | 売上成長率: {fmt_pct('revenueGrowth')}"
        f" | 営業利益率: {fmt_pct('operatingMargins')} | Beta(感応度): {fmt_num('beta')}\n"
        "[米国株 需給・空売り動向・コンセンサス（※空売り残高は月2回公表のためタイムラグに注意）]\n"
        f"浮動株空売り比率(Short % of Float): {fmt_pct('shortPercentOfFloat')} |"
        f" 空売りカバー日数(Days to Cover): {fmt_num('shortRatio')}日\n"
        f"機関投資家保有比率: {fmt_pct('heldPercentInstitutions')} |"
        f" インサイダー保有比率: {fmt_pct('heldPercentInsiders')}\n"
        f"アナリスト評価: {rec_key} | 目標株価コンセンサス: {target_str}\n"
        f"{yf_news_block}"
    )


def build_technical_summary_text(df_d, is_jp, is_intraday=False):
    unit = "円" if is_jp else "ドル"
    close_series = df_d["Close"]
    vol_avg20_series = df_d["Volume"].rolling(20).mean()

    sma_lines = []
    for window in [25, 75, 200]:
        if len(close_series) >= window:
            sma_series = close_series.rolling(window).mean()
            curr_sma = sma_series.iloc[-1]
            prev_sma = sma_series.iloc[-6] if len(close_series) >= window + 5 else pd.NA
            if pd.notna(curr_sma):
                curr_close = close_series.iloc[-1]
                dev_pct = round(((curr_close - curr_sma) / curr_sma) * 100, 2)
                dev_str = f"{'+' if dev_pct > 0 else ''}{dev_pct}%"
                if pd.notna(prev_sma):
                    diff = curr_sma - prev_sma
                    diff_fmt = int(round(diff)) if is_jp else round(diff, 2)
                    slope_str = (
                        f"上昇中(5日前比 +{diff_fmt}{unit})" if diff > 0
                        else (f"下降中(5日前比 {diff_fmt}{unit})" if diff < 0 else "横ばい(5日前比 ±0)")
                    )
                else:
                    slope_str = "傾き算出不可"
                sma_lines.append(f"SMA{window}: {format_price(curr_sma, is_jp)}{unit} [現在値乖離: {dev_str} | 傾き: {slope_str}]")
            else:
                sma_lines.append(f"SMA{window}: N/A")
        else:
            sma_lines.append(f"SMA{window}: N/A (データ期間不足)")

    recent_df = df_d.tail(10)
    recent_vol_avg = vol_avg20_series.tail(10)
    last_idx = recent_df.index[-1] if not recent_df.empty else None

    table_rows = ["日付 | 始値 | 高値(上ヒゲ) | 安値(下ヒゲ) | 終値 | 出来高比(20日平均比) | RSI | MACD"]
    for idx, row in recent_df.iterrows():
        date_str = idx.strftime("%m/%d") if hasattr(idx, "strftime") else str(idx)[:10]
        v_avg = recent_vol_avg.loc[idx] if idx in recent_vol_avg.index else pd.NA
        if pd.notna(v_avg) and v_avg > 0:
            v_ratio = f"{round(row['Volume'] / v_avg, 2)}倍" + ("(ザラ場暫定)" if is_intraday and idx == last_idx else "")
        else:
            v_ratio = "N/A"

        rsi_val = f"{round(row['RSI'], 1)}" if "RSI" in row and pd.notna(row["RSI"]) else "N/A"
        macd_val = f"{round(row['MACD'], 1)}" if "MACD" in row and pd.notna(row["MACD"]) else "N/A"
        table_rows.append(
            f"{date_str} | {format_price(row.get('Open'), is_jp)} | {format_price(row.get('High'), is_jp)} |"
            f" {format_price(row.get('Low'), is_jp)} | {format_price(row.get('Close'), is_jp)} | {v_ratio} | {rsi_val} | {macd_val}"
        )

    return "\n".join(sma_lines), "\n".join(table_rows)


# 起動時EDINET API自動監視チェック
edinet_status = check_edinet_api_status(force_refresh=False)

st.title("📈 AI株式分析データジェネレーター")
st.markdown("このアプリは、Gemini（Web版）に読み込ませるための**最新データとチャート画像4枚（週足・日足・1時間足・マクロ統合）**を瞬時に生成・パッケージ化します。")

if not edinet_status.get("ok", False):
    st.warning(
        f"⚠️ **【EDINET API 状態監視アラート】** {edinet_status.get('message', '')}\n\n"
        f"💡 **対処方法**: {edinet_status.get('guide', '')}"
    )

with st.sidebar:
    st.subheader("🎯 銘柄選択・実行")
    market_filter = st.radio(
        "検索対象市場", ["日本株のみ", "すべて（日米）", "米国株のみ"],
        index=0, horizontal=True, help="日本株の英字社名（MTGなど）が米国株と混同されるのを防ぎます。",
    )
    raw_input = st.text_input("銘柄コード または 企業名（日米対応）", value="6844").strip()

    jp_code_pattern = r"^[0-9]{3}[0-9A-Za-z]$"
    stock_code, is_jp, selected_label_name = "", False, ""

    if re.match(jp_code_pattern, raw_input):
        stock_code, is_jp = raw_input.upper(), True
    elif raw_input:
        jp_matches, us_matches = [], []
        if market_filter in ["日本株のみ", "すべて（日米）"]:
            half_upper = unicodedata.normalize("NFKC", raw_input).upper()
            full_upper = half_upper.translate(
                str.maketrans("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ", "０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ")
            )
            for q in dict.fromkeys([raw_input, half_upper, full_upper]):
                for item in search_japanese_code_by_name(q):
                    if item not in jp_matches:
                        jp_matches.append(item)

        if market_filter in ["すべて（日米）", "米国株のみ"] and re.search(r"[A-Za-z]", raw_input):
            us_matches = search_us_ticker_by_name(raw_input)

        all_matches = jp_matches + us_matches
        if all_matches:
            selected = st.selectbox("候補を選択:", all_matches)
            parts = selected.split(" - ", 1)
            stock_code = parts[0].strip().upper()
            selected_label_name = parts[1].strip() if len(parts) > 1 else ""
            is_jp = bool(re.match(jp_code_pattern, stock_code))
        elif market_filter != "日本株のみ" and re.match(r"^[A-Za-z.\-]+$", raw_input):
            stock_code, is_jp = raw_input.upper(), False
        else:
            st.warning("該当する銘柄が見つかりませんでした。")

    if stock_code:
        display_name = selected_label_name or resolve_stock_display_name(stock_code, is_jp)
        market_badge = "日本株" if is_jp else "米国株"
        if display_name and display_name != stock_code:
            st.success(f"🎯 **[{stock_code}] {display_name}** ({market_badge})")
        else:
            st.info(f"🎯 **[{stock_code}]** ({market_badge})")

    generate_button = st.button("🚀 データ収集＆画像生成スタート", type="primary", width="stretch", disabled=not bool(stock_code))

    if is_jp and stock_code:
        st.caption(
            f"🔗 **外部調査**: [Yahoo](https://finance.yahoo.co.jp/quote/{stock_code}.T) ｜"
            f" [株探(開示)](https://kabutan.jp/stock/news?code={stock_code}&b=k) ｜"
            f" [株探(財務)](https://kabutan.jp/stock/finance?code={stock_code}) ｜"
            f" [TradingView](https://jp.tradingview.com/chart/?symbol=TSE%3A{stock_code}) ｜"
            " [SBI証券](https://www.sbisec.co.jp/)"
        )
    elif not is_jp and stock_code:
        st.caption(
            f"🔗 **外部調査**: [TradingView](https://jp.tradingview.com/chart/?symbol={stock_code.upper()}) ｜"
            f" [Yahoo(US)](https://finance.yahoo.com/quote/{stock_code.upper()}) ｜"
            f" [Finviz](https://finviz.com/quote.ashx?t={stock_code.upper()}) ｜"
            " [SBI証券](https://www.sbisec.co.jp/)"
        )

    st.caption(f"📡 **EDINET連携**: {edinet_status.get('message', '')}")

    with st.expander("⚙️ 取得補正・キャッシュ・API診断", expanded=False):
        use_repair = st.checkbox(
            "株式分割バグ・異常値を自動補正 (repair=True)", value=False,
            help="株式分割直後のスパイクノイズがある場合のみオンにしてください。通常時はオフの方が高速かつ安定します。",
        )
        if st.button("🔄 キャッシュをクリアして初期化", width="stretch"):
            st.cache_data.clear()
            for k in list(st.session_state.keys()):
                if k.startswith("fund_cache_") or k == "_edinet_api_status_cache":
                    del st.session_state[k]
            st.session_state.generated_data = None
            st.session_state.cache_cleared_time = datetime.now(timezone(timedelta(hours=9))).strftime("%H:%M:%S")
            st.rerun()

        if st.button("🔑 EDINET APIキー接続テスト（即時再検証）", width="stretch"):
            ok_flag, diag_msg = verify_edinet_api_key()
            if ok_flag:
                st.success(diag_msg)
            else:
                st.error(diag_msg)

    if st.session_state.cache_cleared_time:
        st.caption(f"✅ キャッシュクリア済 ({st.session_state.cache_cleared_time})")

if generate_button and stock_code:
    st.session_state.generated_data = None
    st.session_state.cache_cleared_time = None
    ticker = f"{stock_code}.T" if is_jp else stock_code.upper()

    with st.spinner("市場データ・マクロボラティリティ(13指標)・世界情勢/政策ヘッドラインの取得とチャート画像4枚を生成中..."):
        macro_text, macro_histories = get_macro_data_and_history()
        macro_policy_news_str = get_macro_policy_news()
        stock, df_w, df_d, df_h = get_fresh_ticker_and_history(ticker, is_jp=is_jp, use_repair=use_repair)
        name = (get_japanese_name(stock_code) or stock_code) if is_jp else get_us_stock_name(stock_code)

        # 3時間軸＋15分足クロスリペア＆全営業日双方向四本値同期エンジン（分割未調整ヒゲ除去付）
        df_w, df_d, df_h = repair_and_sync_multi_timeframe_ohlcv(
            stock=stock, df_w=df_w, df_d=df_d, df_h=df_h, is_jp=is_jp, use_repair=use_repair
        )

        if df_d.empty:
            st.error("株価データが取得できませんでした。")
            st.stop()

        df_w, df_d, df_h = add_indicators(df_w), add_indicators(df_d), add_indicators(df_h)

        # 生成日時・株価基準日・確定状態の算出
        now_jst = datetime.now(timezone(timedelta(hours=9)))
        wd_jp = ["月", "火", "水", "木", "金", "土", "日"][now_jst.weekday()]
        gen_date_slash = now_jst.strftime("%Y/%m/%d")
        gen_date_short = now_jst.strftime("%m/%d")
        gen_date_file = now_jst.strftime("%Y%m%d")
        gen_dt_label = f"{gen_date_slash}({wd_jp}) {now_jst.strftime('%H:%M')} JST"

        latest_d = df_d.iloc[-1]
        latest_date_str = df_d.index[-1].strftime("%Y-%m-%d") if hasattr(df_d.index[-1], "strftime") else str(df_d.index[-1])[:10]
        is_intraday = _is_intraday_unconfirmed(df_d, is_jp)
        bar_confirm_status = "ザラ場立会中・未確定足" if is_intraday else "大引け確定足"
        analysis_basis_str = f"{gen_dt_label}（株価基準日: {latest_date_str} [{bar_confirm_status}]）"

        for temp_file in ["temp_weekly.png", "temp_daily.png", "temp_hourly.png", "temp_macro.png"]:
            if os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except Exception:
                    pass

        # 個別3チャートタイトルを日本語（週足/日足/1時間足）＋日付入りで描画（第5引数の判定キーは維持）
        chart_date_tag = f"[{gen_date_slash}生成 / 基準:{latest_date_str}]"
        img_w = generate_safe_chart_image(df_w, "temp_weekly.png", f"{name} ({stock_code}) 週足 {chart_date_tag}", 260, "weekly", is_jp=is_jp)
        img_d = generate_safe_chart_image(df_d, "temp_daily.png", f"{name} ({stock_code}) 日足 {chart_date_tag}", 130, "daily", is_jp=is_jp)
        img_h = generate_safe_chart_image(df_h, "temp_hourly.png", f"{name} ({stock_code}) 1時間足 {chart_date_tag}", 130, "hourly", is_jp=is_jp)
        img_m = generate_macro_chart_image(macro_histories, "temp_macro.png", tail_days=60)

        unit_str = "円" if is_jp else "ドル"

        open_price = format_price(latest_d.get("Open"), is_jp)
        high_price = format_price(latest_d.get("High"), is_jp)
        low_price = format_price(latest_d.get("Low"), is_jp)
        close_price_num = int(round(float(latest_d["Close"]))) if is_jp else round(float(latest_d["Close"]), 2)
        close_price = format_price(latest_d.get("Close"), is_jp)

        _, _, recent_high_5d_lbl = _get_period_extreme_with_date(df_d["High"], 5, True, is_jp, False)
        _, _, recent_low_5d_lbl = _get_period_extreme_with_date(df_d["Low"], 5, False, is_jp, False)
        _, _, recent_high_20d_lbl = _get_period_extreme_with_date(df_d["High"], 20, True, is_jp, False)
        _, _, recent_low_20d_lbl = _get_period_extreme_with_date(df_d["Low"], 20, False, is_jp, False)
        _, _, recent_high_60d_lbl = _get_period_extreme_with_date(df_d["High"], 60, True, is_jp, False)
        _, _, recent_low_60d_lbl = _get_period_extreme_with_date(df_d["Low"], 60, False, is_jp, False)
        high_52w_val, _, high_52w_lbl = _get_period_extreme_with_date(df_d["High"], 250, True, is_jp, True)
        low_52w_val, _, low_52w_lbl = _get_period_extreme_with_date(df_d["Low"], 250, False, is_jp, True)

        from_52w_str = (
            f" (52週高値比 {round(((close_price_num - high_52w_val) / high_52w_val) * 100, 2)}%)"
            if pd.notna(high_52w_val) and high_52w_val > 0 else ""
        )

        atr_val = latest_d.get("ATR")
        atr_str = (
            f"{format_price(atr_val, is_jp)}{unit_str} ({round((float(atr_val) / close_price_num) * 100, 2)}%)"
            if pd.notna(atr_val) and close_price_num > 0 else "N/A"
        )

        bar_status_tag = " [※ザラ場立会中・未確定足]" if is_intraday else ""
        vol_avg20 = df_d["Volume"].tail(20).mean()
        vol_ratio = round(float(latest_d["Volume"]) / float(vol_avg20), 2) if pd.notna(vol_avg20) and vol_avg20 > 0 else 1.0

        if is_intraday and len(df_d) >= 2:
            prev_vol = float(df_d["Volume"].iloc[-2])
            prev_avg20 = float(df_d["Volume"].iloc[:-1].tail(20).mean()) if len(df_d) >= 21 else float(vol_avg20)
            prev_ratio = round(prev_vol / prev_avg20, 2) if pd.notna(prev_avg20) and prev_avg20 > 0 else 1.0
            vol_ratio_str = f"{vol_ratio}倍(ザラ場暫定 / 前日確定足: {prev_ratio}倍)"
        elif is_intraday:
            vol_ratio_str = f"{vol_ratio}倍(ザラ場暫定)"
        else:
            vol_ratio_str = f"{vol_ratio}倍"

        rel_perf_line = _build_relative_performance_line(df_d, macro_histories, is_jp)
        sma_summary_text, recent_10d_table_text = build_technical_summary_text(df_d, is_jp, is_intraday=is_intraday)

        try:
            raw_info = stock.info
            info = raw_info if isinstance(raw_info, dict) else {}
        except Exception:
            info = {}

        yf_earn_str = _extract_yf_earnings_date(stock)
        us_extra_section, market_seg = "", "N/A"

        if is_jp:
            fund_data = get_japanese_fundamentals(stock_code)
            market_seg = fund_data.get("market_segment", "N/A")
            if fund_data.get("per") and fund_data["per"] != "N/A":
                per_str = f"{fund_data['per']}倍" if fund_data["per"][0].isdigit() else fund_data["per"]
            else:
                yf_pe = info.get("forwardPE") or info.get("trailingPE")
                per_str = f"{round(yf_pe, 2)}倍" if isinstance(yf_pe, (int, float)) and pd.notna(yf_pe) else "N/A"

            if fund_data.get("pbr") and fund_data["pbr"] != "N/A":
                pbr_str = f"{fund_data['pbr']}倍"
            else:
                yf_pbr = info.get("priceToBook")
                pbr_str = f"{round(yf_pbr, 2)}倍" if isinstance(yf_pbr, (int, float)) and pd.notna(yf_pbr) else "N/A"

            if fund_data.get("div") and fund_data["div"] != "N/A":
                div_str = f"{fund_data['div']}%"
            else:
                div_rate = info.get("dividendRate") or info.get("trailingAnnualDividendRate")
                if isinstance(div_rate, (int, float)) and div_rate > 0 and close_price_num > 0:
                    div_str = f"{round((div_rate / close_price_num) * 100, 2)}%"
                else:
                    div_y = info.get("dividendYield") or info.get("trailingAnnualDividendYield")
                    div_str = f"{round(div_y * 100, 2)}%" if isinstance(div_y, (int, float)) and div_y > 0 else "N/A"

            if fund_data.get("market_cap") and fund_data["market_cap"] != "N/A":
                market_cap_str = f"{fund_data['market_cap']}億円"
            else:
                yf_mcap = info.get("marketCap")
                market_cap_str = f"{round(yf_mcap / 1e8, 1):,}億円" if isinstance(yf_mcap, (int, float)) and yf_mcap > 0 else "N/A"

            margin_ratio = fund_data.get("margin", "N/A")
            margin_str = f" | 信用倍率: {margin_ratio}倍" if margin_ratio and margin_ratio != "N/A" else ""
            ed_raw = fund_data.get("earnings_date", "要検索確認")
            if ed_raw == "要検索確認" and yf_earn_str:
                ed_raw = yf_earn_str
            elif yf_earn_str and ("本決算" in ed_raw or "決算日:" in ed_raw):
                ed_raw = f"{ed_raw} / {yf_earn_str}"
            earnings_info_str = f" | 決算情報: {ed_raw}"
        else:
            yf_pe, yf_pbr = info.get("trailingPE"), info.get("priceToBook")
            per_str = f"{round(yf_pe, 2)}倍" if isinstance(yf_pe, (int, float)) and pd.notna(yf_pe) else "N/A"
            pbr_str = f"{round(yf_pbr, 2)}倍" if isinstance(yf_pbr, (int, float)) and pd.notna(yf_pbr) else "N/A"
            div_rate, yf_mcap = info.get("dividendRate"), info.get("marketCap")
            div_str = f"{round((div_rate / close_price_num) * 100, 2)}%" if isinstance(div_rate, (int, float)) and div_rate > 0 and close_price_num > 0 else "N/A"
            market_cap_str = f"{round(yf_mcap / 1e9, 2)} Billion USD" if isinstance(yf_mcap, (int, float)) and yf_mcap > 0 else "N/A"
            margin_str = ""
            earnings_info_str = f" | 次回決算日: {yf_earn_str}" if yf_earn_str else ""
            us_extra_section = build_us_extra_fundamentals(stock, info, close_price_num)

        # 日米共通の最終フォールバック補完（Dividends実績利回り ＆ fast_info時価総額）
        if div_str == "N/A":
            div_str = _fallback_dividend_yield(df_d, close_price_num)
        if market_cap_str == "N/A":
            market_cap_str = _fallback_fast_mcap(stock, is_jp)

        if is_jp:
            # ※fast_infoによる時価総額最終補完の「後」にJPX公式TOPIX CSV照合＋5段階定量判定を実行する
            topix_passive_str = evaluate_topix_passive_status(market_seg, market_cap_str, stock_code)
            topix_section = f"[上場市場・TOPIXパッシブ需給区分]\n{topix_passive_str}\n"
            edinet_str, edinet_reasons = get_edinet_documents(stock_code, days=60)
            news_str = get_news(name, True, edinet_reasons)
            edinet_section = f"[EDINET 公式開示情報 / 有報財務データ]\n{edinet_str}\n"
            market_specific_section = get_sbi_order_limit_dates()
            market_header_label = f"日本株（{market_seg}）" if market_seg and market_seg != "N/A" else "日本株"
        else:
            topix_section, edinet_section = "", ""
            news_str = get_news(name, False)
            market_specific_section = us_extra_section
            market_header_label = "米国株"

        full_text_data = (
            f"【分析対象銘柄：{name}（{stock_code} / {ticker}）】\n"
            f"【データ生成・分析基準日時：{analysis_basis_str}】\n\n"
            f"【グローバルマクロ＆ボラティリティ参考値】\n{macro_text}\n\n"
            f"[リアルタイム世界情勢・日米金融政策・日本の国策ヘッドライン]\n{macro_policy_news_str}\n\n"
            f"【対象銘柄詳細データ】\n--- 【銘柄: {name} ({ticker}) | 市場区分: {market_header_label} | 分析基準日時: {analysis_basis_str}】 ---\n"
            f"[基本ファンダメンタルズ]\n時価総額: {market_cap_str} | PER: {per_str}"
            f" | PBR: {pbr_str} | 配当利回り: {div_str}{margin_str}{earnings_info_str}\n"
            f"{topix_section}"
            f"{market_specific_section}"
            f"[直近ニュース・話題・アナリスト動向]\n{news_str}\n{edinet_section}"
            f"[テクニカル基本値]\n最新営業日({latest_date_str})の四本値{bar_status_tag}:"
            f" 始値 {open_price} | 高値 {high_price} | 安値 {low_price} | 終値 {close_price} {unit_str}\n"
            f"{rel_perf_line}"
            f"期間別高安値: [直近5日] 高値 {recent_high_5d_lbl} / 安値 {recent_low_5d_lbl}"
            f" | [直近20日] 高値 {recent_high_20d_lbl} / 安値 {recent_low_20d_lbl}"
            f" | [直近60日] 高値 {recent_high_60d_lbl} / 安値 {recent_low_60d_lbl}"
            f" | [52週(250日)] 高値 {high_52w_lbl} / 安値 {low_52w_lbl}{from_52w_str}\n"
            f"日足ATR(14日平均値幅): {atr_str} | 出来高20日平均比: {vol_ratio_str}"
            f" | 最新RSI: {round(latest_d['RSI'],1) if pd.notna(latest_d.get('RSI')) else 'N/A'}"
            f" | 最新MACD: {round(latest_d['MACD'],1) if pd.notna(latest_d.get('MACD')) else 'N/A'}\n\n"
            f"[日足 移動平均線（現在値・乖離率・5日前比の傾き）]\n{sma_summary_text}\n\n"
            f"[直近10営業日の時系列推移（日足ベース）]\n{recent_10d_table_text}\n"
        )

        # ZIP展開時の同名ファイル上書きを防ぐため、内部ファイル名にも日付(YYYYMMDD)を付与
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "a", zipfile.ZIP_DEFLATED, False) as zip_file:
            zip_file.writestr(f"{stock_code}_{gen_date_file}_market_data.txt", full_text_data.encode("utf-8"))
            for img_path, img_name in [
                (img_w, f"{stock_code}_{gen_date_file}_1_weekly.png"),
                (img_d, f"{stock_code}_{gen_date_file}_2_daily.png"),
                (img_h, f"{stock_code}_{gen_date_file}_3_hourly.png"),
                (img_m, f"{stock_code}_{gen_date_file}_4_macro.png"),
            ]:
                if img_path and os.path.exists(img_path):
                    zip_file.write(img_path, img_name)

        st.session_state.generated_data = {
            "name": name, "text": full_text_data,
            "images": {"w": img_w, "d": img_d, "h": img_h, "m": img_m},
            "zip_data": zip_buffer.getvalue(), "stock_code": stock_code, "is_jp": is_jp,
            "gen_date_slash": gen_date_slash, "gen_date_short": gen_date_short,
            "gen_date_file": gen_date_file, "analysis_basis_str": analysis_basis_str,
        }
        st.success(f"✅ データとチャート画像4枚の準備が完了しました！（{analysis_basis_str}）")

if st.session_state.generated_data:
    data = st.session_state.generated_data
    gen_date_slash = data.get("gen_date_slash", datetime.now(timezone(timedelta(hours=9))).strftime("%Y/%m/%d"))
    gen_date_short = data.get("gen_date_short", datetime.now(timezone(timedelta(hours=9))).strftime("%m/%d"))
    gen_date_file = data.get("gen_date_file", datetime.now(timezone(timedelta(hours=9))).strftime("%Y%m%d"))
    analysis_basis_str = data.get("analysis_basis_str", gen_date_slash)

    col1, col2 = st.columns([2, 1])
    with col1:
        st.subheader("📦 まとめてダウンロード（PC向け・画像4枚＋テキスト）")
        st.markdown("ZIPを展開し、中のテキストと画像4枚（週足・日足・1時間足・マクロ統合）をGeminiにドラッグ＆ドロップしてください。")
    with col2:
        st.download_button(
            label=f"ZIPファイルをダウンロード ({gen_date_file})", data=data["zip_data"],
            file_name=f"{data['stock_code']}_analysis_data_{gen_date_file}.zip", mime="application/zip",
            type="primary", width="stretch",
        )

    st.markdown("---")
    st.subheader("📱 ① Geminiチャット送信用の命令文（スキル自動発動＆タイトル・分析日反映用）")
    st.markdown("通常のGeminiチャット入力欄にコピー＆ペーストしてください（登録済みスキル `stock-swing-analyzer` が自動発動します）。")

    if data.get("is_jp", True):
        search_focus = (
            f"・重点検索①（企業・需給・重要記事1〜2件の本文深掘り）：「{data['name']}（{data['stock_code']}）」の最新決算・業績推移、"
            "次回決算日、配当権利月、日次信用残・機関空売り残高、および提供データ内『[上場市場・TOPIXパッシブ需給区分]』のJPX公式CSV照合済5段階判定に基づくパッシブ需給影響"
            "（安易に『区分不明』とせず、入替境界圏・段階的ウエイト低減警戒圏・次回新規採用候補圏の場合はJPX第2段階見直し［1・4・7・10月末リバランス］該当有無を検索確認）。"
            "さらに、提供ヘッドラインのうち直近の株価急騰・急落や出来高急増の主因となっている重要記事（上方修正と減損の混在、好決算後の株価下落、重大な規制・国策報道など）が1〜2件存在する場合は、"
            "タイトルの字面だけで判断せず必ずその記事内容をGoogle検索で深掘りし、市場が好感または嫌気した実質的な理由（コンセンサス比や一過性要因の有無）を特定すること（※周辺ニュース全件の網羅検索は検索枠枯渇を防ぐため不要）。\n"
            "・重点検索②（向こう15営業日の日米重要イベント地雷原＆国策織り込み検証）：発注日から15営業日以内の日米重要イベント日程"
            "（日銀金融政策決定会合・FOMC・米雇用統計・米CPI・日米SQ日・主要選挙/政治日程）を必ずGoogle検索で特定し、"
            "イベント通過時のギャップダウン被弾を防ぐため撤退条件（含み益クッション1.5×ATR未満でのイベント前営業日後場撤退）または見送り判断に反映すること。"
            "あわせて所属セクターに対する日本政府の国策・補助金・規制や米通商政策の影響を検索・評価しつつ、"
            "すでに株価へ織り込み済み（材料出尽くし高値圏）でないかをWTI原油(CL=F)・金(GC=F)・VIX・対指数RS・52週高値比の数値で裏付け検証すること。\n"
            "・マクロ13指標表＆5段チャート（改定案A：11指標）＆対指数RS統合評価：Panel2(VVIX×SKEW)のオプション警戒感、Panel3(MOVE×HYG)のクレジット歪み、"
            "WTI原油・金先物の実物インフレ/有事動向、およびPanel5(日経平均＆日経SMA25＆TOPIX相対軌道[1306.T]×ドル円)から『値がさハイテク主導か内需・東証全体への資金流入か』を判定し、本銘柄の対日経・対TOPIX相対強弱と照合すること。\n"
            "・LSEGレポートPDF追加添付時：Q1/Q2単体の表面的な業績スコアと通期EPS予想修正トレンドの乖離（計上時期偏重の有無）、"
            "コンセンサス目標株価レンジ（最高・平均・最低）、対日経225ベータ・相関、過去10年月次リターン季節性を統合評価すること。"
        )
    else:
        search_focus = (
            f"・重点検索①（企業・需給・重要記事1〜2件の本文深掘り）：「{data['name']}（{data['stock_code']}）」の最新四半期決算"
            f"（英語クエリ `{data['stock_code']} earnings release guidance` 必須）、"
            "次回Earnings Date、Form 13F/Form 4、Short Interest推移。"
            "さらに、提供ヘッドラインのうち直近の株価急変や出来高急増の主因となっている重要記事（決算ビート/ミスとガイダンスの乖離、アナリスト格付け変更、重大な規制報道など）が1〜2件存在する場合は、"
            "タイトルの字面だけで判断せず必ずその記事内容を検索で深掘りし、市場が反応した実質的な理由を特定すること。\n"
            "・重点検索②（向こう15営業日の日米重要イベント地雷原＆政策織り込み検証）：発注日から15営業日以内の重要マクロイベント日程"
            "（FOMC・米雇用統計・米CPI・米SQ日・主要政治/通商日程）を必ずGoogle検索で特定し、撤退条件または見送り判断に反映すること。"
            "あわせて所属セクターに対する米通商政策（関税・対中輸出規制・補助金等）の影響を検索し、すでに株価へ織り込み済みでないかをWTI原油(CL=F)・金(GC=F)・VIX・対SOX指数RSの数値で裏付け検証すること。\n"
            "・マクロ13指標表＆5段チャート（改定案A：11指標）統合評価：Panel1(VIX期間構造)、Panel2(VVIX×SKEW)、Panel3(MOVE×HYG)、Panel4(SOX×US10Y)、およびWTI原油・金先物の連動性と本銘柄の対指数相対強弱を照合すること。"
        )

    chat_prompt = (
        f"【株式スイング分析リクエスト：{data['name']}（{data['stock_code']}）｜分析基準日時：{analysis_basis_str}】\n"
        "@Google 添付したデータとチャート画像4枚（週足・日足・1時間足・マクロボラティリティ統合チャート）をもとに、"
        "スキルの【Step 0】〜【7】に従って分析をお願いします。\n"
        f"{search_focus}\n"
        "・戦略厳守（勝率・利確到達率・約定実現性の最優先）：評価の優先順位は『①勝率と利確到達確率 ＞ ②5営業日以内の約定実現性 ＞ ③リスクリワード比率』としてください。"
        "利確目標は必ず『最も手前にある確実な第1上値抵抗線（SMA25/75や直近高値）の手前（1.2〜3.0×ATR目安）』に設定し、計算上のRRを稼ぐために手前の抵抗線を無視して遠くに利確目標を置くことを厳禁とします。"
        "損切り幅はノイズ狩り防止のため買値から『1.0〜1.6×日足ATR（0.8×ATR未満禁止）』とし、高勝率セットアップでは『RR 1 : 1.1以上（標準1 : 1.2以上、1 : 1.0未満は強制見送り）』を適用してください。"
        "また、現在値から『1.2×日足ATR』以内の現実的な距離（3〜5営業日以内に約定可能な範囲）で上記条件が成立しない場合や、急落中で下げ止まり未確認の場合は、数値合わせの無理な深い買い指値をひねり出すことを禁止し、躊躇なく『見送り（アラート待機）』と判定してください。"
        "なお約定後に含み益が乗りトレンド継続中は期間経過だけで売却せず、15営業日目到来時に建値以上へ逆指値を引き上げてOCO再発注する【延長条件】と、7〜10営業日停滞時または含み益1.5×ATR未満での重要イベント前営業日後場に撤退する【撤退条件】を分けて提示してください。\n"
        f"・出力厳守①（見出し末尾に分析日）：回答の1行目には必ず `# 📊 【銘柄分析】{data['name']}（{data['stock_code']}）[{gen_date_slash}分析]` を出力してください。\n"
        f"・出力厳守②（判定表に分析基準日時）：「6. ファンドマネージャーの最終投資判断」の表の先頭行に必ず `分析基準日時：{analysis_basis_str}` を明記してください。\n"
        f"・出力厳守③（SBIメモ先頭に短縮日付）：末尾の「7. 登録銘柄用メモ」はHYPER SBI 2仕様上限（全角100文字以内、推奨60〜80文字の1行）で、"
        f"先頭に必ず `[{gen_date_short}] ` を付与し、「銘柄名」「証券コード」「IFDOCO」を一切含めずに出力してください。"
    )
    st.code(chat_prompt, language="text")

    st.subheader("📱 ② 個別データ（添付用テキスト）")
    st.markdown("以下のテキストをコピーして、上記の命令文と一緒にGeminiへ貼り付けて送信してください。")
    st.code(data["text"], language="text")

    charts = data["images"]
    st.markdown(f"**📊 {data['name']} の個別チャート（週足・日足・1時間足）**")
    img_col1, img_col2, img_col3 = st.columns(3)
    if charts.get("w"):
        with img_col1:
            st.image(charts["w"], width="stretch")
    if charts.get("d"):
        with img_col2:
            st.image(charts["d"], width="stretch")
    if charts.get("h"):
        with img_col3:
            st.image(charts["h"], width="stretch")

    if charts.get("m"):
        st.markdown("**🌐 グローバルマクロ＆ボラティリティ統合チャート（11指標・直近60営業日・X軸完全同期）**")
        st.image(charts["m"], width="stretch")