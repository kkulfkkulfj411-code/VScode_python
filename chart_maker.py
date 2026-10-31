import pandas as pd
import numpy as np
import ta
import mplfinance as mpf
import matplotlib.font_manager as fm
import os
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.ticker import ScalarFormatter, MaxNLocator

SYS_FONT_PATH = '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'
if not os.path.exists(SYS_FONT_PATH):
    SYS_FONT_PATH = 'C:/Windows/Fonts/meiryo.ttc'


def _get_font_prop():
    try:
        if os.path.exists(SYS_FONT_PATH):
            return fm.FontProperties(fname=SYS_FONT_PATH)
    except Exception:
        pass
    return None


def add_indicators(df):
    if df is None or df.empty:
        return df
    df = df.copy()
    df['SMA25'] = ta.trend.sma_indicator(df['Close'], window=25)
    df['SMA75'] = ta.trend.sma_indicator(df['Close'], window=75)
    df['SMA200'] = ta.trend.sma_indicator(df['Close'], window=200)

    bb = ta.volatility.BollingerBands(close=df['Close'], window=20, window_dev=2)
    df['BB_UP'] = bb.bollinger_hband()
    df['BB_MID'] = bb.bollinger_mavg()
    df['BB_LOW'] = bb.bollinger_lband()

    df['RSI'] = ta.momentum.rsi(df['Close'], window=14)
    df['Vol_SMA20'] = ta.trend.sma_indicator(df['Volume'].astype(float), window=20)

    df['MACD_Line'] = ta.trend.macd(df['Close'])
    df['MACD_Signal'] = ta.trend.macd_signal(df['Close'])
    df['MACD_Hist'] = ta.trend.macd_diff(df['Close'])
    df['MACD'] = df['MACD_Hist']

    if len(df) >= 14:
        df['ATR'] = ta.volatility.average_true_range(
            high=df['High'], low=df['Low'], close=df['Close'], window=14
        )
    else:
        df['ATR'] = pd.NA

    return df


def _format_chart_price(val, is_jp):
    if is_jp:
        return f"{int(round(val))}"
    return f"{val:.2f}"


def generate_safe_chart_image(df_full, filename, title, tail_count, timeframe_type, is_jp=True):
    if df_full is None or df_full.empty:
        return None

    df_work = df_full.copy()
    df_work.index = pd.to_datetime(df_work.index)
    if df_work.index.tz is not None:
        target_tz = 'Asia/Tokyo' if is_jp else 'America/New_York'
        try:
            df_work.index = df_work.index.tz_convert(target_tz).tz_localize(None)
        except Exception:
            df_work.index = df_work.index.tz_localize(None)

    df_plot = df_work.tail(tail_count).copy()
    df_plot = df_plot.dropna(subset=['Open', 'High', 'Low', 'Close'])
    if df_plot.empty:
        return None

    aps = []
    legend_texts = []
    if 'SMA25' in df_plot.columns and df_plot['SMA25'].notna().any():
        aps.append(mpf.make_addplot(df_plot['SMA25'], color='blue', width=1.2, secondary_y=False))
        legend_texts.append("SMA25(青)")
    if 'SMA75' in df_plot.columns and df_plot['SMA75'].notna().any():
        aps.append(mpf.make_addplot(df_plot['SMA75'], color='orange', width=1.2, secondary_y=False))
        legend_texts.append("SMA75(橙)")
    if 'SMA200' in df_plot.columns and df_plot['SMA200'].notna().any():
        aps.append(mpf.make_addplot(df_plot['SMA200'], color='red', width=1.2, secondary_y=False))
        legend_texts.append("SMA200(赤)")

    if legend_texts:
        title = f"{title}   [{' / '.join(legend_texts)}]"

    if 'BB_UP' in df_plot.columns and df_plot['BB_UP'].notna().any():
        aps.append(mpf.make_addplot(df_plot['BB_UP'], color='gray', width=0.8, alpha=0.6, secondary_y=False))
        aps.append(mpf.make_addplot(df_plot['BB_MID'], color='purple', width=0.8, alpha=0.6, secondary_y=False))
        aps.append(mpf.make_addplot(df_plot['BB_LOW'], color='gray', width=0.8, alpha=0.6, secondary_y=False))

    panel_ratios = [8]
    panels_count = 0

    has_volume = bool('Volume' in df_plot.columns and df_plot['Volume'].notna().any())
    if has_volume:
        panels_count += 1
        panel_ratios.append(1.4)
        if 'Vol_SMA20' in df_plot.columns and df_plot['Vol_SMA20'].notna().any():
            aps.append(mpf.make_addplot(df_plot['Vol_SMA20'], color='darkgreen', width=1.0, panel=panels_count, secondary_y=False))

    has_rsi = bool('RSI' in df_plot.columns and df_plot['RSI'].notna().any())
    if has_rsi:
        panels_count += 1
        panel_ratios.append(1.0)
        aps.append(mpf.make_addplot(df_plot['RSI'], color='purple', width=1.2, panel=panels_count, secondary_y=False))
        df_plot['RSI_70'] = 70
        df_plot['RSI_50'] = 50
        df_plot['RSI_30'] = 30
        aps.append(mpf.make_addplot(df_plot['RSI_70'], color='gray', linestyle='--', width=0.8, panel=panels_count, secondary_y=False))
        aps.append(mpf.make_addplot(df_plot['RSI_50'], color='gray', linestyle=':', width=0.7, alpha=0.6, panel=panels_count, secondary_y=False))
        aps.append(mpf.make_addplot(df_plot['RSI_30'], color='gray', linestyle='--', width=0.8, panel=panels_count, secondary_y=False))

    has_macd = bool('MACD_Line' in df_plot.columns and df_plot['MACD_Line'].notna().any())
    if has_macd:
        panels_count += 1
        panel_ratios.append(1.2)
        colors = ['green' if val >= 0 else 'red' for val in df_plot['MACD_Hist']]
        aps.append(mpf.make_addplot(df_plot['MACD_Hist'], type='bar', color=colors, panel=panels_count, alpha=0.5, secondary_y=False))
        aps.append(mpf.make_addplot(df_plot['MACD_Line'], color='blue', width=1.1, panel=panels_count, secondary_y=False))
        aps.append(mpf.make_addplot(df_plot['MACD_Signal'], color='orange', width=1.1, panel=panels_count, secondary_y=False))
        df_plot['MACD_Zero'] = 0
        aps.append(mpf.make_addplot(df_plot['MACD_Zero'], color='gray', linestyle=':', width=0.8, panel=panels_count, secondary_y=False))

    font_prop = _get_font_prop()
    try:
        if font_prop:
            my_style = mpf.make_mpf_style(base_mpf_style='yahoo', rc={'font.family': font_prop.get_name()})
        else:
            my_style = 'yahoo'
    except Exception:
        my_style = 'yahoo'

    plot_kwargs = dict(
        type='candle',
        style=my_style,
        volume=has_volume,
        figratio=(12, 10),
        title=title,
        ylabel='Price',
        returnfig=True
    )
    if aps:
        plot_kwargs['addplot'] = aps

    try:
        if len(panel_ratios) > 1:
            plot_kwargs['panel_ratios'] = tuple(panel_ratios)

        fig, axes = mpf.plot(df_plot, **plot_kwargs)
        ax_main = axes[0]

        for ax in fig.axes:
            formatter = ScalarFormatter(useOffset=False, useMathText=False)
            formatter.set_scientific(False)
            ax.yaxis.set_major_formatter(formatter)
            ax.yaxis.offsetText.set_visible(False)
            ax.tick_params(axis='y', labelsize=8.5)

        for ax in axes:
            for side in ['top', 'bottom', 'left', 'right']:
                ax.spines[side].set_visible(True)
                ax.spines[side].set_color('black')
                ax.spines[side].set_linewidth(1.2 if side in ['top', 'bottom'] else 1.0)

        last_close = df_plot['Close'].iloc[-1]
        price_str = _format_chart_price(last_close, is_jp)

        ax_main.text(
            1.0, last_close, f' {price_str} ', color='white',
            backgroundcolor='black', verticalalignment='center', horizontalalignment='left',
            transform=ax_main.get_yaxis_transform(), fontsize=9, fontweight='bold', zorder=10
        )

        current_panel_idx = 2
        if has_volume and current_panel_idx < len(axes):
            ax_vol = axes[current_panel_idx]
            ax_vol.set_ylabel('')
            ax_vol.yaxis.set_major_locator(MaxNLocator(nbins=3, prune='both'))
            ax_vol.text(
                0.01, 0.90, 'Volume', transform=ax_vol.transAxes,
                va='top', ha='left', fontsize=8.5, fontweight='bold', color='black', alpha=0.75
            )
            current_panel_idx += 2

        if has_rsi and current_panel_idx < len(axes):
            ax_rsi = axes[current_panel_idx]
            ax_rsi.set_ylabel('')
            ax_rsi.set_ylim(10, 90)
            ax_rsi.set_yticks([30, 70])
            ax_rsi.text(
                0.01, 0.90, 'RSI', transform=ax_rsi.transAxes,
                va='top', ha='left', fontsize=8.5, fontweight='bold', color='black', alpha=0.75
            )
            current_panel_idx += 2

        if has_macd and current_panel_idx < len(axes):
            ax_macd = axes[current_panel_idx]
            ax_macd.set_ylabel('')
            macd_vals = pd.concat([df_plot['MACD_Line'], df_plot['MACD_Signal'], df_plot['MACD_Hist']]).dropna()
            if not macd_vals.empty:
                m_min, m_max = macd_vals.min(), macd_vals.max()
                m_span = max(m_max - m_min, 1e-5)
                ax_macd.set_ylim(m_min - m_span * 0.15, m_max + m_span * 0.15)
            ax_macd.yaxis.set_major_locator(MaxNLocator(nbins=3, prune='both'))
            ax_macd.text(
                0.01, 0.90, 'MACD', transform=ax_macd.transAxes,
                va='top', ha='left', fontsize=8.5, fontweight='bold', color='black', alpha=0.75
            )
            current_panel_idx += 2

        window_size = 10 if timeframe_type == 'weekly' else (8 if timeframe_type == 'daily' else 20)
        min_dist = max(int(len(df_plot) * 0.11), 10)
        highs = []
        lows = []
        for i in range(window_size, len(df_plot) - window_size):
            val_h = df_plot['High'].iloc[i]
            val_l = df_plot['Low'].iloc[i]
            is_high = all(val_h >= df_plot['High'].iloc[j] for j in range(i - window_size, i + window_size + 1) if i != j)
            is_low = all(val_l <= df_plot['Low'].iloc[j] for j in range(i - window_size, i + window_size + 1) if i != j)

            if is_high:
                if highs and (i - highs[-1][0]) < min_dist:
                    if val_h > highs[-1][1]:
                        highs[-1] = (i, val_h)
                else:
                    highs.append((i, val_h))
            if is_low:
                if lows and (i - lows[-1][0]) < min_dist:
                    if val_l < lows[-1][1]:
                        lows[-1] = (i, val_l)
                else:
                    lows.append((i, val_l))

        current_ylim = ax_main.get_ylim()
        y_range = current_ylim[1] - current_ylim[0]
        offset = y_range * 0.025

        ax_main.set_ylim(current_ylim[0] - offset * 1.5, current_ylim[1] + offset * 1.5)

        for idx, val in highs:
            val_str = _format_chart_price(val, is_jp)
            ax_main.text(idx, val + offset, val_str, ha='center', va='bottom', color='green', fontsize=8, fontweight='bold')
        for idx, val in lows:
            val_str = _format_chart_price(val, is_jp)
            ax_main.text(idx, val - offset, val_str, ha='center', va='top', color='red', fontsize=8, fontweight='bold')

        tick_indices = []
        tick_labels = []

        if timeframe_type == 'weekly':
            last_month = None
            temp_indices = []
            temp_labels = []
            for i, dt in enumerate(df_plot.index):
                if dt.month != last_month:
                    temp_indices.append(i)
                    temp_labels.append(dt)
                    last_month = dt.month

            filtered_indices = []
            filtered_labels = []
            for j in range(len(temp_indices)):
                if j % 3 == 0:
                    filtered_indices.append(temp_indices[j])
                    filtered_labels.append(temp_labels[j])

            seen_years = set()
            for k in range(len(filtered_labels)):
                dt = filtered_labels[k]
                is_last = (k == len(filtered_labels) - 1)
                is_first_of_year = dt.year not in seen_years

                if is_last or is_first_of_year:
                    tick_labels.append(dt.strftime('%y/%m'))
                    seen_years.add(dt.year)
                else:
                    tick_labels.append(dt.strftime('%m'))
            tick_indices = filtered_indices

        elif timeframe_type == 'daily':
            last_month = None
            for i, dt in enumerate(df_plot.index):
                if dt.month != last_month:
                    tick_indices.append(i)
                    tick_labels.append(dt.strftime('%y/%m'))
                    last_month = dt.month

        elif timeframe_type == 'hourly':
            last_date_printed = None
            last_month_printed = -1
            for i, dt in enumerate(df_plot.index):
                current_date = dt.date()
                if current_date != last_date_printed:
                    if not tick_indices or (i - tick_indices[-1]) >= 7:
                        tick_indices.append(i)
                        if dt.month != last_month_printed:
                            tick_labels.append(dt.strftime('%m/%d'))
                            last_month_printed = dt.month
                        else:
                            tick_labels.append(dt.strftime('%d'))
                        last_date_printed = current_date

        ax_main.set_xticks(tick_indices)
        ax_main.set_xticklabels(tick_labels, rotation=45)

        fig.savefig(filename, dpi=200, bbox_inches='tight')
        plt.close(fig)
        return filename
    except Exception:
        return None


def generate_macro_chart_image(macro_df_dict, filename="temp_macro.png", tail_days=60):
    """
    ボラティリティ指数（VIX, VIX3M, VVIX, SKEW, MOVE）とマクロ指標（HYG, SOX, 米10年債, 日経平均, ドル円）を
    同一時間軸（X軸完全同期・5段2軸オーバーレイ）で1枚の画像に統合描画する。
    """
    if not macro_df_dict:
        return None

    try:
        font_prop = _get_font_prop()
        if font_prop:
            plt.rcParams['font.family'] = font_prop.get_name()

        fig, axes = plt.subplots(5, 1, figsize=(13, 12), sharex=True)
        fig.subplots_adjust(hspace=0.10)

        def _prep_series(key):
            df = macro_df_dict.get(key)
            if df is None or df.empty:
                return pd.Series(dtype=float), pd.Series(dtype=float)
            s_close = df['Close'].dropna().tail(tail_days)
            s_high = df['High'].dropna().tail(tail_days) if 'High' in df.columns else s_close
            return s_close, s_high

        # Panel 1: VIX & VIX3M (左軸) + VIX/VIX3M比 (右軸)
        ax1 = axes[0]
        vix_c, vix_h = _prep_series('^VIX')
        vix3m_c, _ = _prep_series('^VIX3M')
        if not vix_c.empty:
            ax1.plot(vix_c.index, vix_c.values, color='#d62728', lw=1.6, label=f"VIX (終値: {vix_c.iloc[-1]:.2f})")
            ax1.plot(vix_h.index, vix_h.values, color='#d62728', lw=0.8, ls=':', alpha=0.6, label=f"VIX (瞬間高値: {vix_h.max():.2f})")
        if not vix3m_c.empty:
            ax1.plot(vix3m_c.index, vix3m_c.values, color='#1f77b4', lw=1.2, ls='--', label=f"VIX3M ({vix3m_c.iloc[-1]:.2f})")
        ax1.axhline(20, color='orange', ls='--', lw=0.8, alpha=0.7)
        ax1.axhline(25, color='red', ls='--', lw=0.9, alpha=0.7)
        ax1.set_ylabel("VIX / VIX3M", fontsize=9, fontweight='bold')
        ax1.legend(loc='upper left', fontsize=8, framealpha=0.85)

        if not vix_c.empty and not vix3m_c.empty:
            common_idx = vix_c.index.intersection(vix3m_c.index)
            if len(common_idx) > 0:
                ratio_s = (vix_c.loc[common_idx] / vix3m_c.loc[common_idx]).dropna()
                if not ratio_s.empty:
                    ax1_r = ax1.twinx()
                    ax1_r.plot(ratio_s.index, ratio_s.values, color='purple', lw=1.1, alpha=0.75, label=f"VIX/VIX3M比 ({ratio_s.iloc[-1]:.2f})")
                    ax1_r.axhline(1.0, color='purple', ls=':', lw=1.0, alpha=0.8)
                    ax1_r.set_ylabel("Term Ratio", color='purple', fontsize=8.5)
                    ax1_r.tick_params(axis='y', labelcolor='purple', labelsize=8)
                    ax1_r.legend(loc='upper right', fontsize=8, framealpha=0.85)

        ax1.set_title(
            "Global Macro & Volatility Time-Series Matrix (直近60営業日・X軸完全同期 / 点線はザラ場瞬間高値)",
            fontsize=11, fontweight='bold', pad=8
        )

        # Panel 2: VVIX (左軸) & MOVE指数 (右軸)
        ax2 = axes[1]
        vvix_c, vvix_h = _prep_series('^VVIX')
        move_c, move_h = _prep_series('^MOVE')
        if not vvix_c.empty:
            ax2.plot(vvix_c.index, vvix_c.values, color='#ff7f0e', lw=1.6, label=f"VVIX (終値: {vvix_c.iloc[-1]:.1f} / 最高: {vvix_h.max():.1f})")
            ax2.plot(vvix_h.index, vvix_h.values, color='#ff7f0e', lw=0.7, ls=':', alpha=0.55)
            ax2.axhline(110, color='#ff7f0e', ls='--', lw=0.8, alpha=0.6)
        ax2.set_ylabel("VVIX", color='#d95f02', fontsize=9, fontweight='bold')
        ax2.tick_params(axis='y', labelcolor='#d95f02', labelsize=8.5)
        ax2.legend(loc='upper left', fontsize=8, framealpha=0.85)

        if not move_c.empty:
            ax2_r = ax2.twinx()
            ax2_r.plot(move_c.index, move_c.values, color='#2ca02c', lw=1.5, label=f"MOVE 米国債Vol (終値: {move_c.iloc[-1]:.1f} / 最高: {move_h.max():.1f})")
            ax2_r.plot(move_h.index, move_h.values, color='#2ca02c', lw=0.7, ls=':', alpha=0.55)
            ax2_r.axhline(120, color='#2ca02c', ls='--', lw=0.8, alpha=0.6)
            ax2_r.set_ylabel("MOVE Index", color='#2ca02c', fontsize=9, fontweight='bold')
            ax2_r.tick_params(axis='y', labelcolor='#2ca02c', labelsize=8.5)
            ax2_r.legend(loc='upper right', fontsize=8, framealpha=0.85)

        # Panel 3: SKEW指数 (左軸) & HYG (右軸)
        ax3 = axes[2]
        skew_c, _ = _prep_series('^SKEW')
        hyg_c, _ = _prep_series('HYG')
        if not skew_c.empty:
            ax3.plot(skew_c.index, skew_c.values, color='#8c564b', lw=1.5, label=f"SKEW ブラックスワン指数 (終値: {skew_c.iloc[-1]:.1f})")
            ax3.axhline(135, color='#8c564b', ls='--', lw=0.8, alpha=0.6)
            ax3.axhline(150, color='red', ls=':', lw=0.8, alpha=0.6)
        ax3.set_ylabel("CBOE SKEW", color='#8c564b', fontsize=9, fontweight='bold')
        ax3.tick_params(axis='y', labelcolor='#8c564b', labelsize=8.5)
        ax3.legend(loc='upper left', fontsize=8, framealpha=0.85)

        if not hyg_c.empty:
            ax3_r = ax3.twinx()
            ax3_r.plot(hyg_c.index, hyg_c.values, color='#17becf', lw=1.5, label=f"HYG ハイイールド債ETF ({hyg_c.iloc[-1]:.2f})")
            ax3_r.set_ylabel("HYG (Credit)", color='#0f7f8a', fontsize=9, fontweight='bold')
            ax3_r.tick_params(axis='y', labelcolor='#0f7f8a', labelsize=8.5)
            ax3_r.legend(loc='upper right', fontsize=8, framealpha=0.85)

        # Panel 4: SOX指数 (左軸) & 米10年債利回り (右軸)
        ax4 = axes[3]
        sox_c, _ = _prep_series('^SOX')
        tnx_c, tnx_h = _prep_series('^TNX')
        if not sox_c.empty:
            ax4.plot(sox_c.index, sox_c.values, color='#1f77b4', lw=1.6, label=f"SOX 半導体指数 ({sox_c.iloc[-1]:.1f})")
        ax4.set_ylabel("SOX Index", color='#1f77b4', fontsize=9, fontweight='bold')
        ax4.tick_params(axis='y', labelcolor='#1f77b4', labelsize=8.5)
        ax4.legend(loc='upper left', fontsize=8, framealpha=0.85)

        if not tnx_c.empty:
            ax4_r = ax4.twinx()
            ax4_r.plot(tnx_c.index, tnx_c.values, color='#e377c2', lw=1.5, label=f"米10年債利回り ({tnx_c.iloc[-1]:.2f}% / 最高: {tnx_h.max():.2f}%)")
            ax4_r.set_ylabel("US 10Y (%)", color='#c51b8a', fontsize=9, fontweight='bold')
            ax4_r.tick_params(axis='y', labelcolor='#c51b8a', labelsize=8.5)
            ax4_r.legend(loc='upper right', fontsize=8, framealpha=0.85)

        # Panel 5: 日経平均株価 (左軸) & ドル円 (右軸)
        ax5 = axes[4]
        n225_c, _ = _prep_series('^N225')
        usdjpy_c, usdjpy_h = _prep_series('USDJPY=X')
        if not n225_c.empty:
            ax5.plot(n225_c.index, n225_c.values, color='#222222', lw=1.6, label=f"日経平均株価 ({int(round(n225_c.iloc[-1]))}円)")
        ax5.set_ylabel("Nikkei 225", color='#222222', fontsize=9, fontweight='bold')
        ax5.tick_params(axis='y', labelcolor='#222222', labelsize=8.5)
        ax5.legend(loc='upper left', fontsize=8, framealpha=0.85)

        if not usdjpy_c.empty:
            ax5_r = ax5.twinx()
            ax5_r.plot(usdjpy_c.index, usdjpy_c.values, color='#2ca02c', lw=1.5, label=f"ドル円 USD/JPY ({usdjpy_c.iloc[-1]:.2f}円 / 最高: {usdjpy_h.max():.2f}円)")
            ax5_r.set_ylabel("USD/JPY", color='#2ca02c', fontsize=9, fontweight='bold')
            ax5_r.tick_params(axis='y', labelcolor='#2ca02c', labelsize=8.5)
            ax5_r.legend(loc='upper right', fontsize=8, framealpha=0.85)

        for ax in axes:
            ax.grid(True, which='major', axis='both', linestyle='--', linewidth=0.6, color='gray', alpha=0.45)
            for side in ['top', 'bottom', 'left', 'right']:
                ax.spines[side].set_visible(True)
                ax.spines[side].set_color('black')
                ax.spines[side].set_linewidth(0.9)

        axes[-1].xaxis.set_major_locator(mdates.DayLocator(interval=5))
        axes[-1].xaxis.set_major_formatter(mdates.DateFormatter('%m/%d'))
        plt.setp(axes[-1].xaxis.get_majorticklabels(), rotation=30, fontsize=8.5)

        fig.savefig(filename, dpi=180, bbox_inches='tight')
        plt.close(fig)
        return filename
    except Exception:
        return None