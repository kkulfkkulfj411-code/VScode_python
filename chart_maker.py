import pandas as pd
import ta
import mplfinance as mpf
import matplotlib.font_manager as fm
import os
import matplotlib.pyplot as plt

SYS_FONT_PATH = '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'
if not os.path.exists(SYS_FONT_PATH):
    SYS_FONT_PATH = 'C:/Windows/Fonts/meiryo.ttc'

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
    
    df['MACD_Line'] = ta.trend.macd(df['Close'])
    df['MACD_Signal'] = ta.trend.macd_signal(df['Close'])
    df['MACD_Hist'] = ta.trend.macd_diff(df['Close'])
    df['MACD'] = df['MACD_Hist']
    return df

def generate_safe_chart_image(df_full, filename, title, tail_count, timeframe_type):
    if df_full.empty: return None
    df_full.index = pd.to_datetime(df_full.index)
    if df_full.index.tz is not None: df_full.index = df_full.index.tz_convert(None)
    
    df_plot = df_full.tail(tail_count).copy()
    df_plot = df_plot.dropna(subset=['Open', 'High', 'Low', 'Close'])
    if df_plot.empty: return None

    aps = []
    
    legend_texts = []
    if 'SMA25' in df_plot.columns and df_plot['SMA25'].notna().any():
        aps.append(mpf.make_addplot(df_plot['SMA25'], color='blue', width=1.2))
        legend_texts.append("SMA25(青)")
    if 'SMA75' in df_plot.columns and df_plot['SMA75'].notna().any():
        aps.append(mpf.make_addplot(df_plot['SMA75'], color='orange', width=1.2))
        legend_texts.append("SMA75(橙)")
    if 'SMA200' in df_plot.columns and df_plot['SMA200'].notna().any():
        aps.append(mpf.make_addplot(df_plot['SMA200'], color='red', width=1.2))
        legend_texts.append("SMA200(赤)")
        
    if legend_texts:
        title = f"{title}   [{' / '.join(legend_texts)}]"
    
    if 'BB_UP' in df_plot.columns and df_plot['BB_UP'].notna().any():
        aps.append(mpf.make_addplot(df_plot['BB_UP'], color='gray', width=0.8, alpha=0.6))
        aps.append(mpf.make_addplot(df_plot['BB_MID'], color='purple', width=0.8, alpha=0.6))
        aps.append(mpf.make_addplot(df_plot['BB_LOW'], color='gray', width=0.8, alpha=0.6))

    panel_ratios = [6]
    panels_count = 0
    
    has_volume = bool('Volume' in df_plot.columns and df_plot['Volume'].notna().any())
    if has_volume:
        panels_count += 1
        panel_ratios.append(1.5)
        if 'Vol_SMA20' in df_plot.columns and df_plot['Vol_SMA20'].notna().any():
            aps.append(mpf.make_addplot(df_plot['Vol_SMA20'], color='darkgreen', width=1.0, panel=panels_count))

    has_rsi = bool('RSI' in df_plot.columns and df_plot['RSI'].notna().any())
    if has_rsi:
        panels_count += 1
        panel_ratios.append(1.5)
        aps.append(mpf.make_addplot(df_plot['RSI'], color='purple', width=1.2, panel=panels_count))
        df_plot['RSI_70'] = 70
        df_plot['RSI_30'] = 30
        aps.append(mpf.make_addplot(df_plot['RSI_70'], color='gray', linestyle='--', width=0.8, panel=panels_count))
        aps.append(mpf.make_addplot(df_plot['RSI_30'], color='gray', linestyle='--', width=0.8, panel=panels_count))

    has_macd = bool('MACD_Line' in df_plot.columns and df_plot['MACD_Line'].notna().any())
    if has_macd:
        panels_count += 1
        panel_ratios.append(1.5)
        colors = ['green' if val >= 0 else 'red' for val in df_plot['MACD_Hist']]
        aps.append(mpf.make_addplot(df_plot['MACD_Hist'], type='bar', color=colors, panel=panels_count, alpha=0.5))
        aps.append(mpf.make_addplot(df_plot['MACD_Line'], color='blue', width=1.0, panel=panels_count))
        aps.append(mpf.make_addplot(df_plot['MACD_Signal'], color='orange', width=1.0, panel=panels_count))

    try:
        meiryo_prop = fm.FontProperties(fname=SYS_FONT_PATH)
        my_style = mpf.make_mpf_style(base_mpf_style='yahoo', rc={'font.family': meiryo_prop.get_name()})
    except:
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
        
        from matplotlib.ticker import ScalarFormatter
        for ax in fig.axes:
            formatter = ScalarFormatter(useOffset=False, useMathText=False)
            formatter.set_scientific(False)
            ax.yaxis.set_major_formatter(formatter)
            ax.yaxis.offsetText.set_visible(False)
            
        for ax in axes:
            ax.spines['top'].set_visible(True)
            ax.spines['bottom'].set_visible(True)
            ax.spines['left'].set_visible(True)
            ax.spines['right'].set_visible(True)
            ax.spines['top'].set_linewidth(1.5)
            ax.spines['bottom'].set_linewidth(1.5)
            ax.spines['left'].set_linewidth(1.0)
            ax.spines['right'].set_linewidth(1.0)
            ax.spines['top'].set_color('black')
            ax.spines['bottom'].set_color('black')
            ax.spines['left'].set_color('black')
            ax.spines['right'].set_color('black')
            
        last_close = df_plot['Close'].iloc[-1]
        price_str = f"{int(last_close)}" if last_close > 100 else f"{last_close:.2f}"
        
        ax_main.text(1.0, last_close, f' {price_str} ', color='white', 
                     backgroundcolor='black', verticalalignment='center', horizontalalignment='left',
                     transform=ax_main.get_yaxis_transform(), fontsize=9, fontweight='bold', zorder=10)
                     
        current_panel_idx = 2  
        if has_volume and current_panel_idx < len(axes):
            ax_vol = axes[current_panel_idx]
            ax_vol.text(0.01, 0.85, 'Volume', transform=ax_vol.transAxes, fontsize=10, fontweight='bold', color='black', alpha=0.7)
            current_panel_idx += 2
            
        if has_rsi and current_panel_idx < len(axes):
            ax_rsi = axes[current_panel_idx]
            ax_rsi.text(0.01, 0.85, 'RSI', transform=ax_rsi.transAxes, fontsize=10, fontweight='bold', color='black', alpha=0.7)
            current_panel_idx += 2
            
        if has_macd and current_panel_idx < len(axes):
            ax_macd = axes[current_panel_idx]
            ax_macd.text(0.01, 0.85, 'MACD', transform=ax_macd.transAxes, fontsize=10, fontweight='bold', color='black', alpha=0.7)
            current_panel_idx += 2

        window_size = 10 if timeframe_type == 'weekly' else (8 if timeframe_type == 'daily' else 20)
        highs = []
        lows = []
        for i in range(window_size, len(df_plot) - window_size):
            is_high = True
            is_low = True
            for j in range(i - window_size, i + window_size + 1):
                if i != j:
                    if df_plot['High'].iloc[i] <= df_plot['High'].iloc[j]:
                        is_high = False
                    if df_plot['Low'].iloc[i] >= df_plot['Low'].iloc[j]:
                        is_low = False
            if is_high: highs.append((i, df_plot['High'].iloc[i]))
            if is_low: lows.append((i, df_plot['Low'].iloc[i]))
            
        for idx, val in highs:
            val_str = f"{int(val)}" if val > 100 else f"{val:.1f}"
            ax_main.text(idx, val + (val*0.015), val_str, ha='center', va='bottom', color='green', fontsize=8, fontweight='bold')
        for idx, val in lows:
            val_str = f"{int(val)}" if val > 100 else f"{val:.1f}"
            ax_main.text(idx, val - (val*0.015), val_str, ha='center', va='top', color='red', fontsize=8, fontweight='bold')

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
                    if not tick_indices or (i - tick_indices[-1]) >= 4:
                        tick_indices.append(i)
                        if dt.month != last_month_printed:
                            tick_labels.append(dt.strftime('%m/%d'))
                            last_month_printed = dt.month
                        else:
                            tick_labels.append(dt.strftime('%d'))
                        last_date_printed = current_date
    
        ax_main.set_xticks(tick_indices)
        ax_main.set_xticklabels(tick_labels, rotation=45)
            
        fig.savefig(filename, dpi=300, bbox_inches='tight')
        plt.close(fig)
        return filename
    except Exception:
        return None