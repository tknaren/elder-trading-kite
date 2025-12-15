"""
Elder Trading System - Kite Connect API Data Provider
======================================================

Provides market data from Kite Connect API for NSE stocks.

PREREQUISITES:
1. Install kiteconnect: pip install kiteconnect
2. Get API Key and Secret from Kite Connect Developer Console
3. Login daily to get the request token
4. Exchange request token for access token

Symbol Format: NSE:RELIANCE, NSE:TCS, etc.
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta, time
from typing import Optional, Dict, List, Tuple, Any
import pytz

# Will be imported when kiteconnect is installed
try:
    from kiteconnect import KiteConnect
except ImportError:
    KiteConnect = None


def convert_to_native(obj: Any) -> Any:
    """Convert numpy types to native Python types for JSON serialization"""
    if isinstance(obj, dict):
        return {k: convert_to_native(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [convert_to_native(item) for item in obj]
    elif isinstance(obj, (np.bool_,)):
        return bool(obj)
    elif isinstance(obj, (np.integer,)):
        return int(obj)
    elif isinstance(obj, (np.floating,)):
        return float(obj) if not np.isnan(obj) else None
    elif isinstance(obj, (np.ndarray,)):
        return obj.tolist()
    elif pd.isna(obj):
        return None
    return obj


# NSE Trading Hours (IST)
NSE_MARKET_OPEN = time(9, 15)  # 9:15 AM IST
NSE_MARKET_CLOSE = time(15, 30)  # 3:30 PM IST

# In-memory session cache for OHLCV data (avoids repeated DB reads)
_session_ohlcv_cache = {}
_session_cache_date = None  # Track when cache was created
IST = pytz.timezone('Asia/Kolkata')


def is_nse_market_open() -> Tuple[bool, str]:
    """
    Check if NSE market is currently open

    Returns:
        Tuple of (is_open: bool, message: str)
    """
    now = datetime.now(IST)
    current_time = now.time()
    weekday = now.weekday()

    # Weekend check (Saturday=5, Sunday=6)
    if weekday >= 5:
        return False, "NSE is closed on weekends"

    # Time check
    if current_time < NSE_MARKET_OPEN:
        return False, f"NSE opens at 9:15 AM IST (current: {current_time.strftime('%H:%M')})"

    if current_time > NSE_MARKET_CLOSE:
        return False, f"NSE closed at 3:30 PM IST (current: {current_time.strftime('%H:%M')})"

    return True, "NSE market is open"


def get_market_status() -> Dict:
    """Get detailed market status information"""
    is_open, message = is_nse_market_open()
    now = datetime.now(IST)

    return {
        'is_open': is_open,
        'message': message,
        'current_time_ist': now.strftime('%Y-%m-%d %H:%M:%S IST'),
        'market_open': '09:15 IST',
        'market_close': '15:30 IST',
        'weekday': now.strftime('%A')
    }


class KiteClient:
    """
    Kite Connect API Client

    Handles all communication with Kite Connect for market data and orders.
    """

    def __init__(self, api_key: str = None, api_secret: str = None, access_token: str = None):
        self.api_key = api_key
        self.api_secret = api_secret
        self.access_token = access_token
        self.kite = None
        self._instrument_cache: Dict[str, int] = {}
        self._authenticated = False

        if api_key:
            self._init_kite()

    def _init_kite(self):
        """Initialize Kite Connect client"""
        if KiteConnect is None:
            raise ImportError(
                "kiteconnect package not installed. Run: pip install kiteconnect")

        self.kite = KiteConnect(api_key=self.api_key)

        if self.access_token:
            self.kite.set_access_token(self.access_token)
            self._authenticated = True

    def get_login_url(self) -> str:
        """Get Kite login URL for authentication"""
        if not self.kite:
            self._init_kite()
        return self.kite.login_url()

    def generate_session(self, request_token: str) -> Dict:
        """
        Generate access token from request token

        Args:
            request_token: Token received after user login

        Returns:
            Session data including access_token
        """
        if not self.kite:
            self._init_kite()

        try:
            data = self.kite.generate_session(
                request_token, api_secret=self.api_secret)
            self.access_token = data['access_token']
            self.kite.set_access_token(self.access_token)
            self._authenticated = True
            return {
                'success': True,
                'access_token': self.access_token,
                'user_id': data.get('user_id'),
                'user_name': data.get('user_name'),
                'email': data.get('email'),
                'broker': data.get('broker', 'ZERODHA')
            }
        except Exception as e:
            return {
                'success': False,
                'error': str(e)
            }

    def set_access_token(self, access_token: str):
        """Set access token for authenticated requests"""
        self.access_token = access_token
        if self.kite:
            self.kite.set_access_token(access_token)
            self._authenticated = True

    def check_auth(self) -> bool:
        """Check if authenticated with Kite"""
        if not self.kite or not self.access_token:
            return False

        try:
            # Try to fetch profile to verify authentication
            profile = self.kite.profile()
            self._authenticated = profile is not None
            return self._authenticated
        except Exception:
            self._authenticated = False
            return False

    def get_profile(self) -> Optional[Dict]:
        """Get user profile"""
        if not self._authenticated:
            return None
        try:
            return self.kite.profile()
        except Exception:
            return None

    def get_instrument_token(self, symbol: str, exchange: str = 'NSE') -> Optional[int]:
        """
        Get instrument token for a symbol

        Args:
            symbol: Stock symbol (e.g., 'RELIANCE', 'TCS')
            exchange: Exchange (NSE or BSE)

        Returns:
            Instrument token or None
        """
        cache_key = f"{exchange}:{symbol}"
        if cache_key in self._instrument_cache:
            return self._instrument_cache[cache_key]

        try:
            # Fetch instruments if not cached
            instruments = self.kite.instruments(exchange)
            for inst in instruments:
                key = f"{inst['exchange']}:{inst['tradingsymbol']}"
                self._instrument_cache[key] = inst['instrument_token']

            return self._instrument_cache.get(cache_key)
        except Exception as e:
            print(f"Error fetching instrument token: {e}")
            return None

    def parse_symbol(self, symbol: str) -> Tuple[str, str]:
        """
        Parse symbol in format NSE:RELIANCE to (exchange, symbol)

        Args:
            symbol: Symbol in format 'NSE:RELIANCE' or just 'RELIANCE'

        Returns:
            Tuple of (exchange, tradingsymbol)
        """
        if ':' in symbol:
            parts = symbol.split(':')
            return parts[0], parts[1]
        return 'NSE', symbol

    def get_historical_data(self, symbol: str, interval: str = 'day',
                            days: int = 365) -> Optional[pd.DataFrame]:
        """
        Get historical OHLCV data

        Args:
            symbol: Stock symbol (e.g., 'NSE:RELIANCE' or 'RELIANCE')
            interval: Candle interval ('minute', '3minute', '5minute', '15minute',
                      '30minute', '60minute', 'day', 'week', 'month')
            days: Number of days of history to fetch

        Returns:
            DataFrame with OHLCV data or None
        """
        if not self._authenticated:
            print(f"❌ Not authenticated with Kite")
            return None

        exchange, tradingsymbol = self.parse_symbol(symbol)
        instrument_token = self.get_instrument_token(tradingsymbol, exchange)

        if not instrument_token:
            print(f"❌ {symbol}: Could not find instrument token")
            return None

        try:
            to_date = datetime.now()
            from_date = to_date - timedelta(days=days)

            data = self.kite.historical_data(
                instrument_token,
                from_date,
                to_date,
                interval
            )

            if not data:
                return None

            df = pd.DataFrame(data)

            # Rename columns to match expected format
            column_map = {
                'date': 'Date',
                'open': 'Open',
                'high': 'High',
                'low': 'Low',
                'close': 'Close',
                'volume': 'Volume'
            }
            df = df.rename(columns=column_map)

            # Set Date as index
            if 'Date' in df.columns:
                df['Date'] = pd.to_datetime(df['Date'])
                df.set_index('Date', inplace=True)

            # Ensure numeric types
            for col in ['Open', 'High', 'Low', 'Close']:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors='coerce')

            if 'Volume' in df.columns:
                df['Volume'] = pd.to_numeric(
                    df['Volume'], errors='coerce').fillna(0).astype(int)

            return df

        except Exception as e:
            print(f"Error fetching historical data for {symbol}: {e}")
            return None

    def get_quote(self, symbols: List[str]) -> Dict:
        """
        Get current market quotes for symbols

        Args:
            symbols: List of symbols in format 'NSE:RELIANCE'

        Returns:
            Dict with quote data for each symbol
        """
        if not self._authenticated:
            return {}

        try:
            # Ensure symbols are in correct format
            formatted = []
            for s in symbols:
                if ':' not in s:
                    formatted.append(f'NSE:{s}')
                else:
                    formatted.append(s)

            return self.kite.quote(formatted)
        except Exception as e:
            print(f"Error fetching quotes: {e}")
            return {}

    def get_ltp(self, symbols: List[str]) -> Dict:
        """Get Last Traded Price for symbols"""
        if not self._authenticated:
            return {}

        try:
            formatted = []
            for s in symbols:
                if ':' not in s:
                    formatted.append(f'NSE:{s}')
                else:
                    formatted.append(s)

            return self.kite.ltp(formatted)
        except Exception as e:
            print(f"Error fetching LTP: {e}")
            return {}

    def get_market_snapshot(self, symbol: str) -> Optional[Dict]:
        """
        Get current market data snapshot for a symbol

        Args:
            symbol: Stock symbol (e.g., 'NSE:RELIANCE')

        Returns:
            Dict with last, bid, ask, high, low, volume, open
        """
        if not self._authenticated:
            return None

        try:
            if ':' not in symbol:
                symbol = f'NSE:{symbol}'

            quote = self.kite.quote([symbol])

            if symbol in quote:
                q = quote[symbol]
                ohlc = q.get('ohlc', {})
                return {
                    'last': q.get('last_price'),
                    'bid': q.get('depth', {}).get('buy', [{}])[0].get('price'),
                    'ask': q.get('depth', {}).get('sell', [{}])[0].get('price'),
                    'high': ohlc.get('high'),
                    'low': ohlc.get('low'),
                    'open': ohlc.get('open'),
                    'close': ohlc.get('close'),  # Previous close
                    'volume': q.get('volume'),
                    'change': q.get('change'),
                    'change_percent': (q.get('change') / ohlc.get('close', 1) * 100) if (q.get('change') is not None and ohlc.get('close')) else 0
                }
            return None
        except Exception as e:
            print(f"Error fetching snapshot for {symbol}: {e}")
            return None


# Global client instance
_client: Optional[KiteClient] = None


def get_client() -> KiteClient:
    """Get or create Kite client instance"""
    global _client
    if _client is None:
        _client = KiteClient()
    return _client


def init_client(api_key: str, api_secret: str, access_token: str = None) -> KiteClient:
    """Initialize Kite client with credentials"""
    global _client
    _client = KiteClient(api_key, api_secret, access_token)
    return _client


def check_connection() -> Tuple[bool, str]:
    """Check Kite Connect connection status"""
    client = get_client()

    if not client.api_key:
        return False, "Kite Connect not configured. Please add API Key and Secret in Settings."

    if not client.access_token:
        return False, "Not logged in. Please login to Kite and enter the request token."

    try:
        if client.check_auth():
            return True, "Connected to Kite Connect"
        else:
            return False, "Session expired. Please login again to Kite."
    except Exception as e:
        return False, f"Connection error: {str(e)}"


def fetch_stock_data(symbol: str, period: str = '2y') -> Optional[Dict]:
    """
    Fetch stock data with multi-layer caching:
    1. In-memory session cache (fastest - no DB hit)
    2. Database cache (persisted across sessions)
    3. Kite API (only if cache miss)

    Args:
        symbol: Stock symbol in format 'NSE:RELIANCE' or 'RELIANCE'
        period: Time period (2y, 1y, 6m, etc.)

    Returns:
        Dict with symbol, name, sector, history DataFrame, snapshot
    """
    from models.database import get_database
    global _session_ohlcv_cache, _session_cache_date

    client = get_client()

    # Check authentication
    if not client.check_auth():
        print(f"❌ {symbol}: Not authenticated with Kite Connect")
        return None

    # Parse period to days
    period_map = {
        '2y': 730, '1y': 365, '6m': 180, '3m': 90, '1m': 30
    }
    days = period_map.get(period, 365)

    # Ensure symbol format
    exchange, tradingsymbol = client.parse_symbol(symbol)
    full_symbol = f"{exchange}:{tradingsymbol}"

    # Check if session cache is stale (new day)
    today = datetime.now().strftime('%Y-%m-%d')
    if _session_cache_date != today:
        _session_ohlcv_cache = {}
        _session_cache_date = today

    # Check in-memory session cache first (fastest)
    if full_symbol in _session_ohlcv_cache:
        cached = _session_ohlcv_cache[full_symbol]
        # Return cached data with fresh snapshot
        snapshot = client.get_market_snapshot(full_symbol)
        return {
            'symbol': full_symbol,
            'name': cached['name'],
            'sector': cached['sector'],
            'history': cached['history'].copy(),
            'info': {},
            'snapshot': snapshot,
            'instrument_token': client.get_instrument_token(tradingsymbol, exchange)
        }

    db = get_database().get_connection()

    # Check if we have fresh OHLCV cache (< 1 day old)
    sync_row = db.execute(
        'SELECT * FROM stock_data_sync WHERE symbol = ?',
        (full_symbol,)
    ).fetchone()

    use_cache = False
    hist = None
    today_str = datetime.now().strftime('%Y-%m-%d')

    if sync_row:
        last_updated = datetime.fromisoformat(sync_row['last_updated'])
        latest_date = sync_row['latest_date']

        # Use cache if updated within 24 hours AND has today's date (or market is closed)
        if datetime.now() - last_updated < timedelta(hours=24):
            use_cache = True

    # Try to use cached data
    if use_cache:
        cached_rows = db.execute('''
            SELECT date, open, high, low, close, volume
            FROM stock_historical_data
            WHERE symbol = ?
            ORDER BY date ASC
        ''', (full_symbol,)).fetchall()

        if cached_rows and len(cached_rows) >= 30:
            hist = pd.DataFrame([
                {
                    'Date': row['date'],
                    'Open': row['open'],
                    'High': row['high'],
                    'Low': row['low'],
                    'Close': row['close'],
                    'Volume': row['volume']
                }
                for row in cached_rows
            ])
            hist['Date'] = pd.to_datetime(hist['Date'])
            hist.set_index('Date', inplace=True)
            # Silent cache hit - no logging needed

    # If cache miss or insufficient data, fetch from Kite
    if hist is None or hist.empty or len(hist) < 30:
        print(f"🔄 {full_symbol}: Fetching from Kite...")
        hist = client.get_historical_data(
            full_symbol, interval='day', days=days)

        if hist is None or hist.empty or len(hist) < 30:
            print(f"❌ {full_symbol}: Insufficient data")
            db.close()
            return None

        # Cache the OHLCV data - only new rows
        new_cached = 0
        for date, row in hist.iterrows():
            date_str = date.strftime('%Y-%m-%d')
            # Check if already exists
            existing = db.execute(
                'SELECT 1 FROM stock_historical_data WHERE symbol = ? AND date = ?',
                (full_symbol, date_str)
            ).fetchone()

            if not existing:
                db.execute('''
                    INSERT INTO stock_historical_data
                    (symbol, date, open, high, low, close, volume)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (
                    full_symbol,
                    date_str,
                    float(row['Open']),
                    float(row['High']),
                    float(row['Low']),
                    float(row['Close']),
                    int(row['Volume'])
                ))
                new_cached += 1

        earliest_date = hist.index.min().strftime('%Y-%m-%d')
        latest_date = hist.index.max().strftime('%Y-%m-%d')

        db.execute('''
            INSERT OR REPLACE INTO stock_data_sync
            (symbol, last_updated, earliest_date, latest_date, record_count)
            VALUES (?, ?, ?, ?, ?)
        ''', (full_symbol, datetime.now().isoformat(), earliest_date, latest_date, len(hist)))
        db.commit()

        if new_cached > 0:
            print(f"✓ {full_symbol}: Cached {new_cached} new bars")

    # Get current snapshot
    snapshot = client.get_market_snapshot(full_symbol)

    # Get instrument info for name (from cached instruments)
    name = tradingsymbol  # Default to symbol
    sector = 'Unknown'

    db.close()

    return {
        'symbol': full_symbol,
        'name': name,
        'sector': sector,
        'history': hist,
        'info': {},
        'snapshot': snapshot,
        'instrument_token': client.get_instrument_token(tradingsymbol, exchange)
    }


# Test function
if __name__ == "__main__":
    print("=" * 60)
    print("  Kite Connect API - Test")
    print("=" * 60)

    # Check market status
    status = get_market_status()
    print(f"\nMarket Status: {status['message']}")
    print(f"Current Time: {status['current_time_ist']}")

    connected, message = check_connection()
    print(f"\n{message}")

    if not connected:
        print("\nTo connect:")
        print("1. Add API Key and Secret in Settings")
        print("2. Click 'Login to Kite' button")
        print("3. Complete login and paste the request token")
