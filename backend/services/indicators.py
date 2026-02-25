"""
Elder Trading System - Technical Indicators Module
All indicator calculations based on Dr. Alexander Elder's methodology

Indicators:
- EMA (Exponential Moving Average)
- MACD (Moving Average Convergence Divergence)
- Force Index
- Stochastic Oscillator
- RSI (Relative Strength Index)
- ATR (Average True Range)
- Impulse System
"""

import pandas as pd
import numpy as np


def calculate_ema(data: pd.Series, period: int) -> pd.Series:
    """
    Calculate Exponential Moving Average

    EMA gives more weight to recent prices, making it more responsive
    to new information than a simple moving average.

    Args:
        data: Price series (typically closing prices)
        period: Number of periods (e.g., 13, 22, 26)

    Returns:
        EMA series
    """
    return data.ewm(span=period, adjust=False).mean()


def calculate_macd(closes: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> dict:
    """
    Calculate MACD (Moving Average Convergence Divergence)

    MACD shows the relationship between two EMAs of prices.
    - MACD Line: Fast EMA - Slow EMA
    - Signal Line: EMA of MACD Line
    - Histogram: MACD Line - Signal Line

    Elder's Key Points:
    - Histogram slope is more important than its position
    - Rising histogram = bulls gaining strength
    - Falling histogram = bears gaining strength
    - Divergence between price and MACD-H is powerful signal

    Args:
        closes: Closing prices
        fast: Fast EMA period (default 12)
        slow: Slow EMA period (default 26)
        signal: Signal line period (default 9)

    Returns:
        Dictionary with macd_line, signal_line, histogram
    """
    ema_fast = calculate_ema(closes, fast)
    ema_slow = calculate_ema(closes, slow)
    macd_line = ema_fast - ema_slow
    signal_line = calculate_ema(macd_line, signal)
    histogram = macd_line - signal_line

    return {
        'macd_line': macd_line,
        'signal_line': signal_line,
        'histogram': histogram
    }


def calculate_force_index(closes: pd.Series, volumes: pd.Series, period: int = 2) -> pd.Series:
    """
    Calculate Force Index (Elder's original indicator)

    Force Index = (Close - Previous Close) × Volume

    Elder's Key Points:
    - Combines price change with volume
    - 2-day EMA of Force Index shows short-term buying/selling pressure
    - Negative Force Index in uptrend = BUYING opportunity (pullback)
    - Positive spike = exhaustion, possible reversal

    Args:
        closes: Closing prices
        volumes: Trading volumes
        period: EMA period (2 for short-term, 13 for intermediate)

    Returns:
        Force Index series
    """
    force_index = (closes - closes.shift(1)) * volumes
    return calculate_ema(force_index, period)


def calculate_stochastic(highs: pd.Series, lows: pd.Series, closes: pd.Series,
                         period: int = 14, smooth_k: int = 3) -> dict:
    """
    Calculate Stochastic Oscillator

    Measures where the close is relative to the high-low range.
    %K = (Close - Lowest Low) / (Highest High - Lowest Low) × 100

    Elder's Key Points:
    - Below 30 = Oversold (potential buy zone)
    - Above 70 = Overbought (potential sell zone)
    - In uptrend, buy when stochastic dips below 30
    - Divergences are powerful signals

    Args:
        highs: High prices
        lows: Low prices
        closes: Closing prices
        period: Lookback period (default 14)
        smooth_k: Smoothing for %K (default 3)

    Returns:
        Dictionary with stoch_k and stoch_d
    """
    lowest_low = lows.rolling(window=period).min()
    highest_high = highs.rolling(window=period).max()

    stoch_k = 100 * (closes - lowest_low) / (highest_high - lowest_low)
    stoch_d = stoch_k.rolling(window=smooth_k).mean()

    return {
        'stoch_k': stoch_k,
        'stoch_d': stoch_d
    }


def calculate_rsi(closes: pd.Series, period: int = 14) -> pd.Series:
    """
    Calculate RSI (Relative Strength Index)

    RSI = 100 - (100 / (1 + RS))
    RS = Average Gain / Average Loss

    Elder's Key Points:
    - Above 70 = Overbought
    - Below 30 = Oversold
    - Divergences between RSI and price are important
    - Best used with other indicators

    Args:
        closes: Closing prices
        period: RSI period (default 14)

    Returns:
        RSI series
    """
    delta = closes.diff()
    gain = delta.where(delta > 0, 0)
    loss = (-delta.where(delta < 0, 0))

    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))

    return rsi


def calculate_atr(highs: pd.Series, lows: pd.Series, closes: pd.Series, period: int = 14) -> pd.Series:
    """
    Calculate ATR (Average True Range) using Wilder's Moving Average (RMA).

    True Range = max(High-Low, |High-PrevClose|, |Low-PrevClose|)
    ATR = Wilder's RMA of True Range (matches TradingView ATR(14))

    Wilder's Smoothing (RMA):
    - First ATR = Simple Mean of first `period` True Range values
    - Subsequent: ATR[i] = (ATR[i-1] * (period-1) + TR[i]) / period

    This produces values identical to TradingView's ta.atr(period).

    Args:
        highs: High prices
        lows: Low prices
        closes: Closing prices
        period: ATR period (default 14)

    Returns:
        ATR series (Wilder's RMA smoothed)
    """
    tr1 = highs - lows
    tr2 = abs(highs - closes.shift(1))
    tr3 = abs(lows - closes.shift(1))

    # True Range: max of three components (skipna so first bar uses high-low only)
    true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    # Wilder's RMA — initialise with SMA, then smooth with Wilder's formula
    result = np.full(len(true_range), np.nan)
    tr_values = true_range.values

    if len(tr_values) < period:
        return pd.Series(result, index=true_range.index)

    # First ATR value = SMA of the first `period` true ranges
    result[period - 1] = float(np.mean(tr_values[:period]))

    # Subsequent values: Wilder's smoothing
    alpha = 1.0 / period
    for i in range(period, len(tr_values)):
        if not np.isnan(tr_values[i]):
            result[i] = result[i - 1] * (1.0 - alpha) + tr_values[i] * alpha
        else:
            result[i] = result[i - 1]

    return pd.Series(result, index=true_range.index)


def calculate_supertrend(highs: pd.Series, lows: pd.Series, closes: pd.Series,
                         period: int = 10, multiplier: float = 2.0) -> dict:
    """
    Calculate SuperTrend indicator

    SuperTrend uses ATR to determine trend direction.
    - GREEN (uptrend): Price above SuperTrend line → use Lower Band as support
    - RED (downtrend): Price below SuperTrend line → use Upper Band as resistance

    Args:
        highs: High prices
        lows: Low prices
        closes: Closing prices
        period: ATR period (default 10)
        multiplier: ATR multiplier (default 2.0)

    Returns:
        Dictionary with supertrend, direction (1=GREEN, -1=RED), upper_band, lower_band, atr
    """
    atr = calculate_atr(highs, lows, closes, period)
    hl2 = (highs + lows) / 2

    # Basic bands
    basic_upper = hl2 + (multiplier * atr)
    basic_lower = hl2 - (multiplier * atr)

    # Initialize final bands and output
    n = len(closes)
    final_upper = np.full(n, np.nan)
    final_lower = np.full(n, np.nan)
    supertrend = np.full(n, np.nan)
    direction = np.full(n, 0, dtype=int)

    # Find first valid index (where ATR is not NaN)
    first_valid = atr.first_valid_index()
    if first_valid is None:
        return {
            'supertrend': pd.Series(supertrend, index=closes.index),
            'direction': pd.Series(direction, index=closes.index),
            'upper_band': pd.Series(final_upper, index=closes.index),
            'lower_band': pd.Series(final_lower, index=closes.index),
            'atr': atr
        }

    start = closes.index.get_loc(first_valid)
    final_upper[start] = basic_upper.iloc[start]
    final_lower[start] = basic_lower.iloc[start]
    supertrend[start] = basic_upper.iloc[start]
    direction[start] = -1  # Start with downtrend assumption

    for i in range(start + 1, n):
        # Final Upper Band: only tighten (lower) if in downtrend
        if basic_upper.iloc[i] < final_upper[i - 1] or closes.iloc[i - 1] > final_upper[i - 1]:
            final_upper[i] = basic_upper.iloc[i]
        else:
            final_upper[i] = final_upper[i - 1]

        # Final Lower Band: only tighten (raise) if in uptrend
        if basic_lower.iloc[i] > final_lower[i - 1] or closes.iloc[i - 1] < final_lower[i - 1]:
            final_lower[i] = basic_lower.iloc[i]
        else:
            final_lower[i] = final_lower[i - 1]

        # Determine direction and SuperTrend value
        if supertrend[i - 1] == final_upper[i - 1]:  # Was in downtrend (RED)
            if closes.iloc[i] > final_upper[i]:
                supertrend[i] = final_lower[i]
                direction[i] = 1   # Switch to GREEN/uptrend
            else:
                supertrend[i] = final_upper[i]
                direction[i] = -1  # Stay RED/downtrend
        else:  # Was in uptrend (GREEN)
            if closes.iloc[i] < final_lower[i]:
                supertrend[i] = final_upper[i]
                direction[i] = -1  # Switch to RED/downtrend
            else:
                supertrend[i] = final_lower[i]
                direction[i] = 1   # Stay GREEN/uptrend

    return {
        'supertrend': pd.Series(supertrend, index=closes.index),
        # 1=GREEN, -1=RED
        'direction': pd.Series(direction, index=closes.index),
        'upper_band': pd.Series(final_upper, index=closes.index),
        'lower_band': pd.Series(final_lower, index=closes.index),
        'atr': atr
    }


def calculate_keltner_channel(highs: pd.Series, lows: pd.Series, closes: pd.Series,
                              ema_period: int = 20, atr_period: int = 10, multiplier: float = 1.0) -> dict:
    """
    Calculate Keltner Channel (KC)

    KC(20,10,1) = 20-period EMA ± (1 × ATR(10))

    Elder's Key Points:
    - Upper band = resistance level
    - Lower band = support level  
    - Channel width indicates volatility
    - Price at lower band in uptrend = buying opportunity
    - Used for setting targets (upper band) and stops (below lower band)

    Args:
        highs: High prices
        lows: Low prices
        closes: Closing prices
        ema_period: EMA period for middle line (default 20)
        atr_period: ATR period for bands (default 10)
        multiplier: ATR multiplier (default 1.0)

    Returns:
        Dictionary with middle, upper, lower bands and channel_height
    """
    middle = calculate_ema(closes, ema_period)
    atr = calculate_atr(highs, lows, closes, atr_period)

    upper = middle + (atr * multiplier)
    lower = middle - (atr * multiplier)
    channel_height = upper - lower

    return {
        'middle': middle,
        'upper': upper,
        'lower': lower,
        'atr': atr,
        'channel_height': channel_height
    }


def calculate_impulse_system(closes: pd.Series, ema_period: int = 13) -> dict:
    """
    Calculate Elder's Impulse System

    Combines EMA slope + MACD Histogram slope to determine market state.

    Colors:
    - GREEN: EMA rising AND MACD-H rising = Bulls in control, OK to buy
    - RED: EMA falling AND MACD-H falling = Bears in control, DO NOT buy
    - BLUE: Mixed signals = Neutral, caution

    Elder's Key Points:
    - GREEN = permission to buy or hold longs
    - RED = permission to sell or stay out
    - BLUE = transition period, no new positions
    - Change from RED to BLUE = early buy signal

    Args:
        closes: Closing prices
        ema_period: EMA period (default 13)

    Returns:
        Dictionary with ema, ema_slope, macd_histogram, macd_slope, impulse_color
    """
    # Calculate EMA
    ema = calculate_ema(closes, ema_period)
    ema_slope = ema - ema.shift(1)

    # Calculate MACD Histogram
    macd = calculate_macd(closes)
    macd_histogram = macd['histogram']
    macd_slope = macd_histogram - macd_histogram.shift(1)

    # Determine Impulse Color
    def get_impulse_color(ema_slope_val, macd_slope_val):
        if pd.isna(ema_slope_val) or pd.isna(macd_slope_val):
            return 'BLUE'
        if ema_slope_val > 0 and macd_slope_val > 0:
            return 'GREEN'
        elif ema_slope_val < 0 and macd_slope_val < 0:
            return 'RED'
        else:
            return 'BLUE'

    impulse_colors = pd.Series([
        get_impulse_color(ema_slope.iloc[i], macd_slope.iloc[i])
        for i in range(len(closes))
    ], index=closes.index)

    return {
        'ema': ema,
        'ema_slope': ema_slope,
        'macd_histogram': macd_histogram,
        'macd_slope': macd_slope,
        'impulse_color': impulse_colors
    }


def detect_divergence(prices: pd.Series, indicator: pd.Series, lookback: int = 20) -> dict:
    """
    Detect bullish and bearish divergences

    Bullish Divergence: Price makes lower low, indicator makes higher low
    Bearish Divergence: Price makes higher high, indicator makes lower high

    Elder's Key Points:
    - "Divergences are the strongest signals in technical analysis"
    - Bullish divergence at oversold = powerful buy signal
    - Look for divergence in MACD-H and RSI

    Args:
        prices: Price series
        indicator: Indicator series (RSI, MACD-H, etc.)
        lookback: Periods to look back for divergence

    Returns:
        Dictionary with bullish_divergence, bearish_divergence booleans
    """
    if len(prices) < lookback:
        return {'bullish': False, 'bearish': False}

    recent_prices = prices.tail(lookback)
    recent_indicator = indicator.tail(lookback)

    # Find local minima and maxima
    price_min_idx = recent_prices.idxmin()
    price_max_idx = recent_prices.idxmax()

    # Bullish: Price lower low but indicator higher low
    current_price = prices.iloc[-1]
    current_indicator = indicator.iloc[-1]

    # Simple divergence detection
    price_trend = (prices.iloc[-1] -
                   prices.iloc[-lookback]) / prices.iloc[-lookback]
    indicator_trend = indicator.iloc[-1] - indicator.iloc[-lookback]

    bullish = price_trend < -0.02 and indicator_trend > 0  # Price down, indicator up
    bearish = price_trend > 0.02 and indicator_trend < 0   # Price up, indicator down

    return {
        'bullish': bullish,
        'bearish': bearish
    }


def calculate_all_indicators(highs: pd.Series, lows: pd.Series,
                             closes: pd.Series, volumes: pd.Series) -> dict:
    """
    Calculate all Elder indicators at once

    Returns comprehensive analysis with all indicators and their interpretations.

    Args:
        highs: High prices
        lows: Low prices
        closes: Closing prices
        volumes: Volume data

    Returns:
        Dictionary with all indicator values and interpretations
    """
    # Core indicators
    ema_13 = calculate_ema(closes, 13)
    ema_22 = calculate_ema(closes, 22)

    macd = calculate_macd(closes)
    force_index_2 = calculate_force_index(closes, volumes, 2)
    force_index_13 = calculate_force_index(closes, volumes, 13)
    stochastic = calculate_stochastic(highs, lows, closes)
    rsi = calculate_rsi(closes)
    atr = calculate_atr(highs, lows, closes)
    impulse = calculate_impulse_system(closes)

    # Keltner Channel (KC 20,10,1) for channel trading
    keltner = calculate_keltner_channel(
        highs, lows, closes, ema_period=20, atr_period=10, multiplier=1.0)

    # Divergences
    macd_divergence = detect_divergence(closes, macd['histogram'])
    rsi_divergence = detect_divergence(closes, rsi)

    # Get current and previous impulse colors for "BLUE after RED" detection
    current_impulse = impulse['impulse_color'].iloc[-1]
    prev_impulse = impulse['impulse_color'].iloc[-2] if len(
        impulse['impulse_color']) > 1 else 'BLUE'

    # Get latest values
    latest = {
        'price': closes.iloc[-1],
        'ema_13': ema_13.iloc[-1],
        'ema_22': ema_22.iloc[-1],
        'macd_line': macd['macd_line'].iloc[-1],
        'macd_signal': macd['signal_line'].iloc[-1],
        'macd_histogram': macd['histogram'].iloc[-1],
        'macd_histogram_prev': macd['histogram'].iloc[-2] if len(macd['histogram']) > 1 else 0,
        'force_index_2': force_index_2.iloc[-1],
        'force_index_13': force_index_13.iloc[-1],
        'stochastic_k': stochastic['stoch_k'].iloc[-1],
        'stochastic_d': stochastic['stoch_d'].iloc[-1],
        'rsi': rsi.iloc[-1],
        'atr': atr.iloc[-1],
        'impulse_color': current_impulse,
        'prev_impulse_color': prev_impulse,  # NEW: for BLUE after RED detection
        'ema_slope': impulse['ema_slope'].iloc[-1],
        'macd_slope': impulse['macd_slope'].iloc[-1],
        'bullish_divergence_macd': macd_divergence['bullish'],
        'bullish_divergence_rsi': rsi_divergence['bullish'],
        'bearish_divergence_macd': macd_divergence['bearish'],
        'bearish_divergence_rsi': rsi_divergence['bearish'],
        # Keltner Channel values (KC 20,10,1)
        'kc_upper': keltner['upper'].iloc[-1],
        'kc_lower': keltner['lower'].iloc[-1],
        'kc_middle': keltner['middle'].iloc[-1],
        'kc_channel_height': keltner['channel_height'].iloc[-1]
    }

    # Calculate interpretations
    latest['ema_trend'] = 'UP' if latest['ema_22'] > ema_22.iloc[-5] else 'DOWN' if latest['ema_22'] < ema_22.iloc[-5] else 'FLAT'
    latest['macd_rising'] = latest['macd_histogram'] > latest['macd_histogram_prev']
    latest['price_vs_ema'] = ((latest['price'] / latest['ema_22']) - 1) * 100
    latest['channel_width'] = (latest['atr'] * 2 / latest['price']) * 100

    return latest


# Grading explanation for transparency
GRADING_CRITERIA = """
╔══════════════════════════════════════════════════════════════════════════════╗
║                    ELDER TRIPLE SCREEN - GRADING CRITERIA                     ║
║                              (Version 2.3)                                    ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                               ║
║  SCREEN 1 (Weekly) - Maximum 6 points                                         ║
║  ─────────────────────────────────────────────────────────────────────────── ║
║  1. Weekly MACD-H Rising:                                                     ║
║     +2 │ Rising from below 0 (Spring - best buy signal)                      ║
║     +1 │ Rising above 0 (Summer)                                              ║
║      0 │ Not rising                                                           ║
║                                                                               ║
║  2. MACD Line vs Signal:                                                      ║
║     +2 │ MACD Line < Signal AND both < 0 (deep oversold)                     ║
║     +1 │ MACD Line < Signal                                                   ║
║      0 │ MACD Line above Signal                                               ║
║                                                                               ║
║  3. EMA Alignment (20 > 50 > 100 > 200):                                      ║
║     +2 │ Perfect alignment (20 > 50 > 100 > 200)                             ║
║     +1 │ Partial (50 > 100 > 200, but 20 < 50)                               ║
║      0 │ No alignment                                                         ║
║                                                                               ║
║  SCREEN 2 (Daily) - Maximum 5 points                                          ║
║  ─────────────────────────────────────────────────────────────────────────── ║
║  1. Price vs Keltner Channel KC(20,10,1):                                     ║
║     +2 │ Between Lower(-1) and Lower(-3) - Deep pullback                     ║
║     +1 │ Between Mid and Lower(-1) - Normal pullback                         ║
║      0 │ Above mid-channel                                                    ║
║                                                                               ║
║  2. Force Index EMA(2):                                                       ║
║     +1 │ Force Index < 0                                                      ║
║      0 │ Force Index >= 0                                                     ║
║                                                                               ║
║  3. Stochastic:                                                               ║
║     +1 │ Stochastic < 50                                                      ║
║      0 │ Stochastic >= 50                                                     ║
║                                                                               ║
║  4. Bullish Pattern:                                                          ║
║     +1 │ Bullish Pinbar, Bullish Engulfing, False Breakout, or similar       ║
║      0 │ No pattern                                                           ║
║                                                                               ║
║  GRADES                                                                       ║
║  ─────────────────────────────────────────────────────────────────────────── ║
║  ⭐ A-TRADE: Score ≥ 7  → TRADE (High probability)                            ║
║  📊 B-TRADE: Score 5-6  → PREPARE (Set alerts)                                ║
║  👀 C-WATCH: Score 1-4  → WATCH (Monitor)                                     ║
║  🔴 AVOID:   Score = 0  → No signals                                          ║
║                                                                               ║
║  MAXIMUM POSSIBLE SCORE: 11 (Screen 1: 6 + Screen 2: 5)                       ║
║                                                                               ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""


def get_grading_criteria():
    """Return the grading criteria explanation"""
    return GRADING_CRITERIA
