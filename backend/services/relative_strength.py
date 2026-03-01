"""
Elder Trading System - Mansfield Relative Strength Scanner
==========================================================

Scans NIFTY 500 stocks for relative strength using Mansfield RS formula:
  RS = (Stock/Benchmark) / SMA(Stock/Benchmark, N) - 1

Filter Rules (ALL must pass):
  a. Stock outperforms Nifty (market benchmark) in both 1W and 3M
  b. Stock outperforms its Sector Index in both 1W and 3M
  c. Static RS > 0 AND Adaptive RS > 0
  d. RS Market (55 days) > 0 AND RS Sector (55 days) > 0
  e. Close within 15% of 52-Week High

Output: 20 columns per stock (see scan_relative_strength return format).
"""

import json
import os
import traceback
from typing import Dict, List, Optional

from services.kite_client import get_client


def calculate_mansfield_rs(stock_prices: List[float], benchmark_prices: List[float], period: int = 52) -> Optional[float]:
    """
    Mansfield Relative Strength:
      RS = (current_ratio / SMA(ratio, period)) - 1

    Positive = outperforming, Negative = underperforming.
    Returns value * 100 for readability.
    """
    if len(stock_prices) < period or len(benchmark_prices) < period:
        return None

    ratios = []
    for s, b in zip(stock_prices[-period:], benchmark_prices[-period:]):
        if b > 0:
            ratios.append(s / b)

    if len(ratios) < period // 2:
        return None

    sma = sum(ratios) / len(ratios)
    current_ratio = ratios[-1]

    return (current_ratio / sma - 1) * 100 if sma > 0 else 0


def calculate_adaptive_rs(stock_prices: List[float], benchmark_prices: List[float], period: int = 52, ema_span: int = 21) -> Optional[float]:
    """
    Adaptive Relative Strength:
      Uses EMA instead of SMA for the denominator.

    More responsive to recent changes.
    """
    if len(stock_prices) < period or len(benchmark_prices) < period:
        return None

    ratios = []
    for s, b in zip(stock_prices[-period:], benchmark_prices[-period:]):
        if b > 0:
            ratios.append(s / b)

    if len(ratios) < ema_span:
        return None

    # EMA calculation
    multiplier = 2 / (ema_span + 1)
    ema = ratios[0]
    for r in ratios[1:]:
        ema = (r - ema) * multiplier + ema

    current_ratio = ratios[-1]
    return (current_ratio / ema - 1) * 100 if ema > 0 else 0


def get_change_percent(prices: List[float], days: int) -> float:
    """Percentage change over last N trading days."""
    if len(prices) < days + 1:
        return 0
    old_price = prices[-(days + 1)]
    if old_price <= 0:
        return 0
    return ((prices[-1] / old_price) - 1) * 100


def get_52_week_high(prices: List[float]) -> float:
    """Get 52-week (260 trading days) high."""
    lookback = min(260, len(prices))
    return max(prices[-lookback:]) if lookback > 0 else 0


def load_sector_mapping() -> Dict:
    """Load sector mapping from JSON file."""
    json_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'sector_mapping.json')
    if not os.path.exists(json_path):
        print(f"  Sector mapping not found at {json_path}")
        return {}

    with open(json_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def _fetch_historical_prices(client, symbol: str, days: int = 365) -> List[float]:
    """
    Fetch daily close prices for a symbol from Kite historical data.
    Returns list of close prices (oldest first).
    """
    from datetime import datetime, timedelta

    try:
        to_date = datetime.now()
        from_date = to_date - timedelta(days=days)

        # Get instrument token
        instruments = client.kite.instruments('NSE')
        token = None
        for inst in instruments:
            if inst['tradingsymbol'] == symbol:
                token = inst['instrument_token']
                break

        if not token:
            return []

        data = client.kite.historical_data(
            token,
            from_date.strftime('%Y-%m-%d'),
            to_date.strftime('%Y-%m-%d'),
            'day'
        )

        return [candle['close'] for candle in data]

    except Exception as e:
        # Silently skip — many symbols may fail
        return []


def _fetch_index_prices(client, index_symbol: str, days: int = 365) -> List[float]:
    """
    Fetch daily close prices for an NSE index.
    Common indices: NIFTY 50, NIFTY BANK, NIFTY IT, etc.
    """
    from datetime import datetime, timedelta

    try:
        to_date = datetime.now()
        from_date = to_date - timedelta(days=days)

        instruments = client.kite.instruments('NSE')
        token = None
        for inst in instruments:
            if inst['tradingsymbol'] == index_symbol and inst['segment'] == 'NSE':
                token = inst['instrument_token']
                break

        # Try indices segment if not found in NSE
        if not token:
            try:
                idx_instruments = client.kite.instruments('INDICES')
                for inst in idx_instruments:
                    if inst['tradingsymbol'] == index_symbol:
                        token = inst['instrument_token']
                        break
            except Exception:
                pass

        if not token:
            return []

        data = client.kite.historical_data(
            token,
            from_date.strftime('%Y-%m-%d'),
            to_date.strftime('%Y-%m-%d'),
            'day'
        )

        return [candle['close'] for candle in data]

    except Exception as e:
        return []


def scan_relative_strength(sector_map: Dict = None) -> List[Dict]:
    """
    Main scanner. For each stock in sector_map:
    1. Fetch 1-year daily close prices for stock, Nifty, sector index
    2. Calculate all RS metrics
    3. Apply filter rules (ALL must pass)
    4. Return qualifying stocks with all 20 columns

    Returns list of dicts with keys:
        name, symbol, exchange, sector, industry, close, change_pct, mcap,
        change_1w, change_3m, mkt_1w, mkt_3m, sect_1w, sect_3m,
        static_rs, adaptive_rs, rs_market_55, rs_sector_55, high, high_52w
    """
    if not sector_map:
        sector_map = load_sector_mapping()

    if not sector_map:
        return []

    client = get_client()
    if not client or not client._authenticated:
        print("  RS Scanner: Kite not authenticated")
        return []

    results = []

    # 1. Fetch Nifty 50 prices (market benchmark)
    print("  Fetching NIFTY 50 index prices...")
    nifty_prices = _fetch_index_prices(client, 'NIFTY 50', 365)
    if len(nifty_prices) < 55:
        print(f"  Insufficient NIFTY data ({len(nifty_prices)} days), aborting")
        return []

    nifty_1w = get_change_percent(nifty_prices, 5)
    nifty_3m = get_change_percent(nifty_prices, 63)

    # 2. Cache sector index prices
    sector_cache = {}
    unique_sectors = set(info.get('sector_index', '') for info in sector_map.values() if info.get('sector_index'))
    for sect_idx in unique_sectors:
        print(f"  Fetching {sect_idx} prices...")
        sector_cache[sect_idx] = _fetch_index_prices(client, sect_idx, 365)

    # 3. Scan each stock
    total = len(sector_map)
    scanned = 0

    for symbol, info in sector_map.items():
        scanned += 1
        if scanned % 50 == 0:
            print(f"  Scanning... {scanned}/{total}")

        try:
            stock_prices = _fetch_historical_prices(client, symbol, 365)
            if len(stock_prices) < 55:
                continue

            close = stock_prices[-1]

            # Change percentages
            stock_1w = get_change_percent(stock_prices, 5)
            stock_3m = get_change_percent(stock_prices, 63)
            change_pct = get_change_percent(stock_prices, 1)

            # Sector index data
            sect_idx = info.get('sector_index', '')
            sect_prices = sector_cache.get(sect_idx, [])
            sect_1w = get_change_percent(sect_prices, 5) if len(sect_prices) > 5 else 0
            sect_3m = get_change_percent(sect_prices, 63) if len(sect_prices) > 63 else 0

            # RS calculations
            static_rs = calculate_mansfield_rs(stock_prices, nifty_prices, 52)
            adaptive_rs = calculate_adaptive_rs(stock_prices, nifty_prices, 52, 21)
            rs_market_55 = calculate_mansfield_rs(stock_prices, nifty_prices, 55)

            # RS vs sector (if sector data available)
            if len(sect_prices) >= 55:
                rs_sector_55 = calculate_mansfield_rs(stock_prices, sect_prices, 55)
            else:
                rs_sector_55 = None

            # 52-week high
            high_52w = get_52_week_high(stock_prices)
            high = max(stock_prices[-5:]) if len(stock_prices) >= 5 else close

            # ── FILTER RULES (ALL must pass) ──

            # a. Stock outperforms Nifty in both 1W and 3M
            if stock_1w <= nifty_1w or stock_3m <= nifty_3m:
                continue

            # b. Stock outperforms sector in both 1W and 3M
            if sect_1w != 0 or sect_3m != 0:  # Only apply if sector data exists
                if stock_1w <= sect_1w or stock_3m <= sect_3m:
                    continue

            # c. Static RS > 0 AND Adaptive RS > 0
            if static_rs is None or adaptive_rs is None:
                continue
            if static_rs <= 0 or adaptive_rs <= 0:
                continue

            # d. RS Market (55 days) > 0 AND RS Sector (55 days) > 0
            if rs_market_55 is None or rs_market_55 <= 0:
                continue
            if rs_sector_55 is not None and rs_sector_55 <= 0:
                continue

            # e. Close within 15% of 52-week high
            if high_52w > 0 and close < high_52w * 0.85:
                continue

            # Passed all filters — add to results
            results.append({
                'name': info.get('name', symbol),
                'symbol': symbol,
                'exchange': 'NSE',
                'sector': info.get('sector', ''),
                'industry': info.get('industry', ''),
                'close': round(close, 2),
                'change_pct': round(change_pct, 2),
                'mcap': info.get('mcap', 0),
                'change_1w': round(stock_1w, 2),
                'change_3m': round(stock_3m, 2),
                'mkt_1w': round(nifty_1w, 2),
                'mkt_3m': round(nifty_3m, 2),
                'sect_1w': round(sect_1w, 2),
                'sect_3m': round(sect_3m, 2),
                'static_rs': round(static_rs, 2),
                'adaptive_rs': round(adaptive_rs, 2),
                'rs_market_55': round(rs_market_55, 2),
                'rs_sector_55': round(rs_sector_55, 2) if rs_sector_55 is not None else 0,
                'high': round(high, 2),
                'high_52w': round(high_52w, 2),
            })

        except Exception as e:
            # Skip individual stock errors
            continue

    print(f"  RS Scan complete: {len(results)} stocks passed all filters out of {total}")

    # Sort by static RS descending
    results.sort(key=lambda x: x.get('static_rs', 0), reverse=True)

    return results
