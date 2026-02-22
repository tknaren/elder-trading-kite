"""
Elder Trading System - GSS (Green SuperTrend Strategy) Screener
Scans NSE 500 stocks for Long and Short setups using SuperTrend + RSI + Stochastic + EMA.

GSS Long Conditions (Daily):
  1. RSI(20) > 50
  2. Stochastic(55,34,21) %K > %D
  3. Day Low < SuperTrend(10,2)
  4. Close > SuperTrend(10,2)
  5. SuperTrend remains GREEN before & EOD
  6. SuperTrend < EMA(20)

GSS Short Conditions (Daily):
  1. RSI(20) < 50
  2. Stochastic(55,34,21) %K < %D
  3. Day High > SuperTrend(10,2)
  4. Close < SuperTrend(10,2)
  5. SuperTrend remains RED before & EOD
  6. SuperTrend > EMA(20)

Long Candle Patterns: Hammer, Pin Bar, Bullish Engulfing
Short Candle Patterns: Inverted Hammer, Shooting Star, Bearish Engulfing

Max Buying Price  = Prev day SuperTrend + Prev day ATR * 0.5
Max Selling Price = Prev day SuperTrend - Prev day ATR * 0.5
"""

import pandas as pd
import numpy as np
from typing import List, Dict, Optional


# ============================================================
# INDICATOR CALCULATIONS
# ============================================================

def calculate_ema(series: pd.Series, period: int) -> pd.Series:
    """Calculate Exponential Moving Average"""
    return series.ewm(span=period, adjust=False).mean()


def calculate_rsi(close: pd.Series, period: int = 20) -> pd.Series:
    """Calculate RSI using Wilder's smoothing. GSS default period = 20."""
    delta = close.diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi


def calculate_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 10) -> pd.Series:
    """Calculate Average True Range"""
    tr1 = high - low
    tr2 = abs(high - close.shift(1))
    tr3 = abs(low - close.shift(1))
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()


def calculate_stochastic_gss(high: pd.Series, low: pd.Series, close: pd.Series,
                              k_period: int = 55, smooth_k: int = 34, d_period: int = 21) -> Dict:
    """
    Calculate Stochastic Oscillator with GSS parameters: (55, 34, 21)
    - k_period=55: raw %K lookback
    - smooth_k=34: %K smoothed with 34-period SMA
    - d_period=21: %D is 21-period SMA of smoothed %K
    """
    lowest_low = low.rolling(window=k_period).min()
    highest_high = high.rolling(window=k_period).max()
    raw_k = 100 * ((close - lowest_low) / (highest_high - lowest_low))
    stoch_k = raw_k.rolling(window=smooth_k).mean()   # Smoothed %K
    stoch_d = stoch_k.rolling(window=d_period).mean()  # %D
    return {'stoch_k': stoch_k, 'stoch_d': stoch_d}


# Import SuperTrend from the main indicators module
from services.indicators import calculate_supertrend


# ============================================================
# CANDLE PATTERN DETECTION (GSS-specific)
# ============================================================

def _body_size(o, c):
    return abs(c - o)


def _is_bullish(o, c):
    return c > o


def _is_bearish(o, c):
    return c < o


def _upper_shadow(h, o, c):
    return h - max(o, c)


def _lower_shadow(l, o, c):
    return min(o, c) - l


def detect_hammer(row) -> Optional[str]:
    """Hammer: small body at top, long lower shadow >= 2x body"""
    o, h, l, c = row['Open'], row['High'], row['Low'], row['Close']
    body = _body_size(o, c)
    ls = _lower_shadow(l, o, c)
    us = _upper_shadow(h, o, c)
    total = h - l
    if total == 0 or body == 0:
        return None
    if ls >= body * 2 and us <= body * 0.3 and max(o, c) > (l + total * 0.6):
        return 'Hammer'
    return None


def detect_pin_bar_bullish(row) -> Optional[str]:
    """Bullish Pin Bar: small body, lower shadow >= 2x body, short upper shadow"""
    o, h, l, c = row['Open'], row['High'], row['Low'], row['Close']
    body = _body_size(o, c)
    ls = _lower_shadow(l, o, c)
    us = _upper_shadow(h, o, c)
    total = h - l
    if total == 0 or body == 0:
        return None
    if ls >= body * 2 and us < body * 0.5:
        return 'Pin Bar'
    return None


def detect_bullish_engulfing(current, prev) -> Optional[str]:
    """Bullish Engulfing: current bullish candle engulfs previous bearish candle"""
    if prev is None:
        return None
    curr_o, curr_c = current['Open'], current['Close']
    prev_o, prev_c = prev['Open'], prev['Close']
    if not _is_bullish(curr_o, curr_c) or not _is_bearish(prev_o, prev_c):
        return None
    curr_body_low = min(curr_o, curr_c)
    curr_body_high = max(curr_o, curr_c)
    prev_body_low = min(prev_o, prev_c)
    prev_body_high = max(prev_o, prev_c)
    if curr_body_low <= prev_body_low and curr_body_high >= prev_body_high:
        return 'Bullish Engulfing'
    return None


def detect_inverted_hammer(row) -> Optional[str]:
    """Inverted Hammer: small body at bottom, long upper shadow >= 2x body"""
    o, h, l, c = row['Open'], row['High'], row['Low'], row['Close']
    body = _body_size(o, c)
    us = _upper_shadow(h, o, c)
    ls = _lower_shadow(l, o, c)
    total = h - l
    if total == 0 or body == 0:
        return None
    body_at_bottom = min(o, c) < (l + total * 0.4)
    if body_at_bottom and us >= body * 2 and ls <= body * 0.3:
        return 'Inv-Hammer'
    return None


def detect_shooting_star(row) -> Optional[str]:
    """Shooting Star: small body at bottom, long upper shadow, in uptrend"""
    o, h, l, c = row['Open'], row['High'], row['Low'], row['Close']
    body = _body_size(o, c)
    us = _upper_shadow(h, o, c)
    ls = _lower_shadow(l, o, c)
    total = h - l
    if total == 0 or body == 0:
        return None
    body_ratio = body / total
    upper_ratio = us / total
    if body_ratio < 0.3 and upper_ratio > 0.6 and ls < body:
        return 'Shooting Star'
    return None


def detect_pin_bar_bearish(row) -> Optional[str]:
    """Bearish Pin Bar: small body, upper shadow >= 2x body, short lower shadow"""
    o, h, l, c = row['Open'], row['High'], row['Low'], row['Close']
    body = _body_size(o, c)
    us = _upper_shadow(h, o, c)
    ls = _lower_shadow(l, o, c)
    if body == 0:
        return None
    if us >= body * 2 and ls < body * 0.5:
        return 'Pin Bar'
    return None


def detect_bearish_engulfing(current, prev) -> Optional[str]:
    """Bearish Engulfing: current bearish candle engulfs previous bullish candle"""
    if prev is None:
        return None
    curr_o, curr_c = current['Open'], current['Close']
    prev_o, prev_c = prev['Open'], prev['Close']
    if not _is_bearish(curr_o, curr_c) or not _is_bullish(prev_o, prev_c):
        return None
    curr_body_low = min(curr_o, curr_c)
    curr_body_high = max(curr_o, curr_c)
    prev_body_low = min(prev_o, prev_c)
    prev_body_high = max(prev_o, prev_c)
    if curr_body_low <= prev_body_low and curr_body_high >= prev_body_high:
        return 'Bearish Engulfing'
    return None


def detect_gss_candle_patterns(df: pd.DataFrame, direction: str = 'long') -> List[str]:
    """
    Detect candle patterns relevant to GSS strategy.
    Long: Hammer, Pin Bar (bullish), Bullish Engulfing
    Short: Inv-Hammer, Shooting Star, Bearish Engulfing (+ bearish Pin Bar)
    """
    if len(df) < 2:
        return []

    patterns = []
    current = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else None

    if direction == 'long':
        p = detect_hammer(current)
        if p:
            patterns.append(p)
        p = detect_pin_bar_bullish(current)
        if p and 'Pin Bar' not in patterns:
            patterns.append(p)
        if prev is not None:
            p = detect_bullish_engulfing(current, prev)
            if p:
                patterns.append(p)
    else:  # short
        p = detect_inverted_hammer(current)
        if p:
            patterns.append(p)
        p = detect_shooting_star(current)
        if p:
            patterns.append(p)
        p = detect_pin_bar_bearish(current)
        if p and 'Pin Bar' not in [x for x in patterns]:
            patterns.append(p)
        if prev is not None:
            p = detect_bearish_engulfing(current, prev)
            if p:
                patterns.append(p)

    return patterns


# ============================================================
# GSS INDICATOR CALCULATION
# ============================================================

def calculate_gss_indicators(df: pd.DataFrame) -> Optional[Dict]:
    """
    Calculate all GSS indicators from a OHLCV DataFrame.
    Returns dict of pandas Series, or None if insufficient data.
    """
    if df is None or len(df) < 110:  # Need enough bars for Stochastic(55,34,21)
        return None

    rsi = calculate_rsi(df['Close'], 20)
    stoch = calculate_stochastic_gss(df['High'], df['Low'], df['Close'], 55, 34, 21)
    ema_20 = calculate_ema(df['Close'], 20)
    ema_50 = calculate_ema(df['Close'], 50)
    st = calculate_supertrend(df['High'], df['Low'], df['Close'], 10, 2.0)

    return {
        'rsi': rsi,
        'stoch_k': stoch['stoch_k'],
        'stoch_d': stoch['stoch_d'],
        'ema_20': ema_20,
        'ema_50': ema_50,
        'supertrend': st['supertrend'],
        'st_direction': st['direction'],
        'atr': st['atr']
    }


# ============================================================
# GSS CONDITION CHECKING
# ============================================================

def check_gss_long_conditions(indicators: Dict, idx: int, df: pd.DataFrame) -> Dict:
    """
    Check GSS Long entry conditions on bar at index `idx`.
    Returns dict with individual condition results and overall pass/fail.
    """
    rsi = indicators['rsi'].iloc[idx]
    stoch_k = indicators['stoch_k'].iloc[idx]
    stoch_d = indicators['stoch_d'].iloc[idx]
    low = df['Low'].iloc[idx]
    close = df['Close'].iloc[idx]
    st_val = indicators['supertrend'].iloc[idx]
    st_dir = indicators['st_direction'].iloc[idx]
    st_dir_prev = indicators['st_direction'].iloc[idx - 1] if idx > 0 else 0
    ema_20 = indicators['ema_20'].iloc[idx]

    # Handle NaN values
    if any(pd.isna(v) for v in [rsi, stoch_k, stoch_d, st_val, ema_20]):
        return {'all_conditions_met': False}

    cond1 = float(rsi) > 50                                   # RSI > 50
    cond2 = float(stoch_k) > float(stoch_d)                   # %K > %D
    cond3 = float(low) < float(st_val)                         # Low dipped below ST
    cond4 = float(close) > float(st_val)                       # Close above ST
    cond5 = int(st_dir) == 1 and int(st_dir_prev) == 1        # GREEN before & EOD
    cond6 = float(st_val) < float(ema_20)                      # ST below EMA(20)

    return {
        'rsi_above_50': cond1,
        'stoch_k_gt_d': cond2,
        'low_below_st': cond3,
        'close_above_st': cond4,
        'st_green_before_and_eod': cond5,
        'st_below_ema20': cond6,
        'all_conditions_met': cond1 and cond2 and cond3 and cond4 and cond5 and cond6
    }


def check_gss_short_conditions(indicators: Dict, idx: int, df: pd.DataFrame) -> Dict:
    """
    Check GSS Short entry conditions on bar at index `idx`.
    """
    rsi = indicators['rsi'].iloc[idx]
    stoch_k = indicators['stoch_k'].iloc[idx]
    stoch_d = indicators['stoch_d'].iloc[idx]
    high = df['High'].iloc[idx]
    close = df['Close'].iloc[idx]
    st_val = indicators['supertrend'].iloc[idx]
    st_dir = indicators['st_direction'].iloc[idx]
    st_dir_prev = indicators['st_direction'].iloc[idx - 1] if idx > 0 else 0
    ema_20 = indicators['ema_20'].iloc[idx]

    if any(pd.isna(v) for v in [rsi, stoch_k, stoch_d, st_val, ema_20]):
        return {'all_conditions_met': False}

    cond1 = float(rsi) < 50                                   # RSI < 50
    cond2 = float(stoch_k) < float(stoch_d)                   # %K < %D
    cond3 = float(high) > float(st_val)                        # High went above ST
    cond4 = float(close) < float(st_val)                       # Close below ST
    cond5 = int(st_dir) == -1 and int(st_dir_prev) == -1      # RED before & EOD
    cond6 = float(st_val) > float(ema_20)                      # ST above EMA(20)

    return {
        'rsi_below_50': cond1,
        'stoch_k_lt_d': cond2,
        'high_above_st': cond3,
        'close_below_st': cond4,
        'st_red_before_and_eod': cond5,
        'st_above_ema20': cond6,
        'all_conditions_met': cond1 and cond2 and cond3 and cond4 and cond5 and cond6
    }


# ============================================================
# MAIN SCANNING FUNCTIONS
# ============================================================

def scan_stock_gss(symbol: str, hist: pd.DataFrame, direction: str = 'long') -> Optional[Dict]:
    """
    Scan a single stock for GSS conditions on the latest candle.
    Returns a signal dict if the stock has enough data, else None.
    """
    indicators = calculate_gss_indicators(hist)
    if indicators is None:
        return None

    idx = len(hist) - 1  # Latest bar
    if idx < 1:
        return None

    # Check conditions
    if direction == 'long':
        conditions = check_gss_long_conditions(indicators, idx, hist)
    else:
        conditions = check_gss_short_conditions(indicators, idx, hist)

    # Detect candle patterns
    candle_patterns = detect_gss_candle_patterns(hist, direction)

    # Extract values (current bar)
    close = float(hist['Close'].iloc[idx])
    rsi_val = float(indicators['rsi'].iloc[idx]) if not pd.isna(indicators['rsi'].iloc[idx]) else None
    stoch_k = float(indicators['stoch_k'].iloc[idx]) if not pd.isna(indicators['stoch_k'].iloc[idx]) else None
    stoch_d = float(indicators['stoch_d'].iloc[idx]) if not pd.isna(indicators['stoch_d'].iloc[idx]) else None
    ema_20 = float(indicators['ema_20'].iloc[idx]) if not pd.isna(indicators['ema_20'].iloc[idx]) else None
    ema_50 = float(indicators['ema_50'].iloc[idx]) if not pd.isna(indicators['ema_50'].iloc[idx]) else None
    st_val = float(indicators['supertrend'].iloc[idx]) if not pd.isna(indicators['supertrend'].iloc[idx]) else None
    st_dir = int(indicators['st_direction'].iloc[idx])

    # Previous bar values (for ATR and SuperTrend)
    prev_idx = idx - 1
    prev_atr = float(indicators['atr'].iloc[prev_idx]) if not pd.isna(indicators['atr'].iloc[prev_idx]) else None
    prev_st = float(indicators['supertrend'].iloc[prev_idx]) if not pd.isna(indicators['supertrend'].iloc[prev_idx]) else None

    # Compute ATR * 0.5 and max buying/selling price
    prev_atr_half = round(prev_atr * 0.5, 2) if prev_atr else None

    if direction == 'long':
        max_price = round(prev_st + prev_atr_half, 2) if prev_st and prev_atr_half else None
    else:
        max_price = round(prev_st - prev_atr_half, 2) if prev_st and prev_atr_half else None

    price_key = 'max_buying_price' if direction == 'long' else 'max_selling_price'

    return {
        'symbol': symbol,
        'close': round(close, 2),
        'candle_pattern': candle_patterns,
        'rsi': round(rsi_val, 1) if rsi_val is not None else None,
        'stoch_k': round(stoch_k, 1) if stoch_k is not None else None,
        'stoch_d': round(stoch_d, 1) if stoch_d is not None else None,
        'ema_20': round(ema_20, 2) if ema_20 is not None else None,
        'ema_50': round(ema_50, 2) if ema_50 is not None else None,
        'supertrend': round(st_val, 2) if st_val is not None else None,
        'st_direction': 'GREEN' if st_dir == 1 else 'RED',
        'prev_atr': round(prev_atr, 2) if prev_atr is not None else None,
        'prev_atr_half': prev_atr_half,
        'prev_supertrend': round(prev_st, 2) if prev_st is not None else None,
        price_key: max_price,
        'conditions': conditions,
        'all_conditions_met': conditions.get('all_conditions_met', False)
    }


def run_gss_screener(symbols: List[str], hist_data: Dict[str, pd.DataFrame],
                      direction: str = 'long') -> Dict:
    """
    Run GSS screener across multiple symbols.

    Args:
        symbols: List of stock tickers (NSE:SYMBOL format)
        hist_data: Dict of symbol -> DataFrame with OHLCV data
        direction: 'long' or 'short'

    Returns:
        Dict with signals, summary, and metadata
    """
    all_signals = []
    matched_count = 0
    skipped_count = 0

    for symbol in symbols:
        try:
            hist = hist_data.get(symbol)
            if hist is None or len(hist) < 110:
                skipped_count += 1
                continue

            signal = scan_stock_gss(symbol, hist, direction)
            if signal:
                all_signals.append(signal)
                if signal.get('all_conditions_met'):
                    matched_count += 1
        except Exception as e:
            print(f"GSS Error scanning {symbol}: {e}")
            skipped_count += 1

    # Sort: matched signals first, then by symbol
    all_signals.sort(key=lambda x: (not x.get('all_conditions_met', False), x.get('symbol', '')))

    return {
        'signals': all_signals,
        'summary': {
            'total_scanned': len(all_signals),
            'matched': matched_count,
            'skipped': skipped_count,
            'direction': direction
        },
        'symbols_scanned': len(symbols),
        'direction': direction
    }


# ============================================================
# NIFTY 500 STOCK LIST (Official constituents)
# ============================================================

NIFTY_500 = [
    # Row 1-50
    'NSE:360ONE', 'NSE:3MINDIA', 'NSE:ABB', 'NSE:ACC', 'NSE:ACMESOLAR',
    'NSE:AIAENG', 'NSE:APLAPOLLO', 'NSE:AUBANK', 'NSE:AWL', 'NSE:AADHARHFC',
    'NSE:AARTIIND', 'NSE:AAVAS', 'NSE:ABBOTINDIA', 'NSE:ACE', 'NSE:ADANIENSOL',
    'NSE:ADANIENT', 'NSE:ADANIGREEN', 'NSE:ADANIPORTS', 'NSE:ADANIPOWER', 'NSE:ATGL',
    'NSE:ABCAPITAL', 'NSE:ABFRL', 'NSE:ABLBL', 'NSE:ABREL', 'NSE:ABSLAMC',
    'NSE:AEGISLOG', 'NSE:AEGISVOPAK', 'NSE:AFCONS', 'NSE:AFFLE', 'NSE:AJANTPHARM',
    'NSE:AKUMS', 'NSE:AKZOINDIA', 'NSE:APLLTD', 'NSE:ALKEM', 'NSE:ALKYLAMINE',
    'NSE:ALOKINDS', 'NSE:ARE&M', 'NSE:AMBER', 'NSE:AMBUJACEM', 'NSE:ANANDRATHI',
    'NSE:ANANTRAJ', 'NSE:ANGELONE', 'NSE:APARINDS', 'NSE:APOLLOHOSP', 'NSE:APOLLOTYRE',
    'NSE:APTUS', 'NSE:ASAHIINDIA', 'NSE:ASHOKLEY', 'NSE:ASIANPAINT', 'NSE:ASTERDM',
    # Row 51-100
    'NSE:ASTRAZEN', 'NSE:ASTRAL', 'NSE:ATHERENERG', 'NSE:ATUL', 'NSE:AUROPHARMA',
    'NSE:AIIL', 'NSE:DMART', 'NSE:AXISBANK', 'NSE:BASF', 'NSE:BEML',
    'NSE:BLS', 'NSE:BSE', 'NSE:BAJAJ-AUTO', 'NSE:BAJFINANCE', 'NSE:BAJAJFINSV',
    'NSE:BAJAJHLDNG', 'NSE:BAJAJHFL', 'NSE:BALKRISIND', 'NSE:BALRAMCHIN', 'NSE:BANDHANBNK',
    'NSE:BANKBARODA', 'NSE:BANKINDIA', 'NSE:MAHABANK', 'NSE:BATAINDIA', 'NSE:BAYERCROP',
    'NSE:BERGEPAINT', 'NSE:BDL', 'NSE:BEL', 'NSE:BHARATFORG', 'NSE:BHEL',
    'NSE:BPCL', 'NSE:BHARTIARTL', 'NSE:BHARTIHEXA', 'NSE:BIKAJI', 'NSE:BIOCON',
    'NSE:BSOFT', 'NSE:BLUEDART', 'NSE:BLUEJET', 'NSE:BLUESTARCO', 'NSE:BBTC',
    'NSE:BOSCHLTD', 'NSE:FIRSTCRY', 'NSE:BRIGADE', 'NSE:BRITANNIA', 'NSE:MAPMYINDIA',
    'NSE:CCL', 'NSE:CESC', 'NSE:CGPOWER', 'NSE:CRISIL', 'NSE:CAMPUS',
    # Row 101-150
    'NSE:CANFINHOME', 'NSE:CANBK', 'NSE:CAPLIPOINT', 'NSE:CGCL', 'NSE:CARBORUNIV',
    'NSE:CASTROLIND', 'NSE:CEATLTD', 'NSE:CENTRALBK', 'NSE:CDSL', 'NSE:CENTURYPLY',
    'NSE:CERA', 'NSE:CHALET', 'NSE:CHAMBLFERT', 'NSE:CHENNPETRO', 'NSE:CHOICEIN',
    'NSE:CHOLAHLDNG', 'NSE:CHOLAFIN', 'NSE:CIPLA', 'NSE:CUB', 'NSE:CLEAN',
    'NSE:COALINDIA', 'NSE:COCHINSHIP', 'NSE:COFORGE', 'NSE:COHANCE', 'NSE:COLPAL',
    'NSE:CAMS', 'NSE:CONCORDBIO', 'NSE:CONCOR', 'NSE:COROMANDEL', 'NSE:CRAFTSMAN',
    'NSE:CREDITACC', 'NSE:CROMPTON', 'NSE:CUMMINSIND', 'NSE:CYIENT', 'NSE:DCMSHRIRAM',
    'NSE:DLF', 'NSE:DOMS', 'NSE:DABUR', 'NSE:DALBHARAT', 'NSE:DATAPATTNS',
    'NSE:DEEPAKFERT', 'NSE:DEEPAKNTR', 'NSE:DELHIVERY', 'NSE:DEVYANI', 'NSE:DIVISLAB',
    'NSE:DIXON', 'NSE:AGARWALEYE', 'NSE:LALPATHLAB', 'NSE:DRREDDY', 'NSE:EIDPARRY',
    # Row 151-200
    'NSE:EIHOTEL', 'NSE:EICHERMOT', 'NSE:ELECON', 'NSE:ELGIEQUIP', 'NSE:EMAMILTD',
    'NSE:EMCURE', 'NSE:ENDURANCE', 'NSE:ENGINERSIN', 'NSE:ERIS', 'NSE:ESCORTS',
    'NSE:ETERNAL', 'NSE:EXIDEIND', 'NSE:NYKAA', 'NSE:FEDERALBNK', 'NSE:FACT',
    'NSE:FINCABLES', 'NSE:FINPIPE', 'NSE:FSL', 'NSE:FIVESTAR', 'NSE:FORCEMOT',
    'NSE:FORTIS', 'NSE:GAIL', 'NSE:GVT&D', 'NSE:GMRAIRPORT', 'NSE:GRSE',
    'NSE:GICRE', 'NSE:GILLETTE', 'NSE:GLAND', 'NSE:GLAXO', 'NSE:GLENMARK',
    'NSE:MEDANTA', 'NSE:GODIGIT', 'NSE:GPIL', 'NSE:GODFRYPHLP', 'NSE:GODREJAGRO',
    'NSE:GODREJCP', 'NSE:GODREJIND', 'NSE:GODREJPROP', 'NSE:GRANULES', 'NSE:GRAPHITE',
    'NSE:GRASIM', 'NSE:GRAVITA', 'NSE:GESHIP', 'NSE:FLUOROCHEM', 'NSE:GUJGASLTD',
    'NSE:GMDCLTD', 'NSE:GSPL', 'NSE:HEG', 'NSE:HBLENGINE', 'NSE:HCLTECH',
    # Row 201-250
    'NSE:HDFCAMC', 'NSE:HDFCBANK', 'NSE:HDFCLIFE', 'NSE:HFCL', 'NSE:HAPPSTMNDS',
    'NSE:HAVELLS', 'NSE:HEROMOTOCO', 'NSE:HEXT', 'NSE:HSCL', 'NSE:HINDALCO',
    'NSE:HAL', 'NSE:HINDCOPPER', 'NSE:HINDPETRO', 'NSE:HINDUNILVR', 'NSE:HINDZINC',
    'NSE:POWERINDIA', 'NSE:HOMEFIRST', 'NSE:HONASA', 'NSE:HONAUT', 'NSE:HUDCO',
    'NSE:HYUNDAI', 'NSE:ICICIBANK', 'NSE:ICICIGI', 'NSE:ICICIPRULI', 'NSE:IDBI',
    'NSE:IDFCFIRSTB', 'NSE:IFCI', 'NSE:IIFL', 'NSE:INOXINDIA', 'NSE:IRB',
    'NSE:IRCON', 'NSE:ITCHOTELS', 'NSE:ITC', 'NSE:ITI', 'NSE:INDGN',
    'NSE:INDIACEM', 'NSE:INDIAMART', 'NSE:INDIANB', 'NSE:IEX', 'NSE:INDHOTEL',
    'NSE:IOC', 'NSE:IOB', 'NSE:IRCTC', 'NSE:IRFC', 'NSE:IREDA',
    'NSE:IGL', 'NSE:INDUSTOWER', 'NSE:INDUSINDBK', 'NSE:NAUKRI', 'NSE:INFY',
    # Row 251-300
    'NSE:INOXWIND', 'NSE:INTELLECT', 'NSE:INDIGO', 'NSE:IGIL', 'NSE:IKS',
    'NSE:IPCALAB', 'NSE:JBCHEPHARM', 'NSE:JKCEMENT', 'NSE:JBMA', 'NSE:JKTYRE',
    'NSE:JMFINANCIL', 'NSE:JSWCEMENT', 'NSE:JSWENERGY', 'NSE:JSWINFRA', 'NSE:JSWSTEEL',
    'NSE:JPPOWER', 'NSE:J&KBANK', 'NSE:JINDALSAW', 'NSE:JSL', 'NSE:JINDALSTEL',
    'NSE:JIOFIN', 'NSE:JUBLFOOD', 'NSE:JUBLINGREA', 'NSE:JUBLPHARMA', 'NSE:JWL',
    'NSE:JYOTHYLAB', 'NSE:JYOTICNC', 'NSE:KPRMILL', 'NSE:KEI', 'NSE:KPITTECH',
    'NSE:KSB', 'NSE:KAJARIACER', 'NSE:KPIL', 'NSE:KALYANKJIL', 'NSE:KARURVYSYA',
    'NSE:KAYNES', 'NSE:KEC', 'NSE:KFINTECH', 'NSE:KIRLOSBROS', 'NSE:KIRLOSENG',
    'NSE:KOTAKBANK', 'NSE:KIMS', 'NSE:KWIL', 'NSE:LTF', 'NSE:LTTS',
    'NSE:LICHSGFIN', 'NSE:LTFOODS', 'NSE:LTIM', 'NSE:LT', 'NSE:LATENTVIEW',
    # Row 301-350
    'NSE:LAURUSLABS', 'NSE:THELEELA', 'NSE:LEMONTREE', 'NSE:LICI', 'NSE:LINDEINDIA',
    'NSE:LLOYDSME', 'NSE:LODHA', 'NSE:LUPIN', 'NSE:MMTC', 'NSE:MRF',
    'NSE:MGL', 'NSE:MAHSCOOTER', 'NSE:MAHSEAMLES', 'NSE:M&MFIN', 'NSE:M&M',
    'NSE:MANAPPURAM', 'NSE:MRPL', 'NSE:MANKIND', 'NSE:MARICO', 'NSE:MARUTI',
    'NSE:MFSL', 'NSE:MAXHEALTH', 'NSE:MAZDOCK', 'NSE:METROPOLIS', 'NSE:MINDACORP',
    'NSE:MSUMI', 'NSE:MOTILALOFS', 'NSE:MPHASIS', 'NSE:MCX', 'NSE:MUTHOOTFIN',
    'NSE:NATCOPHARM', 'NSE:NBCC', 'NSE:NCC', 'NSE:NHPC', 'NSE:NLCINDIA',
    'NSE:NMDC', 'NSE:NSLNISP', 'NSE:NTPCGREEN', 'NSE:NTPC', 'NSE:NH',
    'NSE:NATIONALUM', 'NSE:NAVA', 'NSE:NAVINFLUOR', 'NSE:NESTLEIND', 'NSE:NETWEB',
    'NSE:NEULANDLAB', 'NSE:NEWGEN', 'NSE:NAM-INDIA', 'NSE:NIVABUPA', 'NSE:NUVAMA',
    # Row 351-400
    'NSE:NUVOCO', 'NSE:OBEROIRLTY', 'NSE:ONGC', 'NSE:OIL', 'NSE:OLAELEC',
    'NSE:OLECTRA', 'NSE:PAYTM', 'NSE:ONESOURCE', 'NSE:OFSS', 'NSE:POLICYBZR',
    'NSE:PCBL', 'NSE:PGEL', 'NSE:PIIND', 'NSE:PNBHOUSING', 'NSE:PTCIL',
    'NSE:PVRINOX', 'NSE:PAGEIND', 'NSE:PATANJALI', 'NSE:PERSISTENT', 'NSE:PETRONET',
    'NSE:PFIZER', 'NSE:PHOENIXLTD', 'NSE:PIDILITIND', 'NSE:PPLPHARMA', 'NSE:POLYMED',
    'NSE:POLYCAB', 'NSE:POONAWALLA', 'NSE:PFC', 'NSE:POWERGRID', 'NSE:PRAJIND',
    'NSE:PREMIERENE', 'NSE:PRESTIGE', 'NSE:PGHH', 'NSE:PNB', 'NSE:RRKABEL',
    'NSE:RBLBANK', 'NSE:RECLTD', 'NSE:RHIM', 'NSE:RITES', 'NSE:RADICO',
    'NSE:RVNL', 'NSE:RAILTEL', 'NSE:RAINBOW', 'NSE:RKFORGE', 'NSE:RCF',
    'NSE:REDINGTON', 'NSE:RELIANCE', 'NSE:RELINFRA', 'NSE:RPOWER', 'NSE:SBFC',
    # Row 401-450
    'NSE:SBICARD', 'NSE:SBILIFE', 'NSE:SJVN', 'NSE:SRF', 'NSE:SAGILITY',
    'NSE:SAILIFE', 'NSE:SAMMAANCAP', 'NSE:MOTHERSON', 'NSE:SAPPHIRE', 'NSE:SARDAEN',
    'NSE:SAREGAMA', 'NSE:SCHAEFFLER', 'NSE:SCHNEIDER', 'NSE:SCI', 'NSE:SHREECEM',
    'NSE:SHRIRAMFIN', 'NSE:SHYAMMETL', 'NSE:ENRIN', 'NSE:SIEMENS', 'NSE:SIGNATURE',
    'NSE:SOBHA', 'NSE:SOLARINDS', 'NSE:SONACOMS', 'NSE:SONATSOFTW', 'NSE:STARHEALTH',
    'NSE:SBIN', 'NSE:SAIL', 'NSE:SUMICHEM', 'NSE:SUNPHARMA', 'NSE:SUNTV',
    'NSE:SUNDARMFIN', 'NSE:SUNDRMFAST', 'NSE:SUPREMEIND', 'NSE:SUZLON', 'NSE:SWANCORP',
    'NSE:SWIGGY', 'NSE:SYNGENE', 'NSE:SYRMA', 'NSE:TBOTEK', 'NSE:TVSMOTOR',
    'NSE:TATACHEM', 'NSE:TATACOMM', 'NSE:TCS', 'NSE:TATACONSUM', 'NSE:TATAELXSI',
    'NSE:TATAINVEST', 'NSE:TMPV', 'NSE:TATAPOWER', 'NSE:TATASTEEL', 'NSE:TATATECH',
    # Row 451-501
    'NSE:TTML', 'NSE:TECHM', 'NSE:TECHNOE', 'NSE:TEJASNET', 'NSE:NIACL',
    'NSE:RAMCOCEM', 'NSE:THERMAX', 'NSE:TIMKEN', 'NSE:TITAGARH', 'NSE:TITAN',
    'NSE:TORNTPHARM', 'NSE:TORNTPOWER', 'NSE:TARIL', 'NSE:TRENT', 'NSE:TRIDENT',
    'NSE:TRIVENI', 'NSE:TRITURBINE', 'NSE:TIINDIA', 'NSE:UCOBANK', 'NSE:UNOMINDA',
    'NSE:UPL', 'NSE:UTIAMC', 'NSE:ULTRACEMCO', 'NSE:UNIONBANK', 'NSE:UBL',
    'NSE:UNITDSPR', 'NSE:USHAMART', 'NSE:VGUARD', 'NSE:DBREALTY', 'NSE:VTL',
    'NSE:VBL', 'NSE:MANYAVAR', 'NSE:VEDL', 'NSE:VENTIVE', 'NSE:VIJAYA',
    'NSE:VMM', 'NSE:IDEA', 'NSE:VOLTAS', 'NSE:WAAREEENER', 'NSE:WELCORP',
    'NSE:WELSPUNLIV', 'NSE:WHIRLPOOL', 'NSE:WIPRO', 'NSE:WOCKPHARMA', 'NSE:YESBANK',
    'NSE:ZFCVINDIA', 'NSE:ZEEL', 'NSE:ZENTEC', 'NSE:ZENSARTECH', 'NSE:ZYDUSLIFE',
    'NSE:ECLERX',
]


def get_nse500_stock_list() -> List[str]:
    """Get official Nifty 500 stock list."""
    return list(NIFTY_500)
