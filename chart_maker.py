# ==============================================================================
# AI株式分析データジェネレーター - chart_maker.py (完全版 v2026.11 Rev.4)
# 実装済仕様：ピボット高安値ラベル全域描画（左右端の最高・最安値保護＋格納処理完備） /
#            右Y軸Price表記除去 / 日足X軸左端の月ラベル重なり防止 /
#            マクロ5段チャート共通開始日X軸同期・Y軸上部18%余白確保(凡例重なり防止) /
#            VIX・VVIX・MOVE・SKEW 252日80%・90%動的基準線描画 / 例外時Figure解放保護
# ==============================================================================
import os
import matplotlib.dates as mdates
import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator, ScalarFormatter
import mplfinance as mpf
import numpy as np
import pandas as pd
import ta

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
  for w in [25, 75, 200]:
    df[f'SMA{w}'] = ta.trend.sma_indicator(df['Close'], window=w)

  bb = ta.volatility.BollingerBands(close=df['Close'], window=20, window_dev=2)
  df['BB_UP'] = bb.bollinger_hband()
  df['BB_MID'] = bb.bollinger_mavg()
  df['BB_LOW'] = bb.bollinger_lband()

  df['RSI'] = ta.momentum.rsi(df['Close'], window=14)
  df['Vol_SMA20'] = ta.trend.sma_indicator(
      df['Volume'].astype(float), window=20
  )

  df['MACD_Line'] = ta.trend.macd(df['Close'])
  df['MACD_Signal'] = ta.trend.macd_signal(df['Close'])
  df['MACD_Hist'] = ta.trend.macd_diff(df['Close'])
  df['MACD'] = df['MACD_Hist']
  df['ATR'] = (
      ta.volatility.average_true_range(
          high=df['High'], low=df['Low'], close=df['Close'], window=14
      )
      if len(df) >= 14
      else pd.NA
  )
  return df


def _format_chart_price(val, is_jp):
  return f'{int(round(float(val)))}' if is_jp else f'{float(val):.2f}'


def _add_panel_tag(ax, label_text):
  ax.set_ylabel('')
  ax.text(
      0.01,
      0.90,
      label_text,
      transform=ax.transAxes,
      va='top',
      ha='left',
      fontsize=8.5,
      fontweight='bold',
      color='black',
      alpha=0.75,
  )


def generate_safe_chart_image(
    df_full, filename, title, tail_count, timeframe_type, is_jp=True
):
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

  df_plot = (
      df_work.tail(tail_count)
      .dropna(subset=['Open', 'High', 'Low', 'Close'])
      .copy()
  )
  if df_plot.empty:
    return None

  aps, legend_texts = [], []
  for col, color, lbl in [
      ('SMA25', 'blue', 'SMA25(青)'),
      ('SMA75', 'orange', 'SMA75(橙)'),
      ('SMA200', 'red', 'SMA200(赤)'),
  ]:
    if col in df_plot.columns and df_plot[col].notna().any():
      aps.append(
          mpf.make_addplot(
              df_plot[col], color=color, width=1.2, secondary_y=False
          )
      )
      legend_texts.append(lbl)

  if legend_texts:
    title = f"{title}   [{' / '.join(legend_texts)}]"

  if 'BB_UP' in df_plot.columns and df_plot['BB_UP'].notna().any():
    for col, color in [
        ('BB_UP', 'gray'),
        ('BB_MID', 'purple'),
        ('BB_LOW', 'gray'),
    ]:
      aps.append(
          mpf.make_addplot(
              df_plot[col],
              color=color,
              width=0.8,
              alpha=0.6,
              secondary_y=False,
          )
      )

  panel_ratios, panels_count = [8], 0

  has_volume = bool(
      'Volume' in df_plot.columns and df_plot['Volume'].notna().any()
  )
  if has_volume:
    panels_count += 1
    panel_ratios.append(1.4)
    if 'Vol_SMA20' in df_plot.columns and df_plot['Vol_SMA20'].notna().any():
      aps.append(
          mpf.make_addplot(
              df_plot['Vol_SMA20'],
              color='darkgreen',
              width=1.0,
              panel=panels_count,
              secondary_y=False,
          )
      )

  has_rsi = bool('RSI' in df_plot.columns and df_plot['RSI'].notna().any())
  if has_rsi:
    panels_count += 1
    panel_ratios.append(1.0)
    aps.append(
        mpf.make_addplot(
            df_plot['RSI'],
            color='purple',
            width=1.2,
            panel=panels_count,
            secondary_y=False,
        )
    )
    for lvl, ls, w, al in [
        (70, '--', 0.8, 1.0),
        (50, ':', 0.7, 0.6),
        (30, '--', 0.8, 1.0),
    ]:
      df_plot[f'RSI_{lvl}'] = lvl
      aps.append(
          mpf.make_addplot(
              df_plot[f'RSI_{lvl}'],
              color='gray',
              linestyle=ls,
              width=w,
              alpha=al,
              panel=panels_count,
              secondary_y=False,
          )
      )

  has_macd = bool(
      'MACD_Line' in df_plot.columns and df_plot['MACD_Line'].notna().any()
  )
  if has_macd:
    panels_count += 1
    panel_ratios.append(1.2)
    colors = ['green' if val >= 0 else 'red' for val in df_plot['MACD_Hist']]
    aps.append(
        mpf.make_addplot(
            df_plot['MACD_Hist'],
            type='bar',
            color=colors,
            panel=panels_count,
            alpha=0.5,
            secondary_y=False,
        )
    )
    aps.append(
        mpf.make_addplot(
            df_plot['MACD_Line'],
            color='blue',
            width=1.1,
            panel=panels_count,
            secondary_y=False,
        )
    )
    aps.append(
        mpf.make_addplot(
            df_plot['MACD_Signal'],
            color='orange',
            width=1.1,
            panel=panels_count,
            secondary_y=False,
        )
    )
    df_plot['MACD_Zero'] = 0
    aps.append(
        mpf.make_addplot(
            df_plot['MACD_Zero'],
            color='gray',
            linestyle=':',
            width=0.8,
            panel=panels_count,
            secondary_y=False,
        )
    )

  font_prop = _get_font_prop()
  try:
    my_style = (
        mpf.make_mpf_style(
            base_mpf_style='yahoo', rc={'font.family': font_prop.get_name()}
        )
        if font_prop
        else 'yahoo'
    )
  except Exception:
    my_style = 'yahoo'

  plot_kwargs = dict(
      type='candle',
      style=my_style,
      volume=has_volume,
      figratio=(12, 10),
      title=title,
      ylabel='',
      returnfig=True,
  )
  if aps:
    plot_kwargs['addplot'] = aps
  if len(panel_ratios) > 1:
    plot_kwargs['panel_ratios'] = tuple(panel_ratios)

  try:
    fig, axes = mpf.plot(df_plot, **plot_kwargs)
    ax_main = axes[0]
    ax_main.set_ylabel('')

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

    last_close = float(df_plot['Close'].iloc[-1])
    ax_main.text(
        1.002,
        last_close,
        f' {_format_chart_price(last_close, is_jp)} ',
        color='white',
        backgroundcolor='black',
        verticalalignment='center',
        horizontalalignment='left',
        transform=ax_main.get_yaxis_transform(),
        fontsize=8.5,
        fontweight='bold',
        zorder=10,
    )

    current_panel_idx = 2
    if has_volume and current_panel_idx < len(axes):
      ax_vol = axes[current_panel_idx]
      ax_vol.yaxis.set_major_locator(MaxNLocator(nbins=3, prune='both'))
      _add_panel_tag(ax_vol, 'Volume')
      current_panel_idx += 2

    if has_rsi and current_panel_idx < len(axes):
      ax_rsi = axes[current_panel_idx]
      ax_rsi.set_ylim(10, 90)
      ax_rsi.set_yticks([30, 70])
      _add_panel_tag(ax_rsi, 'RSI')
      current_panel_idx += 2

    if has_macd and current_panel_idx < len(axes):
      ax_macd = axes[current_panel_idx]
      macd_vals = pd.concat(
          [df_plot['MACD_Line'], df_plot['MACD_Signal'], df_plot['MACD_Hist']]
      ).dropna()
      if not macd_vals.empty:
        m_min, m_max = float(macd_vals.min()), float(macd_vals.max())
        m_span = max(m_max - m_min, 1e-5)
        ax_macd.set_ylim(m_min - m_span * 0.15, m_max + m_span * 0.15)
      ax_macd.yaxis.set_major_locator(MaxNLocator(nbins=3, prune='both'))
      _add_panel_tag(ax_macd, 'MACD')
      current_panel_idx += 2

    # ピボット高安値ラベルの算出と描画（左右端の期間最高値・最安値も保護）
    window_size = (
        10
        if timeframe_type == 'weekly'
        else (8 if timeframe_type == 'daily' else 20)
    )
    right_confirm = 2
    min_dist = max(int(len(df_plot) * 0.10), 6)
    highs, lows = [], []
    n_bars = len(df_plot)
    high_arr = df_plot['High'].astype(float).values
    low_arr = df_plot['Low'].astype(float).values
    max_h_all, min_l_all = float(np.nanmax(high_arr)), float(np.nanmin(low_arr))

    for i in range(n_bars):
      val_h, val_l = float(high_arr[i]), float(low_arr[i])
      left_b, right_b = max(0, i - window_size), min(
          n_bars, i + window_size + 1
      )
      can_check_pivot = (i >= 2) and (i < n_bars - right_confirm)

      is_high = (can_check_pivot or val_h >= max_h_all) and all(
          val_h >= float(high_arr[j]) for j in range(left_b, right_b) if i != j
      )
      is_low = (can_check_pivot or val_l <= min_l_all) and all(
          val_l <= float(low_arr[j]) for j in range(left_b, right_b) if i != j
      )

      if is_high:
        if highs and (i - highs[-1][0]) < min_dist:
          if val_h >= highs[-1][1]:
            highs[-1] = (i, val_h)
        else:
          highs.append((i, val_h))
      if is_low:
        if lows and (i - lows[-1][0]) < min_dist:
          if val_l <= lows[-1][1]:
            lows[-1] = (i, val_l)
        else:
          lows.append((i, val_l))

    current_ylim = ax_main.get_ylim()
    offset = (current_ylim[1] - current_ylim[0]) * 0.025
    ax_main.set_ylim(
        current_ylim[0] - offset * 1.5, current_ylim[1] + offset * 1.5
    )

    for idx, val in highs:
      ax_main.text(
          idx,
          val + offset,
          _format_chart_price(val, is_jp),
          ha='center',
          va='bottom',
          color='green',
          fontsize=8,
          fontweight='bold',
          zorder=9,
      )
    for idx, val in lows:
      ax_main.text(
          idx,
          val - offset,
          _format_chart_price(val, is_jp),
          ha='center',
          va='top',
          color='red',
          fontsize=8,
          fontweight='bold',
          zorder=9,
      )

    tick_indices, tick_labels = [], []
    if timeframe_type == 'weekly':
      last_month, temp_indices, temp_labels = None, [], []
      for i, dt in enumerate(df_plot.index):
        if dt.month != last_month:
          temp_indices.append(i)
          temp_labels.append(dt)
          last_month = dt.month
      filtered_indices = [temp_indices[j] for j in range(0, len(temp_indices), 3)]
      filtered_labels = [temp_labels[j] for j in range(0, len(temp_labels), 3)]
      seen_years = set()
      for k, dt in enumerate(filtered_labels):
        if k == len(filtered_labels) - 1 or dt.year not in seen_years:
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
      if len(tick_indices) >= 2 and (tick_indices[1] - tick_indices[0]) < 8:
        tick_indices.pop(0)
        tick_labels.pop(0)
    elif timeframe_type == 'hourly':
      last_date_printed, last_month_printed = None, -1
      for i, dt in enumerate(df_plot.index):
        if dt.date() != last_date_printed:
          if not tick_indices or (i - tick_indices[-1]) >= 7:
            tick_indices.append(i)
            tick_labels.append(
                dt.strftime('%m/%d' if dt.month != last_month_printed else '%d')
            )
            last_month_printed = dt.month
            last_date_printed = dt.date()

    ax_main.set_xticks(tick_indices)
    ax_main.set_xticklabels(tick_labels, rotation=45)

    fig.savefig(filename, dpi=200, bbox_inches='tight')
    plt.close(fig)
    return filename
  except Exception:
    plt.close('all')
    return None


def generate_macro_chart_image(
    macro_df_dict, filename='temp_macro.png', tail_days=60
):
  """ボラティリティ指数とマクロ指標を同一時間軸（5段2軸オーバーレイ）で1枚の画像に統合描画する。"""
  if not macro_df_dict:
    return None

  try:
    font_prop = _get_font_prop()
    if font_prop:
      try:
        plt.rcParams['font.family'] = font_prop.get_name()
      except Exception:
        pass

    fig, axes = plt.subplots(5, 1, figsize=(13, 12), sharex=True)
    fig.subplots_adjust(hspace=0.10)

    thresholds_meta = macro_df_dict.get('_thresholds', {})
    hard_caps_default = {
        '^VIX': 30.0,
        '^VVIX': 130.0,
        '^SKEW': 162.0,
        '^MOVE': 135.0,
    }

    common_start_date, common_end_date = None, None
    for ref_key in ['^VIX', 'HYG', '^SOX', '^N225']:
      ref_df = macro_df_dict.get(ref_key)
      if (
          isinstance(ref_df, pd.DataFrame)
          and not ref_df.empty
          and 'Close' in ref_df.columns
      ):
        ref_c = ref_df['Close'].dropna()
        if len(ref_c) >= 5:
          common_start_date, common_end_date = (
              ref_c.tail(tail_days).index[0],
              ref_c.index[-1],
          )
          break

    all_last_dates = [
        v['Close'].dropna().index[-1]
        for k, v in macro_df_dict.items()
        if k != '_thresholds'
        and isinstance(v, pd.DataFrame)
        and not v.empty
        and 'Close' in v.columns
        and not v['Close'].dropna().empty
    ]
    if all_last_dates:
      common_end_date = max(all_last_dates)

    def _prep_series(key):
      df = macro_df_dict.get(key)
      if (
          not isinstance(df, pd.DataFrame)
          or df.empty
          or 'Close' not in df.columns
          or df['Close'].dropna().empty
      ):
        return pd.Series(dtype=float), pd.Series(dtype=float), None, None

      c_all = df['Close'].dropna()
      s_close = (
          c_all.loc[c_all.index >= common_start_date]
          if common_start_date is not None
          else c_all.tail(tail_days)
      )
      if s_close.empty:
        s_close = c_all.tail(tail_days)

      s_high = (
          np.maximum(
              df['High']
              .reindex(s_close.index)
              .replace(0, np.nan)
              .fillna(s_close),
              s_close,
          )
          if 'High' in df.columns
          else s_close
      )
      th_info = thresholds_meta.get(key, {})
      warn_th, alert_th = th_info.get('warn'), th_info.get('alert')

      if warn_th is None and len(c_all) >= 20:
        win252 = c_all.tail(252)
        cap = hard_caps_default.get(key, float('inf'))
        warn_th = min(float(win252.quantile(0.80)), cap * 0.92)
        alert_th = min(max(float(win252.quantile(0.90)), warn_th + 0.1), cap)
      return s_close, s_high, warn_th, alert_th

    def _fmt_th_label(warn_v, alert_v):
      if warn_v and alert_v:
        return f' | 動的基準:{warn_v:.1f}/{alert_v:.1f}'
      return f' | 80%基準:{warn_v:.1f}' if warn_v else ''

    def _style_macro_axis(ax, ylabel_txt, color_code, loc_str, is_bold=True):
      y0, y1 = ax.get_ylim()
      if y1 > y0:
        ax.set_ylim(y0, y1 + (y1 - y0) * 0.18)
      ax.set_ylabel(
          ylabel_txt,
          color=color_code,
          fontsize=9 if is_bold else 8.5,
          fontweight='bold' if is_bold else 'normal',
      )
      if color_code != 'black':
        ax.tick_params(
            axis='y', labelcolor=color_code, labelsize=8.5 if is_bold else 8
        )
      ax.legend(loc=loc_str, fontsize=8, framealpha=0.85)

    # Panel 1: VIX & VIX3M (左軸) + VIX/VIX3M比 (右軸)
    ax1 = axes[0]
    vix_c, vix_h, vix_warn, vix_alert = _prep_series('^VIX')
    vix3m_c, _, _, _ = _prep_series('^VIX3M')
    if not vix_c.empty:
      ax1.plot(
          vix_c.index,
          vix_c.values,
          color='#d62728',
          lw=1.6,
          label=f'VIX (終値: {vix_c.iloc[-1]:.2f}{_fmt_th_label(vix_warn, vix_alert)})',
      )
      ax1.plot(
          vix_h.index,
          vix_h.values,
          color='#d62728',
          lw=0.8,
          ls=':',
          alpha=0.6,
          label=f'VIX (瞬間高値: {vix_h.max():.2f})',
      )
      if vix_warn is not None:
        ax1.axhline(vix_warn, color='orange', ls='--', lw=0.8, alpha=0.7)
      if vix_alert is not None:
        ax1.axhline(vix_alert, color='red', ls='--', lw=0.9, alpha=0.7)
    if not vix3m_c.empty:
      ax1.plot(
          vix3m_c.index,
          vix3m_c.values,
          color='#1f77b4',
          lw=1.2,
          ls='--',
          label=f'VIX3M ({vix3m_c.iloc[-1]:.2f})',
      )
    _style_macro_axis(ax1, 'VIX / VIX3M', 'black', 'upper left')

    if not vix_c.empty and not vix3m_c.empty:
      common_idx = vix_c.index.intersection(vix3m_c.index)
      if len(common_idx) > 0:
        ratio_s = (vix_c.loc[common_idx] / vix3m_c.loc[common_idx]).dropna()
        if not ratio_s.empty:
          ax1_r = ax1.twinx()
          ax1_r.plot(
              ratio_s.index,
              ratio_s.values,
              color='purple',
              lw=1.1,
              alpha=0.75,
              label=f'VIX/VIX3M比 ({ratio_s.iloc[-1]:.2f})',
          )
          ax1_r.axhline(1.0, color='purple', ls=':', lw=1.0, alpha=0.8)
          _style_macro_axis(
              ax1_r, 'Term Ratio', 'purple', 'upper right', is_bold=False
          )

    ax1.set_title(
        'Global Macro & Volatility Time-Series Matrix'
        ' (直近60営業日・X軸完全同期 / 破線は252日動的補正基準線)',
        fontsize=11,
        fontweight='bold',
        pad=8,
    )

    # Panel 2: VVIX (左軸) & MOVE指数 (右軸)
    ax2 = axes[1]
    vvix_c, vvix_h, vvix_warn, vvix_alert = _prep_series('^VVIX')
    move_c, move_h, move_warn, move_alert = _prep_series('^MOVE')
    if not vvix_c.empty:
      ax2.plot(
          vvix_c.index,
          vvix_c.values,
          color='#ff7f0e',
          lw=1.6,
          label=f'VVIX (終値: {vvix_c.iloc[-1]:.1f} / 最高: {vvix_h.max():.1f}{_fmt_th_label(vvix_warn, vvix_alert)})',
      )
      ax2.plot(
          vvix_h.index, vvix_h.values, color='#ff7f0e', lw=0.7, ls=':', alpha=0.55
      )
      if vvix_warn is not None:
        ax2.axhline(vvix_warn, color='#ff7f0e', ls='--', lw=0.8, alpha=0.65)
      if vvix_alert is not None:
        ax2.axhline(vvix_alert, color='#d62728', ls=':', lw=0.9, alpha=0.7)
    _style_macro_axis(ax2, 'VVIX', '#d95f02', 'upper left')

    if not move_c.empty:
      ax2_r = ax2.twinx()
      ax2_r.plot(
          move_c.index,
          move_c.values,
          color='#2ca02c',
          lw=1.5,
          label=f'MOVE 米国債Vol (終値: {move_c.iloc[-1]:.1f} / 最高: {move_h.max():.1f}{_fmt_th_label(move_warn, move_alert)})',
      )
      ax2_r.plot(
          move_h.index, move_h.values, color='#2ca02c', lw=0.7, ls=':', alpha=0.55
      )
      if move_warn is not None:
        ax2_r.axhline(move_warn, color='#2ca02c', ls='--', lw=0.8, alpha=0.65)
      if move_alert is not None:
        ax2_r.axhline(move_alert, color='#1b611b', ls=':', lw=1.0, alpha=0.8)
      _style_macro_axis(ax2_r, 'MOVE Index', '#2ca02c', 'upper right')

    # Panel 3: SKEW指数 (左軸) & HYG (右軸)
    ax3 = axes[2]
    skew_c, _, skew_warn, skew_alert = _prep_series('^SKEW')
    hyg_c, _, _, _ = _prep_series('HYG')
    if not skew_c.empty:
      ax3.plot(
          skew_c.index,
          skew_c.values,
          color='#8c564b',
          lw=1.5,
          label=f'SKEW ブラックスワン指数 (終値: {skew_c.iloc[-1]:.1f}{_fmt_th_label(skew_warn, skew_alert)})',
      )
      if skew_warn is not None:
        ax3.axhline(skew_warn, color='#8c564b', ls='--', lw=0.8, alpha=0.65)
      if skew_alert is not None:
        ax3.axhline(skew_alert, color='red', ls=':', lw=0.9, alpha=0.7)
    _style_macro_axis(ax3, 'CBOE SKEW', '#8c564b', 'upper left')

    if not hyg_c.empty:
      ax3_r = ax3.twinx()
      ax3_r.plot(
          hyg_c.index,
          hyg_c.values,
          color='#17becf',
          lw=1.5,
          label=f'HYG ハイイールド債ETF ({hyg_c.iloc[-1]:.2f})',
      )
      _style_macro_axis(ax3_r, 'HYG (Credit)', '#0f7f8a', 'upper right')

    # Panel 4: SOX指数 (左軸) & 米10年債利回り (右軸)
    ax4 = axes[3]
    sox_c, _, _, _ = _prep_series('^SOX')
    tnx_c, tnx_h, _, _ = _prep_series('^TNX')
    if not sox_c.empty:
      ax4.plot(
          sox_c.index,
          sox_c.values,
          color='#1f77b4',
          lw=1.6,
          label=f'SOX 半導体指数 ({sox_c.iloc[-1]:.1f})',
      )
    _style_macro_axis(ax4, 'SOX Index', '#1f77b4', 'upper left')

    if not tnx_c.empty:
      ax4_r = ax4.twinx()
      ax4_r.plot(
          tnx_c.index,
          tnx_c.values,
          color='#e377c2',
          lw=1.5,
          label=f'米10年債利回り ({tnx_c.iloc[-1]:.2f}% / 最高: {tnx_h.max():.2f}%)',
      )
      _style_macro_axis(ax4_r, 'US 10Y (%)', '#c51b8a', 'upper right')

    # Panel 5: 日経平均株価 (左軸) & ドル円 (右軸)
    ax5 = axes[4]
    n225_c, _, _, _ = _prep_series('^N225')
    usdjpy_c, usdjpy_h, _, _ = _prep_series('USDJPY=X')
    if not n225_c.empty:
      ax5.plot(
          n225_c.index,
          n225_c.values,
          color='#222222',
          lw=1.6,
          label=f'日経平均株価 ({int(round(n225_c.iloc[-1]))}円)',
      )
    _style_macro_axis(ax5, 'Nikkei 225', '#222222', 'upper left')

    if not usdjpy_c.empty:
      ax5_r = ax5.twinx()
      ax5_r.plot(
          usdjpy_c.index,
          usdjpy_c.values,
          color='#2ca02c',
          lw=1.5,
          label=f'ドル円 USD/JPY ({usdjpy_c.iloc[-1]:.2f}円 / 最高: {usdjpy_h.max():.2f}円)',
      )
      _style_macro_axis(ax5_r, 'USD/JPY', '#2ca02c', 'upper right')

    if common_start_date is not None and common_end_date is not None:
      x_pad_left = common_start_date - pd.Timedelta(days=1)
      x_pad_right = common_end_date + pd.Timedelta(days=1)
      for ax in axes:
        ax.set_xlim(x_pad_left, x_pad_right)

    for ax in axes:
      ax.grid(
          True,
          which='major',
          axis='both',
          linestyle='--',
          linewidth=0.6,
          color='gray',
          alpha=0.45,
      )
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
    plt.close('all')
    return None