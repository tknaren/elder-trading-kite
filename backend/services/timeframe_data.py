"""
Elder Trading System - Multi-Timeframe Data Service
====================================================

Fetches and stores multi-timeframe OHLCV data and indicators for the
trading watchlist. Supports 15-minute, 75-minute (aggregated), and daily candles.

75-minute candle aggregation:
  - Kite API doesn't natively support 75min candles
  - We fetch 15-min candles and aggregate every 5 consecutive candles
  - NSE session: 9:15-15:30 IST → five 75-min blocks:
    9:15-10:30, 10:30-11:45, 11:45-13:00, 13:00-14:15, 14:15-15:30

Rate limiting:
  - ~3 requests/sec to Kite API (0.35s interval, handled by kite_client)
  - 20 symbols × 2 calls = ~14 seconds per cycle (well within 5-min window)
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta, time
from typing import Optional, Dict, List, Tuple
import pytz
import traceback

from services.kite_client import get_client, is_nse_market_open
from services.indicators import (
    calculate_ema,
    calculate_macd,
    calculate_rsi,
    calculate_atr,
    calculate_force_index,
    calculate_stochastic,
    calculate_impulse_system,
    calculate_keltner_channel
)
from models.database import get_database

IST = pytz.timezone('Asia/Kolkata')

# 75-minute block boundaries (IST times marking start of each block)
BLOCK_75_STARTS = [
    time(9, 15),
    time(10, 30),
    time(11, 45),
    time(13, 0),
    time(14, 15),
]


def fetch_15min_candles(symbol: str, days: int = 5) -> Optional[pd.DataFrame]:
    """
    Fetch 15-minute candles from Kite API.

    Args:
        symbol: NSE:SYMBOL format
        days: Number of days of history (default 5 for recent data)

    Returns:
        DataFrame with OHLCV columns, DatetimeIndex (IST-aware)
    """
    client = get_client()
    if not client or not client._authenticated:
        return None

    df = client.get_historical_data(symbol, interval='15minute', days=days)
    return df


def fetch_daily_candles(symbol: str, days: int = 120) -> Optional[pd.DataFrame]:
    """
    Fetch daily candles from Kite API.

    Args:
        symbol: NSE:SYMBOL format
        days: Number of days of history (default 120 for indicator warmup)

    Returns:
        DataFrame with OHLCV columns, DatetimeIndex
    """
    client = get_client()
    if not client or not client._authenticated:
        return None

    df = client.get_historical_data(symbol, interval='day', days=days)
    return df


def aggregate_75min_from_15min(df_15: pd.DataFrame) -> pd.DataFrame:
    """
    Aggregate 15-minute candles into 75-minute candles.

    Each 75-min block consists of 5 consecutive 15-min candles,
    aligned to NSE session boundaries:
      Block 1: 09:15 - 10:30
      Block 2: 10:30 - 11:45
      Block 3: 11:45 - 13:00
      Block 4: 13:00 - 14:15
      Block 5: 14:15 - 15:30

    Args:
        df_15: DataFrame with 15-min OHLCV data, DatetimeIndex

    Returns:
        DataFrame with 75-min OHLCV data
    """
    if df_15 is None or df_15.empty:
        return pd.DataFrame()

    records = []

    # Group by date
    df_15 = df_15.copy()
    if df_15.index.tz is not None:
        dates = df_15.index.normalize().unique()
    else:
        dates = pd.to_datetime(df_15.index.date).unique()

    for date in dates:
        day_mask = df_15.index.date == date.date() if hasattr(date, 'date') else df_15.index.date == date
        day_candles = df_15[day_mask]

        if day_candles.empty:
            continue

        for block_start in BLOCK_75_STARTS:
            block_end_minutes = block_start.hour * 60 + block_start.minute + 75
            block_end = time(block_end_minutes // 60, block_end_minutes % 60)

            # Filter candles in this 75-min block
            block_mask = (day_candles.index.time >= block_start) & (day_candles.index.time < block_end)
            block = day_candles[block_mask]

            if block.empty:
                continue

            # Aggregate OHLCV
            block_time = block.index[0]
            record = {
                'Date': block_time,
                'Open': block['Open'].iloc[0],
                'High': block['High'].max(),
                'Low': block['Low'].min(),
                'Close': block['Close'].iloc[-1],
                'Volume': block['Volume'].sum()
            }
            records.append(record)

    if not records:
        return pd.DataFrame()

    df_75 = pd.DataFrame(records)
    df_75.set_index('Date', inplace=True)
    return df_75


def calculate_indicators_for_timeframe(df: pd.DataFrame) -> Optional[Dict]:
    """
    Calculate all Elder indicators for a given timeframe DataFrame.

    Args:
        df: DataFrame with OHLCV columns (Open, High, Low, Close, Volume)

    Returns:
        Dict with latest indicator values, or None if insufficient data
    """
    if df is None or df.empty or len(df) < 14:
        return None

    try:
        highs = df['High']
        lows = df['Low']
        closes = df['Close']
        volumes = df['Volume']

        # EMAs
        ema_13 = calculate_ema(closes, 13)
        ema_22 = calculate_ema(closes, 22)
        ema_50 = calculate_ema(closes, 50) if len(closes) >= 50 else pd.Series([None] * len(closes))

        # MACD
        macd = calculate_macd(closes)

        # RSI
        rsi = calculate_rsi(closes)

        # ATR
        atr = calculate_atr(highs, lows, closes)

        # Force Index
        force_idx = calculate_force_index(closes, volumes, 2)

        # Stochastic
        stoch = calculate_stochastic(highs, lows, closes)

        # Impulse System (returns dict with 'impulse_color' Series)
        impulse_result = calculate_impulse_system(closes)

        # Keltner Channel (returns dict with 'upper', 'middle', 'lower' Series)
        keltner = calculate_keltner_channel(highs, lows, closes)

        # Build result with latest values
        last = len(closes) - 1

        # Extract impulse color (handle both dict and Series return types)
        impulse_color = None
        if isinstance(impulse_result, dict) and 'impulse_color' in impulse_result:
            ic = impulse_result['impulse_color']
            impulse_color = ic.iloc[last] if hasattr(ic, 'iloc') and last < len(ic) else None
        elif hasattr(impulse_result, 'iloc'):
            impulse_color = impulse_result.iloc[last] if last < len(impulse_result) else None

        # Lowercase impulse color for frontend consistency
        if impulse_color and isinstance(impulse_color, str):
            impulse_color = impulse_color.lower()

        # Extract stochastic values (handle 'stoch_k'/'stoch_d' or 'k'/'d' keys)
        stoch_k_val = None
        stoch_d_val = None
        if isinstance(stoch, dict):
            if 'stoch_k' in stoch:
                stoch_k_val = _safe_float(stoch['stoch_k'].iloc[last])
            elif 'k' in stoch:
                stoch_k_val = _safe_float(stoch['k'].iloc[last])
            if 'stoch_d' in stoch:
                stoch_d_val = _safe_float(stoch['stoch_d'].iloc[last])
            elif 'd' in stoch:
                stoch_d_val = _safe_float(stoch['d'].iloc[last])
        elif hasattr(stoch, 'iloc'):
            stoch_k_val = _safe_float(stoch.iloc[last])

        # Extract Keltner Channel values
        kc_upper = None
        kc_middle = None
        kc_lower = None
        if isinstance(keltner, dict):
            if 'upper' in keltner and hasattr(keltner['upper'], 'iloc'):
                kc_upper = _safe_float(keltner['upper'].iloc[last])
            if 'middle' in keltner and hasattr(keltner['middle'], 'iloc'):
                kc_middle = _safe_float(keltner['middle'].iloc[last])
            if 'lower' in keltner and hasattr(keltner['lower'], 'iloc'):
                kc_lower = _safe_float(keltner['lower'].iloc[last])

        result = {
            'ema_13': _safe_float(ema_13.iloc[last]),
            'ema_22': _safe_float(ema_22.iloc[last]),
            'ema_50': _safe_float(ema_50.iloc[last]) if len(ema_50) > last else None,
            'macd_line': _safe_float(macd['macd_line'].iloc[last]) if isinstance(macd, dict) and 'macd_line' in macd else None,
            'macd_signal': _safe_float(macd['signal_line'].iloc[last]) if isinstance(macd, dict) and 'signal_line' in macd else None,
            'macd_histogram': _safe_float(macd['histogram'].iloc[last]) if isinstance(macd, dict) and 'histogram' in macd else None,
            'rsi': _safe_float(rsi.iloc[last]),
            'atr': _safe_float(atr.iloc[last]),
            'force_index': _safe_float(force_idx.iloc[last]),
            'stochastic': stoch_k_val,
            'stoch_d': stoch_d_val,
            'impulse_color': impulse_color,
            'kc_upper': kc_upper,
            'kc_middle': kc_middle,
            'kc_lower': kc_lower,
            'candle_time': str(df.index[last])
        }
        return result

    except Exception as e:
        print(f"  Error calculating indicators: {e}")
        traceback.print_exc()
        return None


def _safe_float(val):
    """Convert to float safely, return None for NaN/None"""
    if val is None:
        return None
    try:
        f = float(val)
        return None if np.isnan(f) else round(f, 4)
    except (ValueError, TypeError):
        return None


def store_ohlcv_batch(symbol: str, timeframe: str, df: pd.DataFrame):
    """
    Store OHLCV candles into intraday_ohlcv table (MERGE upsert).

    Args:
        symbol: NSE:SYMBOL
        timeframe: '15min', '75min', or 'day'
        df: DataFrame with OHLCV columns and DatetimeIndex
    """
    if df is None or df.empty:
        return

    db = get_database()
    conn = db.get_connection()

    try:
        for idx, row in df.iterrows():
            candle_time = str(idx)
            conn.execute('''
                MERGE intraday_ohlcv AS target
                USING (SELECT ? AS symbol, ? AS timeframe, ? AS candle_time) AS source
                ON target.symbol = source.symbol
                   AND target.timeframe = source.timeframe
                   AND target.candle_time = source.candle_time
                WHEN MATCHED THEN
                    UPDATE SET [open] = ?, high = ?, low = ?, [close] = ?, volume = ?
                WHEN NOT MATCHED THEN
                    INSERT (symbol, timeframe, candle_time, [open], high, low, [close], volume)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            ''', (
                symbol, timeframe, candle_time,
                float(row['Open']), float(row['High']), float(row['Low']), float(row['Close']), int(row['Volume']),
                symbol, timeframe, candle_time,
                float(row['Open']), float(row['High']), float(row['Low']), float(row['Close']), int(row['Volume'])
            ))

        conn.commit()
    except Exception as e:
        print(f"  Error storing OHLCV for {symbol}/{timeframe}: {e}")
        conn.rollback()
    finally:
        conn.close()


def store_indicators_latest(symbol: str, timeframe: str, indicators: Dict):
    """
    Store latest indicator values into intraday_indicators table (MERGE upsert).

    Args:
        symbol: NSE:SYMBOL
        timeframe: '15min', '75min', or 'day'
        indicators: Dict from calculate_indicators_for_timeframe()
    """
    if not indicators:
        return

    db = get_database()
    conn = db.get_connection()

    candle_time = indicators.get('candle_time', str(datetime.now()))

    try:
        conn.execute('''
            MERGE intraday_indicators AS target
            USING (SELECT ? AS symbol, ? AS timeframe, ? AS candle_time) AS source
            ON target.symbol = source.symbol
               AND target.timeframe = source.timeframe
               AND target.candle_time = source.candle_time
            WHEN MATCHED THEN
                UPDATE SET
                    ema_13 = ?, ema_22 = ?, ema_50 = ?,
                    macd_line = ?, macd_signal = ?, macd_histogram = ?,
                    rsi = ?, atr = ?, force_index = ?,
                    stochastic = ?, stoch_d = ?, impulse_color = ?,
                    kc_upper = ?, kc_middle = ?, kc_lower = ?
            WHEN NOT MATCHED THEN
                INSERT (symbol, timeframe, candle_time,
                        ema_13, ema_22, ema_50,
                        macd_line, macd_signal, macd_histogram,
                        rsi, atr, force_index,
                        stochastic, stoch_d, impulse_color,
                        kc_upper, kc_middle, kc_lower)
                VALUES (?, ?, ?,
                        ?, ?, ?,
                        ?, ?, ?,
                        ?, ?, ?,
                        ?, ?, ?,
                        ?, ?, ?);
        ''', (
            symbol, timeframe, candle_time,
            # UPDATE values
            indicators.get('ema_13'), indicators.get('ema_22'), indicators.get('ema_50'),
            indicators.get('macd_line'), indicators.get('macd_signal'), indicators.get('macd_histogram'),
            indicators.get('rsi'), indicators.get('atr'), indicators.get('force_index'),
            indicators.get('stochastic'), indicators.get('stoch_d'), indicators.get('impulse_color'),
            indicators.get('kc_upper'), indicators.get('kc_middle'), indicators.get('kc_lower'),
            # INSERT values
            symbol, timeframe, candle_time,
            indicators.get('ema_13'), indicators.get('ema_22'), indicators.get('ema_50'),
            indicators.get('macd_line'), indicators.get('macd_signal'), indicators.get('macd_histogram'),
            indicators.get('rsi'), indicators.get('atr'), indicators.get('force_index'),
            indicators.get('stochastic'), indicators.get('stoch_d'), indicators.get('impulse_color'),
            indicators.get('kc_upper'), indicators.get('kc_middle'), indicators.get('kc_lower')
        ))

        conn.commit()
    except Exception as e:
        print(f"  Error storing indicators for {symbol}/{timeframe}: {e}")
        conn.rollback()
    finally:
        conn.close()


def refresh_symbol_timeframes(symbol: str) -> Dict:
    """
    Refresh all timeframe data for a single symbol:
      1. Fetch 15-min candles (5 days) → store
      2. Aggregate to 75-min → store
      3. Fetch daily candles (120 days) → store
      4. Calculate indicators for each timeframe → store

    Args:
        symbol: Bare symbol (e.g., 'RELIANCE') or NSE:SYMBOL format.
                Kite API calls use NSE:SYMBOL internally.
                DB storage always uses bare symbol.

    Returns:
        Dict with status per timeframe
    """
    # Normalize: strip NSE: prefix for DB storage, add it for Kite API
    bare_symbol = symbol.replace('NSE:', '').strip().upper()
    kite_symbol = f'NSE:{bare_symbol}'

    result = {'symbol': bare_symbol, '15min': False, '75min': False, 'day': False}

    try:
        # 1. Fetch and store 15-min candles (Kite needs NSE:SYMBOL)
        df_15 = fetch_15min_candles(kite_symbol, days=5)
        if df_15 is not None and not df_15.empty:
            store_ohlcv_batch(bare_symbol, '15min', df_15)
            ind_15 = calculate_indicators_for_timeframe(df_15)
            if ind_15:
                store_indicators_latest(bare_symbol, '15min', ind_15)
            result['15min'] = True
            print(f"  {bare_symbol} 15min: {len(df_15)} candles")

        # 2. Aggregate 15-min → 75-min and store
        if df_15 is not None and not df_15.empty:
            df_75 = aggregate_75min_from_15min(df_15)
            if not df_75.empty:
                store_ohlcv_batch(bare_symbol, '75min', df_75)
                ind_75 = calculate_indicators_for_timeframe(df_75)
                if ind_75:
                    store_indicators_latest(bare_symbol, '75min', ind_75)
                result['75min'] = True
                print(f"  {bare_symbol} 75min: {len(df_75)} candles")

        # 3. Fetch and store daily candles (Kite needs NSE:SYMBOL)
        df_day = fetch_daily_candles(kite_symbol, days=120)
        if df_day is not None and not df_day.empty:
            store_ohlcv_batch(bare_symbol, 'day', df_day)
            ind_day = calculate_indicators_for_timeframe(df_day)
            if ind_day:
                store_indicators_latest(bare_symbol, 'day', ind_day)
            result['day'] = True
            print(f"  {bare_symbol} day: {len(df_day)} candles")

    except Exception as e:
        print(f"  Error refreshing {bare_symbol}: {e}")
        traceback.print_exc()

    return result


def refresh_all_timeframes(symbols: List[str]) -> Dict:
    """
    Refresh all timeframe data for all trading watchlist symbols.

    Args:
        symbols: List of NSE:SYMBOL strings

    Returns:
        Dict with overall stats
    """
    start = datetime.now()
    results = []
    errors = []

    print(f"\n{'='*60}")
    print(f"  Refreshing {len(symbols)} symbols — {start.strftime('%H:%M:%S')}")
    print(f"{'='*60}")

    for i, symbol in enumerate(symbols):
        print(f"\n[{i+1}/{len(symbols)}] {symbol}")
        try:
            r = refresh_symbol_timeframes(symbol)
            results.append(r)
        except Exception as e:
            print(f"  FAILED: {e}")
            errors.append({'symbol': symbol, 'error': str(e)})

    elapsed = (datetime.now() - start).total_seconds()
    success_count = sum(1 for r in results if r.get('day'))

    summary = {
        'total': len(symbols),
        'success': success_count,
        'errors': len(errors),
        'elapsed_seconds': round(elapsed, 1),
        'details': results,
        'error_list': errors
    }

    print(f"\n{'='*60}")
    print(f"  Done: {success_count}/{len(symbols)} symbols in {elapsed:.1f}s")
    print(f"{'='*60}\n")

    return summary


def get_latest_indicators(symbol: str, timeframe: str) -> Optional[Dict]:
    """
    Read latest stored indicators from database.

    Args:
        symbol: Bare symbol (e.g., 'RELIANCE') or NSE:SYMBOL format
        timeframe: '15min', '75min', or 'day'

    Returns:
        Dict with indicator values or None
    """
    # Normalize to bare symbol for DB lookup
    bare_symbol = symbol.replace('NSE:', '').strip().upper()

    db = get_database()
    conn = db.get_connection()

    try:
        row = conn.execute('''
            SELECT TOP 1 * FROM intraday_indicators
            WHERE symbol = ? AND timeframe = ?
            ORDER BY candle_time DESC
        ''', (bare_symbol, timeframe)).fetchone()

        if row:
            return dict(row)
        return None
    finally:
        conn.close()


def get_ohlcv_history(symbol: str, timeframe: str, limit: int = 100) -> List[Dict]:
    """
    Read stored OHLCV candles from database.

    Args:
        symbol: Bare symbol (e.g., 'RELIANCE') or NSE:SYMBOL format
        timeframe: '15min', '75min', or 'day'
        limit: Maximum number of candles to return

    Returns:
        List of OHLCV dicts (newest first)
    """
    # Normalize to bare symbol for DB lookup
    bare_symbol = symbol.replace('NSE:', '').strip().upper()

    db = get_database()
    conn = db.get_connection()

    try:
        rows = conn.execute('''
            SELECT TOP (?) symbol, timeframe, candle_time,
                   [open], high, low, [close], volume
            FROM intraday_ohlcv
            WHERE symbol = ? AND timeframe = ?
            ORDER BY candle_time DESC
        ''', (limit, bare_symbol, timeframe)).fetchall()

        return [dict(r) for r in rows]
    finally:
        conn.close()
