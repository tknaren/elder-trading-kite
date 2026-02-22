"""
Elder Trading System - Enhanced API Routes v2
New endpoints for connected workflow: Screener → Trade Bill → Kite Connect → Trade Log → Position Management

Data Source: Kite Connect API (NSE)
Symbol Format: NSE:SYMBOL (e.g., NSE:RELIANCE, NSE:TCS)
"""

from flask import Blueprint, request, jsonify, g
from datetime import datetime, timedelta
import json
import logging

logger = logging.getLogger(__name__)

from models.database import get_database
from services.screener_v2 import (
    run_weekly_screen_v2,
    run_daily_screen_v2,
    scan_stock_v2,
    calculate_elder_trade_levels
)
from services.indicators import get_grading_criteria
from services.kite_orders import (
    check_kite_connection,
    get_account_info,
    place_order,
    place_gtt_order,
    place_gtt_oco,
    get_gtt_orders,
    cancel_gtt,
    get_open_orders,
    cancel_order,
    modify_order,
    get_positions,
    get_holdings,
    get_position_alerts,
    get_filled_trades,
    create_trade_from_bill,
    check_trading_hours,
    TRANSACTION_BUY,
    TRANSACTION_SELL,
    ORDER_TYPE_LIMIT,
    ORDER_TYPE_MARKET,
    PRODUCT_CNC
)
from services.kite_client import (
    get_client,
    init_client,
    get_market_status
)
from services.nse_charges import (
    estimate_trade_charges,
    calculate_break_even
)

api_v2 = Blueprint('api_v2', __name__, url_prefix='/api/v2')


def get_db():
    if 'db' not in g:
        g.db = get_database().get_connection()
    return g.db


def get_user_id():
    """Get current user ID (defaults to first user in database)"""
    if hasattr(g, 'user_id'):
        return g.user_id

    # Get first user from database
    db = get_db()
    user = db.execute('SELECT TOP 1 id FROM users ORDER BY id').fetchone()
    if user:
        g.user_id = user['id']
        return user['id']
    return 1  # Fallback


@api_v2.teardown_app_request
def close_db_connection(error):
    """Close database connection at end of request"""
    db = g.pop('db', None)
    if db is not None:
        db.close()


# ══════════════════════════════════════════════════════════════════════════════
# DATA MANAGEMENT ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@api_v2.route('/data/load', methods=['POST'])
def load_market_data():
    """
    Bulk load OHLCV data and pre-calculate all indicators for NSE 500 stocks.
    Loads instruments from Kite, then fetches historical data for top 500.
    Called manually by user from Settings page after market close.

    This is the ONLY place where Kite API is called for historical data.
    All other features read from the database cache.

    Body params:
        force: bool - Force refresh even if loaded today
        limit: int - Max instruments to load (default 500)
    """
    from services.screener_v2 import calculate_all_indicators, analyze_weekly_trend, NIFTY_100
    from services.kite_client import get_client, clear_session_cache

    client = get_client()
    if not client or not client.kite or not client.access_token:
        return jsonify({
            'success': False,
            'error': 'Not authenticated with Kite Connect. Please login first.'
        }), 401

    db = get_db()
    user_id = get_user_id()
    req_data = request.get_json() or {}

    # Check last refresh date
    sync_info = db.execute('''
        SELECT MAX(last_updated) as last_refresh
        FROM stock_data_sync
    ''').fetchone()

    last_refresh = sync_info['last_refresh'] if sync_info else None
    today = datetime.now().strftime('%Y-%m-%d')

    # Check if already loaded today (allow force refresh via request body)
    force = req_data.get('force', False)
    if last_refresh and not force:
        # Handle both datetime objects and strings
        if hasattr(last_refresh, 'strftime'):
            last_date = last_refresh.strftime('%Y-%m-%d')
        else:
            last_date = str(last_refresh)[:10]  # Extract date part from string
        if last_date == today:
            return jsonify({
                'success': True,
                'message': 'Data already loaded for today',
                'last_refresh': last_refresh,
                'symbols_loaded': 0,
                'skipped': True
            })

    # Clear session cache to force fresh load
    clear_session_cache()

    # Step 1: Ensure instruments are loaded in cache
    cached_instruments = db.execute('''
        SELECT tradingsymbol FROM nse_instruments ORDER BY tradingsymbol
    ''').fetchall()

    if not cached_instruments:
        # Auto-load instruments from Kite if cache is empty
        try:
            client._rate_limit()
            instruments = client.kite.instruments('NSE')
            for inst in instruments:
                if inst.get('segment') != 'NSE' or inst.get('instrument_type') != 'EQ':
                    continue
                symbol = inst['tradingsymbol']
                name = inst.get('name', symbol)
                token = inst['instrument_token']
                lot_size = inst.get('lot_size', 1)
                tick_size = inst.get('tick_size', 0.05)
                db.execute('''
                    MERGE nse_instruments AS target
                    USING (SELECT ? AS tradingsymbol) AS source
                    ON target.tradingsymbol = source.tradingsymbol
                    WHEN MATCHED THEN
                        UPDATE SET name = ?, instrument_token = ?, lot_size = ?,
                                   tick_size = ?, updated_at = GETDATE()
                    WHEN NOT MATCHED THEN
                        INSERT (tradingsymbol, name, instrument_token, lot_size, tick_size)
                        VALUES (?, ?, ?, ?, ?);
                ''', (symbol, name, token, lot_size, tick_size,
                      symbol, name, token, lot_size, tick_size))
            db.commit()
            cached_instruments = db.execute('''
                SELECT tradingsymbol FROM nse_instruments ORDER BY tradingsymbol
            ''').fetchall()
            print(f"  Auto-loaded {len(cached_instruments)} instruments for market data load")
        except Exception as e:
            return jsonify({
                'success': False,
                'error': f'Failed to load instruments from Kite: {str(e)}'
            }), 500

    # Step 2: Build NSE 500 symbol list
    # Priority order: NIFTY 100 first, then remaining instruments up to 500
    max_symbols = int(req_data.get('limit', 500))
    all_cached = set(r['tradingsymbol'] for r in cached_instruments)

    # Start with NIFTY 100 (known liquid stocks)
    nifty_100_bare = [s.replace('NSE:', '') for s in NIFTY_100]
    priority_symbols = [s for s in nifty_100_bare if s in all_cached]

    # Add remaining cached instruments up to the limit
    remaining = sorted(all_cached - set(priority_symbols))
    symbol_list_bare = priority_symbols + remaining[:max(0, max_symbols - len(priority_symbols))]

    # Build NSE:SYMBOL format
    symbol_list = [f"NSE:{s}" for s in symbol_list_bare]
    print(f"📊 Loading market data for {len(symbol_list)} NSE instruments (limit: {max_symbols})")

    symbols_loaded = 0
    symbols_failed = []
    total = len(symbol_list)

    for i, symbol in enumerate(symbol_list):
        try:
            exchange, tradingsymbol = client.parse_symbol(symbol)
            full_symbol = f"{exchange}:{tradingsymbol}"

            # Fetch 2 years of historical data
            hist = client.get_historical_data(
                full_symbol, interval='day', days=730)

            if hist is None or hist.empty or len(hist) < 30:
                symbols_failed.append(symbol)
                continue

            # Save OHLCV data to database
            for date, row in hist.iterrows():
                date_str = date.strftime('%Y-%m-%d')
                db.execute('''
                    MERGE INTO stock_historical_data AS target
                    USING (SELECT ? AS symbol, ? AS date) AS source
                    ON target.symbol = source.symbol AND target.date = source.date
                    WHEN MATCHED THEN
                        UPDATE SET [open] = ?, high = ?, low = ?, [close] = ?, volume = ?
                    WHEN NOT MATCHED THEN
                        INSERT (symbol, date, [open], high, low, [close], volume)
                        VALUES (?, ?, ?, ?, ?, ?, ?);
                ''', (
                    full_symbol, date_str,
                    float(row['Open']), float(row['High']), float(
                        row['Low']), float(row['Close']), int(row['Volume']),
                    full_symbol, date_str,
                    float(row['Open']), float(row['High']), float(
                        row['Low']), float(row['Close']), int(row['Volume'])
                ))

            # Update sync record
            earliest_date = hist.index.min().strftime('%Y-%m-%d')
            latest_date = hist.index.max().strftime('%Y-%m-%d')
            db.execute('''
                MERGE INTO stock_data_sync AS target
                USING (SELECT ? AS symbol) AS source
                ON target.symbol = source.symbol
                WHEN MATCHED THEN
                    UPDATE SET last_updated = ?, earliest_date = ?, latest_date = ?, record_count = ?
                WHEN NOT MATCHED THEN
                    INSERT (symbol, last_updated, earliest_date, latest_date, record_count)
                    VALUES (?, ?, ?, ?, ?);
            ''', (full_symbol,
                  datetime.now().isoformat(), earliest_date, latest_date, len(hist),
                  full_symbol, datetime.now().isoformat(), earliest_date, latest_date, len(hist)))

            # Pre-calculate and cache ALL indicators
            indicators = calculate_all_indicators(
                hist['High'], hist['Low'], hist['Close'], hist['Volume']
            )

            # Save daily indicators
            latest_date_str = hist.index.max().strftime('%Y-%m-%d')
            latest_row = hist.iloc[-1]

            ind_values = (
                float(latest_row['Close']),
                float(indicators.get('ema_22', 0)),
                float(indicators.get('ema_50', 0)),
                float(indicators.get('ema_100', 0)),
                float(indicators.get('ema_200', 0)),
                float(indicators.get('macd_line', 0)),
                float(indicators.get('macd_signal', 0)),
                float(indicators.get('macd_histogram', 0)),
                float(indicators.get('rsi', 50)),
                float(indicators.get('stochastic_k', 50)),
                float(indicators.get('stochastic_d', 50)),
                float(indicators.get('atr', 0)),
                float(indicators.get('force_index_2', 0)),
                float(indicators.get('kc_upper', 0)),
                float(indicators.get('kc_middle', 0)),
                float(indicators.get('kc_lower', 0))
            )
            db.execute('''
                MERGE INTO stock_indicators_daily AS target
                USING (SELECT ? AS symbol, ? AS date) AS source
                ON target.symbol = source.symbol AND target.date = source.date
                WHEN MATCHED THEN
                    UPDATE SET [close] = ?, ema_22 = ?, ema_50 = ?, ema_100 = ?, ema_200 = ?,
                        macd_line = ?, macd_signal = ?, macd_histogram = ?, rsi = ?, stochastic = ?,
                        stoch_d = ?, atr = ?, force_index = ?, kc_upper = ?, kc_middle = ?, kc_lower = ?
                WHEN NOT MATCHED THEN
                    INSERT (symbol, date, [close], ema_22, ema_50, ema_100, ema_200,
                        macd_line, macd_signal, macd_histogram, rsi, stochastic,
                        stoch_d, atr, force_index, kc_upper, kc_middle, kc_lower)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            ''', (full_symbol, latest_date_str) + ind_values +
                 (full_symbol, latest_date_str) + ind_values)

            # Update indicator sync
            db.execute('''
                MERGE INTO stock_indicator_sync AS target
                USING (SELECT ? AS symbol) AS source
                ON target.symbol = source.symbol
                WHEN MATCHED THEN
                    UPDATE SET last_updated = ?, last_daily_date = ?, daily_record_count = 1
                WHEN NOT MATCHED THEN
                    INSERT (symbol, last_updated, last_daily_date, daily_record_count)
                    VALUES (?, ?, ?, 1);
            ''', (full_symbol, datetime.now().isoformat(), latest_date_str,
                  full_symbol, datetime.now().isoformat(), latest_date_str))

            symbols_loaded += 1

            # Commit every 10 symbols to avoid losing progress
            if symbols_loaded % 10 == 0:
                db.commit()
                print(f"✓ Loaded {symbols_loaded}/{total} symbols...")

        except Exception as e:
            print(f"❌ {symbol}: {str(e)}")
            symbols_failed.append(symbol)
            continue

    db.commit()

    # Update global refresh timestamp
    db.execute('''
        UPDATE account_settings
        SET last_data_refresh = ?, updated_at = GETDATE()
        WHERE user_id = ?
    ''', (datetime.now().isoformat(), user_id))
    db.commit()

    return jsonify({
        'success': True,
        'message': f'Loaded {symbols_loaded} symbols successfully',
        'symbols_loaded': symbols_loaded,
        'symbols_failed': symbols_failed,
        'last_refresh': datetime.now().isoformat()
    })


@api_v2.route('/data/status', methods=['GET'])
def get_data_status():
    """Get current data load status and last refresh date"""
    db = get_db()

    # Get last refresh from sync table
    sync_info = db.execute('''
        SELECT
            MAX(last_updated) as last_refresh,
            COUNT(DISTINCT symbol) as symbols_cached,
            MIN(earliest_date) as data_from,
            MAX(latest_date) as data_to
        FROM stock_data_sync
    ''').fetchone()

    # Get indicator cache status
    indicator_info = db.execute('''
        SELECT
            COUNT(DISTINCT symbol) as symbols_with_indicators,
            MAX(last_updated) as indicators_updated
        FROM stock_indicator_sync
    ''').fetchone()

    return jsonify({
        'last_refresh': sync_info['last_refresh'] if sync_info else None,
        'symbols_cached': sync_info['symbols_cached'] if sync_info else 0,
        'data_from': sync_info['data_from'] if sync_info else None,
        'data_to': sync_info['data_to'] if sync_info else None,
        'symbols_with_indicators': indicator_info['symbols_with_indicators'] if indicator_info else 0,
        'indicators_updated': indicator_info['indicators_updated'] if indicator_info else None,
        'is_stale': _is_data_stale(sync_info['last_refresh'] if sync_info else None)
    })


def _is_data_stale(last_refresh) -> bool:
    """Check if data is stale (not refreshed today)"""
    if not last_refresh:
        return True
    try:
        if hasattr(last_refresh, 'strftime'):
            last_date = last_refresh.strftime('%Y-%m-%d')
        else:
            last_date = str(last_refresh)[:10]
        today = datetime.now().strftime('%Y-%m-%d')
        return last_date != today
    except:
        return True


@api_v2.route('/data/stock/<symbol>', methods=['GET'])
def get_stock_from_cache(symbol):
    """
    Get stock data from database cache only (no API calls).
    Used by TradeBill for quick data retrieval.
    """
    db = get_db()

    # Normalize symbol format
    if ':' not in symbol:
        symbol = f'NSE:{symbol}'

    # Get latest OHLCV data
    ohlcv = db.execute('''
        SELECT TOP 1 * FROM stock_historical_data
        WHERE symbol = ?
        ORDER BY date DESC
    ''', (symbol,)).fetchone()

    if not ohlcv:
        return jsonify({'error': f'No cached data for {symbol}. Please load data first.'}), 404

    # Get pre-calculated indicators
    indicators = db.execute('''
        SELECT TOP 1 * FROM stock_indicators_daily
        WHERE symbol = ?
        ORDER BY date DESC
    ''', (symbol,)).fetchone()

    if not indicators:
        return jsonify({'error': f'No indicator data for {symbol}. Please load data first.'}), 404

    # Calculate Keltner Channel extensions (±3 ATR)
    kc_middle = indicators['kc_middle'] or ohlcv['close']
    atr = indicators['atr'] or 0
    kc_upper_3 = round(kc_middle + 3 * atr,
                       2) if atr else ohlcv['close'] * 1.03
    kc_lower_3 = round(kc_middle - 3 * atr,
                       2) if atr else ohlcv['close'] * 0.97

    # Suggested trade levels
    entry_price = indicators['ema_22'] or ohlcv['close']
    stop_loss = round(kc_lower_3 - atr, 2) if atr else ohlcv['close'] * 0.95

    return jsonify({
        'symbol': symbol,
        'price': ohlcv['close'],
        'date': ohlcv['date'],
        # Pre-calculated indicators from cache
        'ema_22': round(indicators['ema_22'], 2) if indicators['ema_22'] else ohlcv['close'],
        'ema_50': round(indicators['ema_50'], 2) if indicators['ema_50'] else None,
        'atr': round(atr, 2),
        'rsi': round(indicators['rsi'], 1) if indicators['rsi'] else 50,
        'stochastic': round(indicators['stochastic'], 1) if indicators['stochastic'] else 50,
        'macd_histogram': round(indicators['macd_histogram'], 4) if indicators['macd_histogram'] else 0,
        'force_index': round(indicators['force_index'], 0) if indicators['force_index'] else 0,
        # Keltner Channels
        'kc_upper': round(indicators['kc_upper'], 2) if indicators['kc_upper'] else None,
        'kc_middle': round(kc_middle, 2),
        'kc_lower': round(indicators['kc_lower'], 2) if indicators['kc_lower'] else None,
        'kc_upper_3': kc_upper_3,
        'kc_lower_3': kc_lower_3,
        'channel_height': round(kc_upper_3 - kc_lower_3, 2),
        # Trade suggestions
        'suggested_entry': round(entry_price, 2),
        'suggested_stop': stop_loss,
        'suggested_target': kc_upper_3
    })


# ══════════════════════════════════════════════════════════════════════════════
# ENHANCED SCREENER ENDPOINTS (v2 with validation fixes)
# ══════════════════════════════════════════════════════════════════════════════

@api_v2.route('/screener/run', methods=['POST'])
def run_screener_v2():
    """
    Run enhanced screener v2 with all validation fixes

    Features:
    - Screen 1 as mandatory gate
    - Impulse RED blocks trades
    - Correct daily_ready logic
    - New high-scoring rules
    - Elder Entry/Stop/Target calculations
    """
    data = request.get_json() or {}
    market = data.get('market', 'IN')
    symbols = data.get('symbols')

    results = run_weekly_screen_v2(market, symbols)

    # Save to database
    db = get_db()
    user_id = get_user_id()
    today = datetime.now().date()

    row = db.execute('''
        INSERT INTO weekly_scans
        (user_id, market, scan_date, week_start, week_end, results, summary)
        OUTPUT INSERTED.id
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (
        user_id, market, today,
        today - timedelta(days=today.weekday()),
        today + timedelta(days=6-today.weekday()),
        json.dumps(results['all_results']),
        json.dumps(results['summary'])
    )).fetchone()
    db.commit()

    scan_id = int(row[0])
    results['scan_id'] = scan_id

    return jsonify(results)


@api_v2.route('/screener/stock/<symbol>', methods=['GET'])
def analyze_stock_v2(symbol):
    """Analyze a single stock with v2 logic"""
    result = scan_stock_v2(symbol)
    if result:
        return jsonify(result)
    return jsonify({'error': f'Could not analyze {symbol}'}), 404


@api_v2.route('/stock/quote/<symbol>', methods=['GET'])
def get_stock_quote(symbol):
    """
    Get stock quote with KCB(20,10,1) channel data for Trade Bill

    Returns:
    - Current market price (LTP)
    - Keltner Channel Upper (+3 ATR)
    - Keltner Channel Lower (-3 ATR)
    - ATR value
    - Entry suggestion (EMA-22)
    - Stop Loss suggestion (below KC Lower)
    """
    result = scan_stock_v2(symbol)
    if not result:
        return jsonify({'error': f'Could not fetch data for {symbol}'}), 404

    # Get KCB(20,10,1) values with ±3 ATR extensions
    price = result.get('price', 0)
    kc_middle = result.get('kc_middle', price)
    atr = result.get('atr', 0)

    # Calculate extended Keltner channels (±3 ATR)
    kc_upper_3 = round(kc_middle + 3 * atr, 2) if atr else price * 1.03
    kc_lower_3 = round(kc_middle - 3 * atr, 2) if atr else price * 0.97

    # Entry at EMA-22, Stop below lower channel
    entry_price = result.get('ema_22', price)
    stop_loss = round(kc_lower_3 - atr, 2) if atr else price * 0.95

    return jsonify({
        'symbol': symbol,
        'name': result.get('name', symbol),
        'price': price,
        'change': result.get('change', 0),
        'change_percent': result.get('change_percent', 0),
        # Keltner Channel (KC 20,10,1)
        'kc_upper': result.get('kc_upper', price * 1.02),
        'kc_lower': result.get('kc_lower', price * 0.98),
        'kc_middle': kc_middle,
        # Extended channels (±3 ATR)
        'kc_upper_3': kc_upper_3,
        'kc_lower_3': kc_lower_3,
        'channel_height': round(kc_upper_3 - kc_lower_3, 2),
        # ATR and EMAs
        'atr': round(atr, 2) if atr else 0,
        'ema_22': round(entry_price, 2),
        'ema_13': result.get('ema_13', price),
        # Suggestions for Trade Bill
        'suggested_entry': round(entry_price, 2),
        'suggested_stop': stop_loss,
        'suggested_target': kc_upper_3,
        # Additional indicators for reference
        'rsi': result.get('rsi', 50),
        'stochastic': result.get('stochastic', 50),
        'impulse_color': result.get('impulse_color', 'BLUE'),
        'grade': result.get('grade', 'C')
    })


# ══════════════════════════════════════════════════════════════════════════════
# KITE CONNECT API - Authentication & Status
# ══════════════════════════════════════════════════════════════════════════════

@api_v2.route('/kite/status', methods=['GET'])
def kite_status():
    """Check Kite Connect connection status"""
    connected, message = check_kite_connection()
    market_status = get_market_status()

    return jsonify({
        'connected': connected,
        'message': message,
        'market': market_status,
        'broker': 'Zerodha'
    })


@api_v2.route('/kite/login-url', methods=['GET'])
def get_kite_login_url():
    """Get Kite login URL for authentication"""
    db = get_db()
    user_id = get_user_id()

    # Get API key from settings
    settings = db.execute('''
        SELECT kite_api_key FROM account_settings WHERE user_id = ?
    ''', (user_id,)).fetchone()

    if not settings or not settings['kite_api_key']:
        return jsonify({
            'success': False,
            'error': 'API Key not configured. Please add your Kite API Key in Settings.'
        }), 400

    client = init_client(settings['kite_api_key'], None)
    login_url = client.get_login_url()

    return jsonify({
        'success': True,
        'login_url': login_url
    })


@api_v2.route('/kite/authenticate', methods=['POST'])
def kite_authenticate():
    """
    Exchange request token for access token

    Body:
    {
        "request_token": "xxx"
    }
    """
    data = request.get_json()
    request_token = data.get('request_token')

    if not request_token:
        return jsonify({'success': False, 'error': 'Request token required'}), 400

    db = get_db()
    user_id = get_user_id()

    # Get API credentials from settings
    settings = db.execute('''
        SELECT kite_api_key, kite_api_secret FROM account_settings WHERE user_id = ?
    ''', (user_id,)).fetchone()

    if not settings or not settings['kite_api_key'] or not settings['kite_api_secret']:
        return jsonify({
            'success': False,
            'error': 'API Key and Secret not configured. Please add them in Settings.'
        }), 400

    client = init_client(settings['kite_api_key'], settings['kite_api_secret'])
    result = client.generate_session(request_token)

    if result['success']:
        # Save access token to database
        db.execute('''
            UPDATE account_settings
            SET kite_access_token = ?, kite_token_expiry = ?, updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ?
        ''', (result['access_token'], datetime.now().strftime('%Y-%m-%d'), user_id))
        db.commit()

        return jsonify({
            'success': True,
            'message': f"Logged in as {result.get('user_name', 'User')}",
            'user_id': result.get('user_id'),
            'user_name': result.get('user_name'),
            'email': result.get('email')
        })

    return jsonify(result), 400


@api_v2.route('/kite/account', methods=['GET'])
def get_kite_account():
    """Get Kite account summary"""
    result = get_account_info()
    return jsonify(result)


# ══════════════════════════════════════════════════════════════════════════════
# KITE CONNECT API - Regular Orders
# ══════════════════════════════════════════════════════════════════════════════

@api_v2.route('/kite/orders', methods=['GET'])
def get_orders():
    """Get all open/pending orders"""
    result = get_open_orders()
    return jsonify(result)


@api_v2.route('/kite/orders', methods=['POST'])
def create_order():
    """
    Place a new order

    Body:
    {
        "symbol": "RELIANCE",
        "transaction_type": "BUY",
        "quantity": 10,
        "price": 2500.00,
        "order_type": "LIMIT",  // LIMIT, MARKET, SL, SL-M
        "product": "CNC"  // CNC (delivery), MIS (intraday)
    }
    """
    data = request.get_json()

    # Ensure whole shares (no fractional)
    quantity = int(data.get('quantity', 0))
    if quantity <= 0:
        return jsonify({'success': False, 'error': 'Quantity must be a positive integer'}), 400

    result = place_order(
        symbol=data['symbol'],
        transaction_type=data.get('transaction_type', TRANSACTION_BUY),
        quantity=quantity,
        price=data.get('price'),
        order_type=data.get('order_type', ORDER_TYPE_LIMIT),
        product=data.get('product', PRODUCT_CNC),
        trigger_price=data.get('trigger_price')
    )

    if result['success']:
        return jsonify(result), 201
    return jsonify(result), 400


@api_v2.route('/kite/orders/<order_id>', methods=['DELETE'])
def cancel_order_endpoint(order_id):
    """Cancel an order"""
    result = cancel_order(order_id)
    if result['success']:
        return jsonify(result)
    return jsonify(result), 400


@api_v2.route('/kite/orders/<order_id>', methods=['PUT'])
def modify_order_endpoint(order_id):
    """Modify an existing order"""
    data = request.get_json()
    result = modify_order(
        order_id,
        quantity=data.get('quantity'),
        price=data.get('price'),
        trigger_price=data.get('trigger_price'),
        order_type=data.get('order_type')
    )
    if result['success']:
        return jsonify(result)
    return jsonify(result), 400


# ══════════════════════════════════════════════════════════════════════════════
# KITE CONNECT API - GTT Orders (Good Till Triggered)
# ══════════════════════════════════════════════════════════════════════════════

@api_v2.route('/kite/gtt', methods=['GET'])
def get_all_gtt():
    """Get all GTT orders"""
    result = get_gtt_orders()
    return jsonify(result)


@api_v2.route('/kite/gtt', methods=['POST'])
def create_gtt():
    """
    Place a GTT single-trigger order

    Body:
    {
        "symbol": "RELIANCE",
        "transaction_type": "BUY",
        "quantity": 10,
        "trigger_price": 2400.00,
        "limit_price": 2410.00
    }
    """
    data = request.get_json()

    # Enforce trade rules for BUY (entry) orders only
    if data.get('transaction_type', TRANSACTION_BUY) == TRANSACTION_BUY:
        db = get_db()
        user_id = get_user_id()
        rule_check = _check_trade_rules(db, user_id)
        if not rule_check['allowed']:
            return jsonify({'success': False, 'error': rule_check['reason']}), 400

    quantity = int(data.get('quantity', 0))
    if quantity <= 0:
        return jsonify({'success': False, 'error': 'Quantity must be a positive integer'}), 400

    # Accept both 'limit_price' and 'price' from frontend
    limit_price = data.get('limit_price') or data.get('price')
    if not limit_price:
        return jsonify({'success': False, 'error': 'limit_price or price is required'}), 400

    result = place_gtt_order(
        symbol=data['symbol'],
        transaction_type=data.get('transaction_type', TRANSACTION_BUY),
        quantity=quantity,
        trigger_price=data['trigger_price'],
        limit_price=limit_price
    )

    if result['success']:
        return jsonify(result), 201
    return jsonify(result), 400


@api_v2.route('/kite/gtt/oco', methods=['POST'])
def create_gtt_oco():
    """
    Place a GTT-OCO order (Stop Loss + Target)

    This is the primary bracket strategy for NSE.
    Use after buying to set both stop loss and target.

    Body:
    {
        "symbol": "RELIANCE",
        "quantity": 10,
        "stop_loss_trigger": 2400.00,
        "stop_loss_price": 2390.00,
        "target_trigger": 2700.00,
        "target_price": 2690.00
    }
    """
    data = request.get_json()

    quantity = int(data.get('quantity', 0))
    if quantity <= 0:
        return jsonify({'success': False, 'error': 'Quantity must be a positive integer'}), 400

    result = place_gtt_oco(
        symbol=data['symbol'],
        quantity=quantity,
        stop_loss_trigger=data['stop_loss_trigger'],
        stop_loss_price=data['stop_loss_price'],
        target_trigger=data['target_trigger'],
        target_price=data['target_price']
    )

    if result['success']:
        return jsonify(result), 201
    return jsonify(result), 400


@api_v2.route('/kite/gtt/<int:trigger_id>', methods=['DELETE'])
def delete_gtt(trigger_id):
    """Cancel a GTT order"""
    result = cancel_gtt(trigger_id)
    if result['success']:
        return jsonify(result)
    return jsonify(result), 400


@api_v2.route('/kite/gtt/oco-split', methods=['POST'])
def create_gtt_oco_split():
    """
    Place OCO-style exits as two separate single-leg GTT orders:
    1. Full quantity at Stop Loss
    2. Half quantity at Target

    This workaround is needed because Kite OCO requires same qty on both legs.

    Body:
    {
        "symbol": "RELIANCE",
        "quantity": 10,
        "stop_loss_trigger": 2400.00,
        "stop_loss_price": 2390.00,
        "target_trigger": 2700.00,
        "target_price": 2690.00
    }
    """
    data = request.get_json()
    quantity = int(data.get('quantity', 0))
    if quantity <= 0:
        return jsonify({'success': False, 'error': 'Quantity must be positive'}), 400

    half_qty = quantity // 2
    if half_qty <= 0:
        return jsonify({'success': False, 'error': 'Quantity must be at least 2 for OCO split'}), 400

    symbol = data['symbol']
    results = {'sl_gtt': None, 'target_gtt': None}

    # 1. GTT for Stop Loss - FULL quantity
    sl_result = place_gtt_order(
        symbol=symbol,
        transaction_type=TRANSACTION_SELL,
        quantity=quantity,
        trigger_price=data['stop_loss_trigger'],
        limit_price=data['stop_loss_price']
    )
    results['sl_gtt'] = sl_result

    # 2. GTT for Target - HALF quantity
    target_result = place_gtt_order(
        symbol=symbol,
        transaction_type=TRANSACTION_SELL,
        quantity=half_qty,
        trigger_price=data['target_trigger'],
        limit_price=data['target_price']
    )
    results['target_gtt'] = target_result

    success = sl_result.get('success', False) or target_result.get('success', False)
    return jsonify({
        'success': success,
        'sl_gtt': sl_result,
        'target_gtt': target_result,
        'sl_quantity': quantity,
        'target_quantity': half_qty,
        'message': f'SL GTT: {quantity} shares, Target GTT: {half_qty} shares'
    }), 201 if success else 400


@api_v2.route('/kite/gtt/<int:trigger_id>/modify', methods=['PUT'])
def modify_gtt(trigger_id):
    """
    Modify a GTT order by cancelling and re-creating.
    Kite API doesn't support direct GTT modification.

    Body:
    {
        "symbol": "RELIANCE",
        "transaction_type": "SELL",
        "quantity": 10,
        "trigger_price": 2400.00,
        "limit_price": 2390.00
    }
    """
    data = request.get_json()

    # 1. Cancel existing GTT
    cancel_result = cancel_gtt(trigger_id)
    if not cancel_result.get('success', False):
        return jsonify({
            'success': False,
            'error': f'Failed to cancel GTT {trigger_id}: {cancel_result.get("error", "Unknown error")}'
        }), 400

    # 2. Create new GTT with updated values
    new_result = place_gtt_order(
        symbol=data['symbol'],
        transaction_type=data.get('transaction_type', TRANSACTION_SELL),
        quantity=int(data['quantity']),
        trigger_price=data['trigger_price'],
        limit_price=data['limit_price']
    )

    if new_result.get('success'):
        return jsonify({
            'success': True,
            'old_trigger_id': trigger_id,
            'new_trigger_id': new_result.get('trigger_id'),
            'message': f'GTT modified: cancelled #{trigger_id}, created new'
        })

    return jsonify({
        'success': False,
        'error': f'Cancelled old GTT but failed to create new: {new_result.get("error", "Unknown")}',
        'cancelled_id': trigger_id
    }), 400


@api_v2.route('/kite/orders/nrml', methods=['POST'])
def place_nrml_order():
    """
    Place a NRML (Normal) delivery order.

    Body:
    {
        "symbol": "RELIANCE",
        "transaction_type": "BUY",
        "quantity": 10,
        "price": 2500.00,
        "order_type": "LIMIT"
    }
    """
    data = request.get_json()

    # Enforce trade rules for BUY (entry) orders only
    if data.get('transaction_type', TRANSACTION_BUY) == TRANSACTION_BUY:
        db = get_db()
        user_id = get_user_id()
        rule_check = _check_trade_rules(db, user_id)
        if not rule_check['allowed']:
            return jsonify({'success': False, 'error': rule_check['reason']}), 400

    quantity = int(data.get('quantity', 0))
    if quantity <= 0:
        return jsonify({'success': False, 'error': 'Quantity must be positive'}), 400

    # Use CNC for NSE equity delivery (NRML is for F&O derivatives)
    product = data.get('product', PRODUCT_CNC)
    result = place_order(
        symbol=data['symbol'],
        transaction_type=data.get('transaction_type', TRANSACTION_BUY),
        quantity=quantity,
        price=data.get('price'),
        order_type=data.get('order_type', ORDER_TYPE_LIMIT),
        product=product,
        trigger_price=data.get('trigger_price')
    )

    if result['success']:
        return jsonify(result), 201
    return jsonify(result), 400


# ══════════════════════════════════════════════════════════════════════════════
# NSE TRADE CHARGES CALCULATOR
# ══════════════════════════════════════════════════════════════════════════════

@api_v2.route('/charges/estimate', methods=['POST'])
def estimate_charges():
    """
    Estimate NSE trade charges

    Body:
    {
        "entry_price": 2500.00,
        "stop_loss": 2400.00,
        "target": 2700.00,
        "quantity": 10,
        "is_intraday": false
    }
    """
    data = request.get_json()

    quantity = int(data.get('quantity', 0))
    if quantity <= 0:
        return jsonify({'success': False, 'error': 'Quantity must be a positive integer'}), 400

    result = estimate_trade_charges(
        entry_price=data['entry_price'],
        stop_loss=data['stop_loss'],
        target=data['target'],
        quantity=quantity,
        is_intraday=data.get('is_intraday', False)
    )

    return jsonify(result)


@api_v2.route('/charges/break-even', methods=['POST'])
def get_break_even():
    """
    Calculate break-even price after charges

    Body:
    {
        "entry_price": 2500.00,
        "quantity": 10,
        "is_intraday": false
    }
    """
    data = request.get_json()

    quantity = int(data.get('quantity', 0))
    if quantity <= 0:
        return jsonify({'success': False, 'error': 'Quantity must be a positive integer'}), 400

    break_even = calculate_break_even(
        entry_price=data['entry_price'],
        quantity=quantity,
        is_intraday=data.get('is_intraday', False)
    )

    return jsonify({
        'entry_price': data['entry_price'],
        'break_even': break_even,
        'difference': round(break_even - data['entry_price'], 2),
        'difference_percent': round((break_even - data['entry_price']) / data['entry_price'] * 100, 3)
    })


# ══════════════════════════════════════════════════════════════════════════════
# POSITION MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

@api_v2.route('/positions', methods=['GET'])
def get_all_positions():
    """
    Get all open positions with current P/L
    Returns positions from Kite Connect with real-time market prices
    """
    result = get_positions()

    if result['success']:
        # Add alerts
        db = get_db()
        user_id = get_user_id()

        # Get trade bills for matching
        bills = db.execute('''
            SELECT * FROM trade_bills WHERE user_id = ? AND status = 'PENDING'
        ''', (user_id,)).fetchall()
        trade_bills = [dict(b) for b in bills]

        alerts = get_position_alerts(result['positions'], trade_bills)
        result['alerts'] = alerts
        result['alert_count'] = len(alerts)
        result['high_priority_alerts'] = len(
            [a for a in alerts if a.get('severity') == 'HIGH'])

    return jsonify(result)


@api_v2.route('/positions/summary', methods=['GET'])
def get_position_summary():
    """Get position summary with totals"""
    result = get_positions()

    if result['success']:
        positions = result['positions']

        return jsonify({
            'success': True,
            'total_positions': len(positions),
            'total_market_value': sum(p['market_value'] for p in positions),
            'total_unrealized_pnl': sum(p['unrealized_pnl'] for p in positions),
            'total_realized_pnl': sum(p['realized_pnl'] for p in positions),
            'winning_positions': len([p for p in positions if p['unrealized_pnl'] > 0]),
            'losing_positions': len([p for p in positions if p['unrealized_pnl'] < 0]),
            'positions': positions
        })

    return jsonify(result)


@api_v2.route('/positions/<symbol>/close', methods=['POST'])
def close_position(symbol):
    """
    Close a position (market sell)

    Body (optional):
    {
        "quantity": 50  // Partial close (whole shares only)
    }
    """
    data = request.get_json() or {}

    # Get current position
    positions = get_positions()
    if not positions['success']:
        return jsonify(positions), 400

    position = None
    for p in positions['positions']:
        if p['symbol'] == symbol:
            position = p
            break

    if not position:
        return jsonify({'success': False, 'error': f'No position found for {symbol}'}), 404

    quantity = int(data.get('quantity', position['quantity']))

    # Place market sell order via Kite
    result = place_order(
        symbol=symbol,
        transaction_type=TRANSACTION_SELL,
        quantity=quantity,
        order_type=ORDER_TYPE_MARKET,
        product=PRODUCT_CNC
    )

    if result['success']:
        result['message'] = f'Closing {quantity} shares of {symbol}'

    return jsonify(result)


@api_v2.route('/holdings', methods=['GET'])
def get_all_holdings():
    """Get all holdings (delivery positions) from Kite"""
    result = get_holdings()
    return jsonify(result)


# ══════════════════════════════════════════════════════════════════════════════
# CONNECTED WORKFLOW: Trade Bill → Kite Order
# ══════════════════════════════════════════════════════════════════════════════

@api_v2.route('/trade-bills/<int:bill_id>/place-order', methods=['POST'])
def place_order_from_bill(bill_id):
    """
    Place Kite order directly from Trade Bill

    This is the key connection in the workflow:
    Screener → Trade Bill → Kite Order → GTT-OCO for SL/Target

    The Trade Bill contains:
    - Entry price (EMA-22)
    - Stop loss (deepest penetration)
    - Target (KC upper)
    - Quantity (calculated from risk, whole shares only)
    """
    db = get_db()
    user_id = get_user_id()

    # Enforce trade rules (always a BUY from trade bill)
    rule_check = _check_trade_rules(db, user_id)
    if not rule_check['allowed']:
        return jsonify({'success': False, 'error': rule_check['reason']}), 400

    # Get trade bill
    bill = db.execute('''
        SELECT * FROM trade_bills WHERE id = ? AND user_id = ?
    ''', (bill_id, user_id)).fetchone()

    if not bill:
        return jsonify({'success': False, 'error': 'Trade Bill not found'}), 404

    bill_data = dict(bill)

    # Place the order
    result = create_trade_from_bill({
        'id': bill_id,
        'symbol': bill_data['symbol'],
        'entry': bill_data['entry_price'],
        'stop_loss': bill_data['stop_loss'],
        'target': bill_data['target_price'],
        'quantity': bill_data['quantity']
    })

    if result['success']:
        # Update trade bill status
        db.execute('''
            UPDATE trade_bills 
            SET status = 'ORDERED', order_id = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (result.get('order_id'), bill_id))
        db.commit()

        result['trade_bill_updated'] = True

    return jsonify(result)


@api_v2.route('/trade-bills/from-screener', methods=['POST'])
def create_bill_from_screener():
    """
    Create Trade Bill directly from screener result

    Body:
    {
        "symbol": "NSE:RELIANCE",
        "screener_data": { ... }  // Optional, will fetch if not provided
    }
    """
    data = request.get_json()
    symbol = data.get('symbol')

    if not symbol:
        return jsonify({'success': False, 'error': 'Symbol required'}), 400

    # Get fresh analysis if not provided
    screener_data = data.get('screener_data')
    if not screener_data:
        screener_data = scan_stock_v2(symbol)
        if not screener_data:
            return jsonify({'success': False, 'error': f'Could not analyze {symbol}'}), 400

    # Get account settings for position sizing
    db = get_db()
    user_id = get_user_id()

    account = db.execute('''
        SELECT * FROM account_settings WHERE user_id = ?
    ''', (user_id,)).fetchone()

    if not account:
        return jsonify({'success': False, 'error': 'Account settings not found'}), 400

    account = dict(account)
    risk_per_trade = account['trading_capital'] * \
        (account['risk_per_trade'] / 100)

    # Calculate position size
    entry = screener_data['entry']
    stop = screener_data['stop_loss']
    risk_per_share = entry - stop

    if risk_per_share <= 0:
        return jsonify({'success': False, 'error': 'Invalid stop loss (above entry)'}), 400

    quantity = int(risk_per_trade / risk_per_share)
    position_value = quantity * entry

    # Create trade bill
    bill_row = db.execute('''
        INSERT INTO trade_bills (
            user_id, symbol, market, direction, entry_price, stop_loss,
            target_price, quantity, position_value, risk_amount,
            risk_reward_ratio, signal_strength, grade, notes, status
        )
        OUTPUT INSERTED.id
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        user_id, symbol, 'US', 'LONG',
        entry, stop, screener_data['target'],
        quantity, position_value, risk_per_trade,
        screener_data['risk_reward_ratio'],
        screener_data['signal_strength'],
        screener_data['grade'],
        f"Created from screener. High-value signals: {', '.join(screener_data.get('high_value_signals', []))}",
        'PENDING'
    )).fetchone()
    db.commit()

    bill_id = int(bill_row[0])

    return jsonify({
        'success': True,
        'trade_bill_id': bill_id,
        'symbol': symbol,
        'entry': entry,
        'stop_loss': stop,
        'target': screener_data['target'],
        'quantity': quantity,
        'position_value': position_value,
        'risk_amount': risk_per_trade,
        'risk_reward': screener_data['rr_display'],
        'grade': screener_data['grade'],
        'signal_strength': screener_data['signal_strength']
    }), 201


# ══════════════════════════════════════════════════════════════════════════════
# TRADE JOURNAL V2 (Enhanced with Multiple Entries/Exits)
# ══════════════════════════════════════════════════════════════════════════════

@api_v2.route('/trade-journal', methods=['GET'])
def get_trade_journals():
    """Get all trade journals for the user"""
    db = get_db()
    user_id = get_user_id()

    journals = db.execute('''
        SELECT * FROM trade_journal_v2
        WHERE user_id = ?
        ORDER BY journal_date DESC, created_at DESC
    ''', (user_id,)).fetchall()

    result = []
    for j in journals:
        journal = dict(j)
        # Ensure journal_date is ISO format string (YYYY-MM-DD) for HTML date inputs
        if journal.get('journal_date') and hasattr(journal['journal_date'], 'isoformat'):
            journal['journal_date'] = journal['journal_date'].isoformat()
        # Get entries
        entries = db.execute('''
            SELECT * FROM trade_entries WHERE journal_id = ? ORDER BY entry_datetime
        ''', (j['id'],)).fetchall()
        journal['entries'] = [dict(e) for e in entries]

        # Get exits
        exits = db.execute('''
            SELECT * FROM trade_exits WHERE journal_id = ? ORDER BY exit_datetime
        ''', (j['id'],)).fetchall()
        journal['exits'] = [dict(e) for e in exits]

        result.append(journal)

    return jsonify({'journals': result})


@api_v2.route('/trade-journal/<int:journal_id>', methods=['GET'])
def get_trade_journal(journal_id):
    """Get a single trade journal with entries and exits"""
    db = get_db()
    user_id = get_user_id()

    journal = db.execute('''
        SELECT * FROM trade_journal_v2 WHERE id = ? AND user_id = ?
    ''', (journal_id, user_id)).fetchone()

    if not journal:
        return jsonify({'error': 'Journal not found'}), 404

    result = dict(journal)

    # Ensure journal_date is ISO format string (YYYY-MM-DD) for HTML date inputs
    if result.get('journal_date') and hasattr(result['journal_date'], 'isoformat'):
        result['journal_date'] = result['journal_date'].isoformat()

    # Get entries
    entries = db.execute('''
        SELECT * FROM trade_entries WHERE journal_id = ? ORDER BY entry_datetime
    ''', (journal_id,)).fetchall()
    result['entries'] = [dict(e) for e in entries]

    # Get exits
    exits = db.execute('''
        SELECT * FROM trade_exits WHERE journal_id = ? ORDER BY exit_datetime
    ''', (journal_id,)).fetchall()
    result['exits'] = [dict(e) for e in exits]

    # Compute notional P&L for remaining position (unrealized, not stored)
    remaining = result.get('remaining_qty') or 0
    avg_entry = result.get('avg_entry') or 0
    direction = result.get('direction', 'Long')
    exited_qty = (result.get('total_shares') or 0) - remaining
    result['exited_qty'] = exited_qty

    return jsonify(result)


@api_v2.route('/trade-journal', methods=['POST'])
def create_trade_journal():
    """Create a new trade journal entry"""
    db = get_db()
    user_id = get_user_id()
    data = request.get_json()

    # initial_stop_loss = first entry's stop loss (static, never changes)
    initial_sl = data.get('initial_stop_loss') or data.get('stop_loss')

    row = db.execute('''
        INSERT INTO trade_journal_v2 (
            user_id, trade_bill_id, ticker, cmp, direction, status, journal_date,
            remaining_qty, order_type, mental_state, entry_price, quantity,
            target_price, stop_loss, initial_stop_loss, rr_ratio, potential_loss,
            trailing_stop, new_target, potential_gain, target_a, target_b, target_c,
            entry_tactic, entry_reason, exit_tactic, exit_reason,
            open_trade_comments, followup_analysis, strategy, mistake
        )
        OUTPUT INSERTED.id
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        user_id,
        data.get('trade_bill_id'),
        data.get('ticker'),
        data.get('cmp'),
        data.get('direction', 'Long'),
        data.get('status', 'open'),
        data.get('journal_date', datetime.now().strftime('%Y-%m-%d')),
        data.get('remaining_qty', 0),
        data.get('order_type'),
        data.get('mental_state'),
        data.get('entry_price'),
        data.get('quantity'),
        data.get('target_price'),
        data.get('stop_loss'),
        initial_sl,
        data.get('rr_ratio'),
        data.get('potential_loss'),
        data.get('trailing_stop'),
        data.get('new_target'),
        data.get('potential_gain'),
        data.get('target_a'),
        data.get('target_b'),
        data.get('target_c'),
        data.get('entry_tactic'),
        data.get('entry_reason'),
        data.get('exit_tactic'),
        data.get('exit_reason'),
        data.get('open_trade_comments'),
        data.get('followup_analysis'),
        data.get('strategy'),
        data.get('mistake')
    )).fetchone()
    db.commit()

    journal_id = int(row[0])
    return jsonify({'success': True, 'id': journal_id}), 201


@api_v2.route('/trade-journal/<int:journal_id>', methods=['PUT'])
def update_trade_journal(journal_id):
    """Update a trade journal entry"""
    db = get_db()
    user_id = get_user_id()
    data = request.get_json()

    # Verify ownership
    journal = db.execute('''
        SELECT id FROM trade_journal_v2 WHERE id = ? AND user_id = ?
    ''', (journal_id, user_id)).fetchone()

    if not journal:
        return jsonify({'error': 'Journal not found'}), 404

    # Only update user-editable fields. NEVER overwrite calculated fields
    # (remaining_qty, total_shares, avg_entry, avg_exit, first_entry_date,
    #  last_exit_date, high_during_trade, low_during_trade, gain_loss_amount,
    #  gain_loss_percent, status) — those are set by _recalculate_journal_totals()
    db.execute('''
        UPDATE trade_journal_v2 SET
            ticker = ?, cmp = ?, direction = ?, journal_date = ?,
            order_type = ?, mental_state = ?,
            entry_price = ?, quantity = ?, target_price = ?, stop_loss = ?,
            initial_stop_loss = COALESCE(initial_stop_loss, ?),
            rr_ratio = ?, potential_loss = ?, trailing_stop = ?, new_target = ?,
            potential_gain = ?, target_a = ?, target_b = ?, target_c = ?,
            entry_tactic = ?, entry_reason = ?, exit_tactic = ?, exit_reason = ?,
            trade_grade = ?,
            max_drawdown = ?, percent_captured = ?,
            open_trade_comments = ?, followup_analysis = ?,
            strategy = ?, mistake = ?,
            tv_link_entry = ?, tv_link_exit = ?, tv_link_result = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ? AND user_id = ?
    ''', (
        data.get('ticker'),
        data.get('cmp'),
        data.get('direction'),
        data.get('journal_date'),
        data.get('order_type'),
        data.get('mental_state'),
        data.get('entry_price'),
        data.get('quantity'),
        data.get('target_price'),
        data.get('stop_loss'),
        data.get('initial_stop_loss') or data.get('stop_loss'),
        data.get('rr_ratio'),
        data.get('potential_loss'),
        data.get('trailing_stop'),
        data.get('new_target'),
        data.get('potential_gain'),
        data.get('target_a'),
        data.get('target_b'),
        data.get('target_c'),
        data.get('entry_tactic'),
        data.get('entry_reason'),
        data.get('exit_tactic'),
        data.get('exit_reason'),
        data.get('trade_grade'),
        data.get('max_drawdown'),
        data.get('percent_captured'),
        data.get('open_trade_comments'),
        data.get('followup_analysis'),
        data.get('strategy'),
        data.get('mistake'),
        data.get('tv_link_entry'),
        data.get('tv_link_exit'),
        data.get('tv_link_result'),
        journal_id,
        user_id
    ))
    db.commit()

    # Recalculate totals (direction may have changed, affecting P&L sign)
    try:
        _recalculate_journal_totals(db, journal_id)
    except Exception as e:
        logger.error(f"Recalculation failed after updating journal {journal_id}: {e}")

    return jsonify({'success': True})


@api_v2.route('/trade-journal/<int:journal_id>', methods=['DELETE'])
def delete_trade_journal(journal_id):
    """Delete a trade journal and its entries/exits"""
    db = get_db()
    user_id = get_user_id()

    # Verify ownership
    journal = db.execute('''
        SELECT id FROM trade_journal_v2 WHERE id = ? AND user_id = ?
    ''', (journal_id, user_id)).fetchone()

    if not journal:
        return jsonify({'error': 'Journal not found'}), 404

    # Delete entries and exits first (cascaded by FK)
    db.execute('DELETE FROM trade_entries WHERE journal_id = ?', (journal_id,))
    db.execute('DELETE FROM trade_exits WHERE journal_id = ?', (journal_id,))
    db.execute('DELETE FROM trade_journal_v2 WHERE id = ?', (journal_id,))
    db.commit()

    return jsonify({'success': True})


@api_v2.route('/trade-journal/<int:journal_id>/entry', methods=['POST'])
def add_trade_entry(journal_id):
    """Add an entry record to a journal"""
    db = get_db()
    user_id = get_user_id()
    data = request.get_json()

    # Verify ownership
    journal = db.execute('''
        SELECT id FROM trade_journal_v2 WHERE id = ? AND user_id = ?
    ''', (journal_id, user_id)).fetchone()

    if not journal:
        return jsonify({'error': 'Journal not found'}), 404

    row = db.execute('''
        INSERT INTO trade_entries (
            journal_id, entry_datetime, quantity, order_price, filled_price,
            slippage, commission, position_size, day_high, day_low, grade, notes
        )
        OUTPUT INSERTED.id
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        journal_id,
        data.get('entry_datetime'),
        data.get('quantity'),
        data.get('order_price'),
        data.get('filled_price'),
        data.get('slippage'),
        data.get('commission'),
        data.get('position_size'),
        data.get('day_high'),
        data.get('day_low'),
        data.get('grade'),
        data.get('notes')
    )).fetchone()
    db.commit()

    entry_id = int(row[0])

    # Update journal totals
    try:
        metrics = _recalculate_journal_totals(db, journal_id)
    except Exception as e:
        logger.error(f"Recalculation failed after adding entry {entry_id}: {e}")
        metrics = {}

    return jsonify({'success': True, 'id': entry_id, 'metrics': metrics}), 201


@api_v2.route('/trade-journal/<int:journal_id>/exit', methods=['POST'])
def add_trade_exit(journal_id):
    """Add an exit record to a journal"""
    db = get_db()
    user_id = get_user_id()
    data = request.get_json()

    # Verify ownership
    journal = db.execute('''
        SELECT id FROM trade_journal_v2 WHERE id = ? AND user_id = ?
    ''', (journal_id, user_id)).fetchone()

    if not journal:
        return jsonify({'error': 'Journal not found'}), 404

    row = db.execute('''
        INSERT INTO trade_exits (
            journal_id, exit_datetime, quantity, order_price, filled_price,
            slippage, commission, position_size, day_high, day_low, grade, notes
        )
        OUTPUT INSERTED.id
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        journal_id,
        data.get('exit_datetime'),
        data.get('quantity'),
        data.get('order_price'),
        data.get('filled_price'),
        data.get('slippage'),
        data.get('commission'),
        data.get('position_size'),
        data.get('day_high'),
        data.get('day_low'),
        data.get('grade'),
        data.get('notes')
    )).fetchone()
    db.commit()

    exit_id = int(row[0])

    # Update journal totals
    try:
        metrics = _recalculate_journal_totals(db, journal_id)
    except Exception as e:
        logger.error(f"Recalculation failed after adding exit {exit_id}: {e}")
        metrics = {}

    return jsonify({'success': True, 'id': exit_id, 'metrics': metrics}), 201


@api_v2.route('/trade-journal/entry/<int:entry_id>', methods=['DELETE'])
def delete_trade_entry(entry_id):
    """Delete an entry record"""
    db = get_db()
    user_id = get_user_id()

    # Get journal_id and verify ownership
    entry = db.execute('''
        SELECT e.journal_id FROM trade_entries e
        JOIN trade_journal_v2 j ON e.journal_id = j.id
        WHERE e.id = ? AND j.user_id = ?
    ''', (entry_id, user_id)).fetchone()

    if not entry:
        return jsonify({'error': 'Entry not found'}), 404

    journal_id = entry['journal_id']
    db.execute('DELETE FROM trade_entries WHERE id = ?', (entry_id,))
    db.commit()

    # Recalculate totals
    try:
        _recalculate_journal_totals(db, journal_id)
    except Exception as e:
        logger.error(f"Recalculation failed after deleting entry {entry_id}: {e}")

    return jsonify({'success': True})


@api_v2.route('/trade-journal/exit/<int:exit_id>', methods=['DELETE'])
def delete_trade_exit(exit_id):
    """Delete an exit record"""
    db = get_db()
    user_id = get_user_id()

    # Get journal_id and verify ownership
    exit_rec = db.execute('''
        SELECT e.journal_id FROM trade_exits e
        JOIN trade_journal_v2 j ON e.journal_id = j.id
        WHERE e.id = ? AND j.user_id = ?
    ''', (exit_id, user_id)).fetchone()

    if not exit_rec:
        return jsonify({'error': 'Exit not found'}), 404

    journal_id = exit_rec['journal_id']
    db.execute('DELETE FROM trade_exits WHERE id = ?', (exit_id,))
    db.commit()

    # Recalculate totals
    try:
        _recalculate_journal_totals(db, journal_id)
    except Exception as e:
        logger.error(f"Recalculation failed after deleting exit {exit_id}: {e}")

    return jsonify({'success': True})


@api_v2.route('/trade-journal/<int:journal_id>/recalculate', methods=['POST'])
def recalculate_trade_journal(journal_id):
    """Force recalculation of journal totals from entries/exits"""
    db = get_db()
    user_id = get_user_id()

    # Verify ownership
    journal = db.execute('''
        SELECT id FROM trade_journal_v2 WHERE id = ? AND user_id = ?
    ''', (journal_id, user_id)).fetchone()

    if not journal:
        return jsonify({'error': 'Journal not found'}), 404

    try:
        metrics = _recalculate_journal_totals(db, journal_id)
        return jsonify({'success': True, 'metrics': metrics})
    except Exception as e:
        logger.error(f"Manual recalculation failed for journal {journal_id}: {e}", exc_info=True)
        return jsonify({'error': f'Recalculation failed: {str(e)}'}), 500


@api_v2.route('/trade-journal/<int:journal_id>/trailing-stop', methods=['PUT'])
def update_trailing_stop(journal_id):
    """
    Update trailing stop for a journal entry (pyramiding model).
    The initial_stop_loss never changes; only trailing_stop moves up.
    """
    db = get_db()
    user_id = get_user_id()
    data = request.get_json()

    journal = db.execute('''
        SELECT id, initial_stop_loss, trailing_stop FROM trade_journal_v2
        WHERE id = ? AND user_id = ?
    ''', (journal_id, user_id)).fetchone()

    if not journal:
        return jsonify({'error': 'Journal not found'}), 404

    new_trailing = data.get('trailing_stop')
    if new_trailing is None:
        return jsonify({'error': 'trailing_stop is required'}), 400

    # Trailing stop can only move up (for longs), never below initial_stop_loss
    initial_sl = journal['initial_stop_loss'] or 0
    if new_trailing < initial_sl:
        return jsonify({'error': f'Trailing stop cannot be below initial stop loss ({initial_sl})'}), 400

    db.execute('''
        UPDATE trade_journal_v2
        SET trailing_stop = ?, stop_loss = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
    ''', (new_trailing, new_trailing, journal_id))
    db.commit()

    return jsonify({
        'success': True,
        'initial_stop_loss': initial_sl,
        'trailing_stop': new_trailing
    })


@api_v2.route('/trade-journal/from-bill/<int:bill_id>', methods=['POST'])
def create_journal_from_bill(bill_id):
    """Create a trade journal pre-filled from a trade bill"""
    db = get_db()
    user_id = get_user_id()

    # Get the trade bill
    bill = db.execute('''
        SELECT * FROM trade_bills WHERE id = ? AND user_id = ?
    ''', (bill_id, user_id)).fetchone()

    if not bill:
        return jsonify({'error': 'Trade Bill not found'}), 404

    bill = dict(bill)

    # Calculate potential loss and gain
    entry = bill.get('entry_price') or 0
    sl = bill.get('stop_loss') or 0
    target = bill.get('target_price') or 0
    qty = bill.get('quantity') or 0

    potential_loss = abs(entry - sl) * qty if entry and sl else 0
    potential_gain = abs(target - entry) * qty if entry and target else 0

    # Create journal from bill data
    j_row = db.execute('''
        INSERT INTO trade_journal_v2 (
            user_id, trade_bill_id, ticker, cmp, direction, status, journal_date,
            entry_price, quantity, target_price, stop_loss, rr_ratio,
            potential_loss, potential_gain, target_a, target_b, target_c
        )
        OUTPUT INSERTED.id
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        user_id,
        bill_id,
        bill.get('ticker') or bill.get('symbol', ''),
        bill.get('current_market_price'),
        'Long',  # Default
        'open',
        datetime.now().strftime('%Y-%m-%d'),
        entry,
        qty,
        target,
        sl,
        bill.get('risk_reward_ratio'),
        potential_loss,
        potential_gain,
        bill.get('target_1_3_a'),
        bill.get('target_1_2_b'),
        bill.get('target_1_1_c')
    )).fetchone()
    db.commit()

    journal_id = int(j_row[0])

    # Update trade bill to mark journal entered
    db.execute('''
        UPDATE trade_bills SET journal_entered = 1, updated_at = GETDATE()
        WHERE id = ?
    ''', (bill_id,))
    db.commit()

    return jsonify({'success': True, 'id': journal_id}), 201


def _check_trade_rules(db, user_id):
    """
    Check all trade rules before allowing a new BUY order.
    Returns {'allowed': True} or {'allowed': False, 'reason': '...'}.
    Hard block — no override.
    """
    account = db.execute('''
        SELECT trading_capital, max_open_positions, risk_per_day,
               max_trades_per_day, risk_per_week
        FROM account_settings WHERE user_id = ?
    ''', (user_id,)).fetchone()

    if not account:
        return {'allowed': True}  # No settings configured = no rules

    trading_capital = account['trading_capital'] or 500000
    max_positions = account['max_open_positions'] or 5
    risk_per_day_pct = account.get('risk_per_day') or 2.0
    max_trades_day = account.get('max_trades_per_day') or 3
    risk_per_week_pct = account.get('risk_per_week') or 5.0

    today = datetime.now().strftime('%Y-%m-%d')

    # Rule 1: Open positions < max_open_positions
    open_count = db.execute('''
        SELECT COUNT(*) AS cnt FROM trade_journal_v2
        WHERE user_id = ? AND status = 'open' AND remaining_qty > 0
    ''', (user_id,)).fetchone()
    open_count = open_count['cnt'] if open_count else 0

    if open_count >= max_positions:
        return {
            'allowed': False,
            'reason': f'Maximum concurrent open positions ({max_positions}) reached. Currently have {open_count} open trades.'
        }

    # Rule 2: Trades opened today < max_trades_per_day
    today_trades = db.execute('''
        SELECT COUNT(*) AS cnt FROM trade_journal_v2
        WHERE user_id = ? AND CONVERT(DATE, journal_date) = ?
    ''', (user_id, today)).fetchone()
    today_trades = today_trades['cnt'] if today_trades else 0

    if today_trades >= max_trades_day:
        return {
            'allowed': False,
            'reason': f'Maximum trades per day ({max_trades_day}) reached. Already opened {today_trades} trades today.'
        }

    # Rule 3: Total risk today < risk_per_day % of capital
    max_daily_risk = trading_capital * risk_per_day_pct / 100
    today_risk_row = db.execute('''
        SELECT COALESCE(SUM(
            CASE WHEN direction = 'Short'
                 THEN (COALESCE(stop_loss, 0) - COALESCE(avg_entry, entry_price, 0)) * COALESCE(remaining_qty, 0)
                 ELSE (COALESCE(avg_entry, entry_price, 0) - COALESCE(stop_loss, 0)) * COALESCE(remaining_qty, 0)
            END
        ), 0) AS risk
        FROM trade_journal_v2
        WHERE user_id = ? AND CONVERT(DATE, journal_date) = ?
              AND status = 'open' AND COALESCE(remaining_qty, 0) > 0
              AND COALESCE(stop_loss, 0) > 0
    ''', (user_id, today)).fetchone()
    today_risk = today_risk_row['risk'] if today_risk_row else 0

    if today_risk >= max_daily_risk:
        return {
            'allowed': False,
            'reason': f'Daily risk limit reached. Max: \u20b9{max_daily_risk:,.0f} ({risk_per_day_pct}%), Current: \u20b9{today_risk:,.0f}.'
        }

    # Rule 4: Total risk this week < risk_per_week % of capital
    max_weekly_risk = trading_capital * risk_per_week_pct / 100
    now = datetime.now()
    week_start = (now - timedelta(days=now.weekday())).strftime('%Y-%m-%d')
    week_risk_row = db.execute('''
        SELECT COALESCE(SUM(
            CASE WHEN direction = 'Short'
                 THEN (COALESCE(stop_loss, 0) - COALESCE(avg_entry, entry_price, 0)) * COALESCE(remaining_qty, 0)
                 ELSE (COALESCE(avg_entry, entry_price, 0) - COALESCE(stop_loss, 0)) * COALESCE(remaining_qty, 0)
            END
        ), 0) AS risk
        FROM trade_journal_v2
        WHERE user_id = ? AND CONVERT(DATE, journal_date) >= ?
              AND status = 'open' AND COALESCE(remaining_qty, 0) > 0
              AND COALESCE(stop_loss, 0) > 0
    ''', (user_id, week_start)).fetchone()
    week_risk = week_risk_row['risk'] if week_risk_row else 0

    if week_risk >= max_weekly_risk:
        return {
            'allowed': False,
            'reason': f'Weekly risk limit reached. Max: \u20b9{max_weekly_risk:,.0f} ({risk_per_week_pct}%), Current: \u20b9{week_risk:,.0f}.'
        }

    return {'allowed': True}


def _recalculate_journal_totals(db, journal_id):
    """Recalculate journal aggregate fields from entries/exits.
    Returns a dict of computed metrics for callers to use."""
    try:
        logger.info(f"Recalculating journal {journal_id}...")

        # Get direction from journal for P&L calculation
        journal = db.execute('''
            SELECT direction FROM trade_journal_v2 WHERE id = ?
        ''', (journal_id,)).fetchone()
        direction = journal['direction'] if journal else 'Long'

        # Get all entries
        entries = db.execute('''
            SELECT * FROM trade_entries WHERE journal_id = ? ORDER BY entry_datetime
        ''', (journal_id,)).fetchall()

        # Get all exits
        exits = db.execute('''
            SELECT * FROM trade_exits WHERE journal_id = ? ORDER BY exit_datetime
        ''', (journal_id,)).fetchall()

        logger.debug(f"Journal {journal_id}: {len(entries)} entries, {len(exits)} exits, direction={direction}")

        # Calculate totals
        total_entry_shares = sum(e['quantity'] or 0 for e in entries)
        total_exit_shares = sum(e['quantity'] or 0 for e in exits)
        remaining_qty = total_entry_shares - total_exit_shares

        # Weighted average entry
        total_entry_value = sum(
            (e['filled_price'] or e['order_price'] or 0) * (e['quantity'] or 0) for e in entries)
        avg_entry = total_entry_value / total_entry_shares if total_entry_shares > 0 else 0

        # Weighted average exit
        total_exit_value = sum(
            (e['filled_price'] or e['order_price'] or 0) * (e['quantity'] or 0) for e in exits)
        avg_exit = total_exit_value / total_exit_shares if total_exit_shares > 0 else 0

        # First entry and last exit dates
        first_entry = entries[0]['entry_datetime'] if entries else None
        last_exit = exits[-1]['exit_datetime'] if exits else None

        # High/Low during trade
        all_highs = [e['day_high'] for e in entries if e['day_high']
                     ] + [e['day_high'] for e in exits if e['day_high']]
        all_lows = [e['day_low'] for e in entries if e['day_low']] + \
            [e['day_low'] for e in exits if e['day_low']]
        high_during = max(all_highs) if all_highs else None
        low_during = min(all_lows) if all_lows else None

        # Gain/Loss calculation (realized P&L for exited/closed portion only)
        # Direction-aware: Long = (exit - entry), Short = (entry - exit)
        gain_loss_amount = 0
        if total_exit_shares > 0 and avg_exit > 0 and avg_entry > 0:
            if direction == 'Short':
                gain_loss_amount = (avg_entry - avg_exit) * total_exit_shares
            else:
                gain_loss_amount = (avg_exit - avg_entry) * total_exit_shares
            # Subtract commissions
            total_commission = sum((e['commission'] or 0)
                                   for e in entries) + sum((e['commission'] or 0) for e in exits)
            gain_loss_amount -= total_commission

        gain_loss_percent = (gain_loss_amount / (avg_entry * total_exit_shares)
                             * 100) if (avg_entry and total_exit_shares) else 0

        # Status
        status = 'closed' if remaining_qty == 0 and total_entry_shares > 0 else 'open'

        logger.info(f"Journal {journal_id} computed: total_shares={total_entry_shares}, "
                     f"remaining={remaining_qty}, avg_entry={avg_entry:.2f}, avg_exit={avg_exit:.2f}, "
                     f"gain_loss={gain_loss_amount:.2f}, gain_loss_pct={gain_loss_percent:.2f}, status={status}")

        # Update journal (only calculated fields — never overwrite user-editable fields)
        db.execute('''
            UPDATE trade_journal_v2 SET
                remaining_qty = ?, total_shares = ?, avg_entry = ?, avg_exit = ?,
                first_entry_date = ?, last_exit_date = ?,
                high_during_trade = ?, low_during_trade = ?,
                gain_loss_amount = ?, gain_loss_percent = ?,
                status = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (
            remaining_qty, total_entry_shares, avg_entry, avg_exit,
            first_entry, last_exit, high_during, low_during,
            gain_loss_amount, gain_loss_percent, status, journal_id
        ))
        db.commit()

        logger.info(f"Journal {journal_id} recalculated and committed successfully")

        return {
            'remaining_qty': remaining_qty,
            'total_shares': total_entry_shares,
            'avg_entry': avg_entry,
            'avg_exit': avg_exit,
            'first_entry_date': str(first_entry) if first_entry else None,
            'last_exit_date': str(last_exit) if last_exit else None,
            'high_during_trade': high_during,
            'low_during_trade': low_during,
            'gain_loss_amount': gain_loss_amount,
            'gain_loss_percent': gain_loss_percent,
            'status': status
        }
    except Exception as e:
        logger.error(f"Failed to recalculate journal {journal_id}: {e}", exc_info=True)
        raise


# ══════════════════════════════════════════════════════════════════════════════
# AUTO-SYNC TRADE LOG FROM KITE CONNECT
# ══════════════════════════════════════════════════════════════════════════════

@api_v2.route('/trade-log/sync-kite', methods=['POST'])
def sync_trade_log_from_kite():
    """
    Sync trade log with filled orders from Kite Connect

    This pulls executed trades from Kite and creates/updates trade log entries
    """
    result = get_filled_trades(days_back=7)

    if not result['success']:
        return jsonify(result), 400

    db = get_db()
    user_id = get_user_id()

    synced = 0
    skipped = 0

    for trade in result['trades']:
        # Check if already exists
        existing = db.execute('''
            SELECT id FROM trade_log
            WHERE user_id = ? AND symbol = ? AND entry_date = ?
        ''', (user_id, trade['symbol'], trade['execution_time'])).fetchone()

        if existing:
            skipped += 1
            continue

        # Create trade log entry
        side = 'Long' if trade['transaction_type'] == 'BUY' else 'Short'
        status = 'open' if trade['transaction_type'] == 'BUY' else 'closed'

        db.execute('''
            INSERT INTO trade_log (
                user_id, entry_date, symbol, strategy, direction,
                entry_price, shares, trade_costs, status, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            user_id, trade['execution_time'], trade['symbol'],
            'EL - Elder System', side, trade['price'],
            trade['quantity'], 0, status,
            f"Auto-synced from Kite. Order ID: {trade['order_id']}"
        ))
        synced += 1

    db.commit()

    return jsonify({
        'success': True,
        'synced': synced,
        'skipped': skipped,
        'total_kite_trades': len(result['trades']),
        'message': f'Synced {synced} new trades from Kite ({skipped} already existed)'
    })


@api_v2.route('/trade-log/update-from-positions', methods=['POST'])
def update_trade_log_from_positions():
    """
    Update open trade log entries with current position P/L
    """
    positions = get_positions()

    if not positions['success']:
        return jsonify(positions), 400

    db = get_db()
    user_id = get_user_id()

    updated = 0

    for pos in positions['positions']:
        # Find matching open trade
        trade = db.execute('''
            SELECT id FROM trade_log 
            WHERE user_id = ? AND symbol = ? AND status = 'open'
        ''', (user_id, pos['symbol'])).fetchone()

        if trade:
            # Update with current P/L
            db.execute('''
                UPDATE trade_log
                SET gross_pnl = ?, notes = ?
                WHERE id = ?
            ''', (
                pos['unrealized_pnl'],
                f"Live P/L: ${pos['unrealized_pnl']:.2f} ({pos['pnl_percent']:.1f}%)",
                trade['id']
            ))
            updated += 1

    db.commit()

    return jsonify({
        'success': True,
        'updated': updated,
        'total_positions': len(positions['positions'])
    })


# ══════════════════════════════════════════════════════════════════════════════
# MARKET DATA
# ══════════════════════════════════════════════════════════════════════════════

@api_v2.route('/market-data/<symbol>', methods=['GET'])
def get_symbol_market_data(symbol):
    """Get current market data for a symbol"""
    result = get_market_data(symbol)
    return jsonify(result)


@api_v2.route('/market-data/batch', methods=['POST'])
def get_batch_market_data():
    """Get market data for multiple symbols"""
    data = request.get_json()
    symbols = data.get('symbols', [])

    results = {}
    for symbol in symbols[:50]:  # Limit to 20
        results[symbol] = get_market_data(symbol)

    return jsonify({'results': results})


# ══════════════════════════════════════════════════════════════════════════════
# WORKFLOW STATUS
# ══════════════════════════════════════════════════════════════════════════════

@api_v2.route('/workflow/status', methods=['GET'])
def get_workflow_status():
    """
    Get overall workflow status for dashboard

    Shows:
    - Latest screener results
    - Pending trade bills
    - Open orders
    - Open positions with P/L
    - Alerts
    """
    db = get_db()
    user_id = get_user_id()

    # Get latest scan
    latest_scan = db.execute('''
        SELECT TOP 1 * FROM weekly_scans
        WHERE user_id = ?
        ORDER BY scan_date DESC
    ''', (user_id,)).fetchone()

    scan_summary = None
    if latest_scan:
        scan_summary = json.loads(latest_scan['summary'])
        scan_summary['scan_date'] = latest_scan['scan_date']

    # Get pending trade bills
    pending_bills = db.execute('''
        SELECT COUNT(*) as count FROM trade_bills 
        WHERE user_id = ? AND status = 'PENDING'
    ''', (user_id,)).fetchone()

    # Get open orders from Kite
    orders = get_open_orders()

    # Get positions
    positions = get_positions()

    # Get trade bills for alerts
    bills = db.execute('''
        SELECT * FROM trade_bills WHERE user_id = ?
    ''', (user_id,)).fetchall()
    trade_bills = [dict(b) for b in bills]

    alerts = []
    if positions['success']:
        alerts = get_position_alerts(positions['positions'], trade_bills)

    return jsonify({
        'kite_connected': positions['success'],
        'latest_scan': scan_summary,
        'pending_trade_bills': pending_bills['count'] if pending_bills else 0,
        'open_orders': orders.get('count', 0),
        'open_positions': positions.get('count', 0),
        'total_unrealized_pnl': positions.get('total_unrealized_pnl', 0),
        'total_market_value': positions.get('total_market_value', 0),
        'alerts': alerts,
        'high_priority_alerts': len([a for a in alerts if a.get('severity') == 'HIGH'])
    })


# ══════════════════════════════════════════════════════════════════════════════
# BACKTESTING ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@api_v2.route('/backtest/run', methods=['POST'])
def run_backtest_endpoint():
    """
    Run backtest for a single symbol using Elder's practical methodology

    Body:
    {
        "symbol": "NSE:RELIANCE",
        "market": "IN",
        "lookback_days": 365,
        "initial_capital": 100000,
        "risk_per_trade_pct": 2.0,
        "rr_target": 1.5,
        "min_score": 3
    }

    Core Logic: Weekly UP + Daily DOWN = Go Long
    - Weekly UP: EMA rising OR MACD-H rising OR Price > EMA
    - Daily DOWN: Force Index < 0 OR Stochastic < 50 OR RSI < 50
    """
    from services.backtesting import run_backtest

    data = request.get_json() or {}

    symbol = data.get('symbol')
    if not symbol:
        return jsonify({'error': 'Symbol required'}), 400

    result = run_backtest(
        symbol=symbol,
        market=data.get('market', 'IN'),
        lookback_days=data.get('lookback_days', 365),
        initial_capital=data.get('initial_capital', 100000),
        risk_per_trade_pct=data.get('risk_per_trade_pct', 2.0),
        rr_target=data.get('rr_target', 1.5),
        min_score=data.get('min_score', 3)
    )

    if result:
        return jsonify(result)
    return jsonify({'error': f'Could not run backtest for {symbol}'}), 500


@api_v2.route('/backtest/portfolio', methods=['POST'])
def run_portfolio_backtest_endpoint():
    """
    Run backtest across multiple symbols

    Body:
    {
        "symbols": ["NSE:RELIANCE", "NSE:TCS", "NSE:HDFCBANK"],
        "market": "IN",
        "lookback_days": 365,
        "initial_capital": 100000,
        "risk_per_trade_pct": 2.0,
        "rr_target": 1.5,
        "min_score": 3
    }
    """
    from services.backtesting import run_portfolio_backtest

    data = request.get_json() or {}

    symbols = data.get('symbols', [])
    if not symbols:
        return jsonify({'error': 'Symbols list required'}), 400

    result = run_portfolio_backtest(
        symbols=symbols,
        market=data.get('market', 'IN'),
        lookback_days=data.get('lookback_days', 365),
        initial_capital=data.get('initial_capital', 100000),
        risk_per_trade_pct=data.get('risk_per_trade_pct', 2.0),
        rr_target=data.get('rr_target', 1.5),
        min_score=data.get('min_score', 3)
    )

    return jsonify(result)


@api_v2.route('/backtest/quick/<symbol>', methods=['GET'])
def quick_backtest(symbol):
    """
    Quick backtest for a single symbol with default parameters
    Uses 365 days lookback, 2% risk, 1.5 R:R, min_score 3
    """
    from services.backtesting import run_backtest

    result = run_backtest(
        symbol=symbol,
        market='IN',
        lookback_days=365,
        initial_capital=100000,
        risk_per_trade_pct=2.0,
        rr_target=1.5,
        min_score=3
    )

    if result:
        # Return summary for quick view
        return jsonify({
            'symbol': result['symbol'],
            'period': f"{result['period_days']} days",
            'data_bars': result.get('data_bars', 0),
            'total_signals': result.get('total_signals', 0),
            'total_trades': result['total_trades'],
            'winning_trades': result['winning_trades'],
            'losing_trades': result['losing_trades'],
            'win_rate': result['win_rate'],
            'total_pnl': result['total_pnl'],
            'return_percent': result.get('return_percent', 0),
            'profit_factor': result['profit_factor'],
            'expectancy': result['expectancy'],
            'max_drawdown': result['max_drawdown'],
            'avg_days_held': result.get('avg_days_held', 0),
            'initial_capital': result.get('initial_capital', 100000),
            'final_capital': result.get('final_capital', 100000),
            'trades': result['trades'][:10],  # Last 10 trades for preview
            'full_result_available': True
        })
    return jsonify({'error': f'Could not run backtest for {symbol}'}), 500


# ══════════════════════════════════════════════════════════════════════════════
# HISTORICAL SCREENER ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@api_v2.route('/historical-screener/stocks', methods=['GET'])
def get_available_stocks():
    """
    Get list of available stocks for screening
    """
    market = request.args.get('market', 'IN')

    from services.historical_screener import get_stock_list
    stocks = get_stock_list(market)

    return jsonify({
        'market': market,
        'stocks': stocks,
        'count': len(stocks)
    })


@api_v2.route('/historical-screener/run', methods=['POST'])
def run_historical_screener():
    """
    Run historical screener to find signals

    Request body:
    {
        "symbols": ["NSE:RELIANCE", "NSE:TCS", ...],  // or "all" for all stocks
        "lookback_days": 180,
        "min_score": 5,
        "market": "IN"
    }

    Returns signals sorted by date (newest first)
    """
    data = request.get_json() or {}

    symbols = data.get('symbols', [])
    lookback_days = data.get('lookback_days', 180)
    min_score = data.get('min_score', 5)
    market = data.get('market', 'IN')

    from services.historical_screener import run_historical_screener, get_stock_list

    # Handle "all" or empty symbols
    if not symbols or symbols == 'all' or (isinstance(symbols, list) and 'all' in symbols):
        symbols = get_stock_list(market)

    # Validate
    if lookback_days < 30:
        lookback_days = 30
    if lookback_days > 365:
        lookback_days = 365

    if min_score < 1:
        min_score = 1
    if min_score > 10:
        min_score = 10

    try:
        result = run_historical_screener(
            symbols=symbols,
            lookback_days=lookback_days,
            min_score=min_score
        )

        return jsonify(result)

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@api_v2.route('/historical-screener/single/<symbol>', methods=['GET'])
def scan_single_stock_history(symbol):
    """
    Scan a single stock's history
    """
    lookback_days = request.args.get('lookback_days', 180, type=int)
    min_score = request.args.get('min_score', 5, type=int)

    from services.historical_screener import scan_stock_historical

    try:
        signals = scan_stock_historical(
            symbol=symbol.upper(),
            lookback_days=lookback_days,
            min_score=min_score
        )

        return jsonify({
            'symbol': symbol.upper(),
            'signals': signals,
            'count': len(signals),
            'lookback_days': lookback_days,
            'min_score': min_score
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# NSE INSTRUMENTS ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@api_v2.route('/instruments/search', methods=['GET'])
def search_instruments():
    """
    Typeahead search for NSE instruments.
    Searches the local nse_instruments cache first.
    If cache is empty, auto-downloads from Kite Connect on the fly.

    Query params:
        q: search query (min 1 char)
        limit: max results (default 20)
    """
    query = request.args.get('q', '').strip()
    limit = request.args.get('limit', 20, type=int)

    if not query:
        return jsonify([])

    db = get_db()

    # Check if instruments cache has data
    count_row = db.execute('SELECT COUNT(*) AS cnt FROM nse_instruments').fetchone()
    cache_count = count_row['cnt'] if count_row else 0

    if cache_count == 0:
        # No instruments in local DB - advise user to load from Settings
        return jsonify({'message': 'No instruments in local database. Please load market data from Settings first.', 'instruments': []})

    search_pattern = f'%{query}%'

    results = db.execute('''
        SELECT TOP (?) tradingsymbol, name, instrument_token, lot_size, tick_size
        FROM nse_instruments
        WHERE tradingsymbol LIKE ? OR name LIKE ?
        ORDER BY
            CASE WHEN tradingsymbol LIKE ? THEN 0 ELSE 1 END,
            tradingsymbol
    ''', (limit, search_pattern, search_pattern, f'{query}%')).fetchall()

    return jsonify([{
        'symbol': r['tradingsymbol'],
        'name': r['name'],
        'instrument_token': r['instrument_token'],
        'lot_size': r['lot_size'],
        'tick_size': r['tick_size']
    } for r in results])


@api_v2.route('/instruments/load', methods=['POST'])
def load_instruments():
    """
    Load NIFTY 500 instruments from Kite API and cache in database.
    Should be called once, then periodically to refresh.
    """
    try:
        client = get_client()
        if not client or not client.kite:
            return jsonify({'error': 'Kite not connected. Login first.'}), 400

        if not client._authenticated:
            return jsonify({'error': 'Not authenticated with Kite. Please login first.'}), 401

        # Fetch all NSE instruments via Kite SDK
        client._rate_limit()
        instruments = client.kite.instruments('NSE')

        db = get_db()
        loaded_count = 0

        # NIFTY 500 constituents - we load all NSE EQ instruments
        # and filter by segment
        for inst in instruments:
            if inst.get('segment') != 'NSE' or inst.get('instrument_type') != 'EQ':
                continue

            symbol = inst['tradingsymbol']
            name = inst.get('name', symbol)
            token = inst['instrument_token']
            lot_size = inst.get('lot_size', 1)
            tick_size = inst.get('tick_size', 0.05)

            db.execute('''
                MERGE nse_instruments AS target
                USING (SELECT ? AS tradingsymbol) AS source
                ON target.tradingsymbol = source.tradingsymbol
                WHEN MATCHED THEN
                    UPDATE SET name = ?, instrument_token = ?, lot_size = ?,
                               tick_size = ?, updated_at = GETDATE()
                WHEN NOT MATCHED THEN
                    INSERT (tradingsymbol, name, instrument_token, lot_size, tick_size)
                    VALUES (?, ?, ?, ?, ?);
            ''', (symbol, name, token, lot_size, tick_size,
                  symbol, name, token, lot_size, tick_size))
            loaded_count += 1

        db.commit()

        return jsonify({
            'success': True,
            'loaded': loaded_count,
            'message': f'Loaded {loaded_count} NSE equity instruments'
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# SYNC ALL ENDPOINT (Orders + Positions + Holdings from Kite)
# ══════════════════════════════════════════════════════════════════════════════

@api_v2.route('/sync/all', methods=['POST'])
def sync_all_from_kite():
    """
    Sync orders, positions, and holdings from Kite to SQL Server.
    Called manually via button or auto every 5 minutes when connected.
    """
    try:
        client = get_client()
        if not client or not client.kite or not client.access_token:
            return jsonify({'error': 'Kite not connected. Please login first.'}), 400

        db = get_db()
        user_id = get_user_id()
        sync_time = datetime.now().isoformat()
        results = {}

        # 1. Sync Orders
        try:
            client._rate_limit()
            orders = client.kite.orders()
            # Clear old cache for today
            db.execute(
                "DELETE FROM kite_orders_cache WHERE user_id = ? AND CAST(cached_at AS DATE) = CAST(GETDATE() AS DATE)", (user_id,))

            for order in orders:
                db.execute('''
                    INSERT INTO kite_orders_cache
                    (user_id, order_id, tradingsymbol, exchange, transaction_type,
                     order_type, quantity, price, trigger_price, status,
                     filled_quantity, average_price, placed_at, order_data)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    user_id, order.get('order_id'), order.get('tradingsymbol'),
                    order.get('exchange', 'NSE'), order.get(
                        'transaction_type'),
                    order.get('order_type'), order.get('quantity'),
                    order.get('price', 0), order.get('trigger_price', 0),
                    order.get('status'), order.get('filled_quantity', 0),
                    order.get('average_price', 0),
                    order.get('order_timestamp', sync_time),
                    json.dumps(order)
                ))

            results['orders'] = len(orders)
        except Exception as e:
            results['orders_error'] = str(e)

        # 2. Sync Positions
        try:
            client._rate_limit()
            positions = client.kite.positions()
            day_positions = positions.get('day', [])
            net_positions = positions.get('net', [])
            all_positions = net_positions or day_positions

            db.execute(
                "DELETE FROM kite_positions_cache WHERE user_id = ?", (user_id,))

            for pos in all_positions:
                db.execute('''
                    INSERT INTO kite_positions_cache
                    (user_id, tradingsymbol, exchange, product, quantity,
                     average_price, last_price, pnl, buy_value, sell_value,
                     position_data)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    user_id, pos.get('tradingsymbol'), pos.get(
                        'exchange', 'NSE'),
                    pos.get('product'), pos.get('quantity', 0),
                    pos.get('average_price', 0), pos.get('last_price', 0),
                    pos.get('pnl', 0), pos.get('buy_value', 0),
                    pos.get('sell_value', 0), json.dumps(pos)
                ))

            results['positions'] = len(all_positions)
        except Exception as e:
            results['positions_error'] = str(e)

        # 3. Sync Holdings
        try:
            client._rate_limit()
            holdings = client.kite.holdings()

            db.execute(
                "DELETE FROM kite_holdings_cache WHERE user_id = ?", (user_id,))

            for h in holdings:
                db.execute('''
                    INSERT INTO kite_holdings_cache
                    (user_id, tradingsymbol, exchange, isin, quantity,
                     average_price, last_price, pnl, day_change,
                     day_change_percentage, holding_data)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    user_id, h.get('tradingsymbol'), h.get('exchange', 'NSE'),
                    h.get('isin', ''), h.get('quantity', 0),
                    h.get('average_price', 0), h.get('last_price', 0),
                    h.get('pnl', 0), h.get('day_change', 0),
                    h.get('day_change_percentage', 0), json.dumps(h)
                ))

            results['holdings'] = len(holdings)

            # Also save to holdings_snapshot for historical tracking
            snapshot_date = datetime.now().strftime('%Y-%m-%d')
            for h in holdings:
                db.execute('''
                    MERGE holdings_snapshot AS target
                    USING (SELECT ? AS user_id, ? AS tradingsymbol, ? AS snapshot_date) AS source
                    ON target.user_id = source.user_id
                       AND target.tradingsymbol = source.tradingsymbol
                       AND target.snapshot_date = source.snapshot_date
                    WHEN MATCHED THEN
                        UPDATE SET quantity = ?, average_price = ?, last_price = ?,
                                   pnl = ?, day_change = ?, day_change_percentage = ?,
                                   updated_at = GETDATE()
                    WHEN NOT MATCHED THEN
                        INSERT (user_id, tradingsymbol, snapshot_date, quantity,
                                average_price, last_price, pnl, day_change, day_change_percentage)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                ''', (
                    user_id, h.get('tradingsymbol'), snapshot_date,
                    h.get('quantity', 0), h.get('average_price', 0),
                    h.get('last_price', 0), h.get('pnl', 0),
                    h.get('day_change', 0), h.get('day_change_percentage', 0),
                    user_id, h.get('tradingsymbol'), snapshot_date,
                    h.get('quantity', 0), h.get('average_price', 0),
                    h.get('last_price', 0), h.get('pnl', 0),
                    h.get('day_change', 0), h.get('day_change_percentage', 0)
                ))
        except Exception as e:
            results['holdings_error'] = str(e)

        # 4. Sync GTT Orders
        try:
            client._rate_limit()
            gtt_orders = client.kite.get_gtts()

            # Clear old GTT cache
            db.execute("DELETE FROM kite_gtt_cache WHERE user_id = ?", (user_id,))

            for gtt in gtt_orders:
                condition = gtt.get('condition', {})
                orders_list = gtt.get('orders', [])
                first_order = orders_list[0] if orders_list else {}

                db.execute('''
                    INSERT INTO kite_gtt_cache
                    (user_id, trigger_id, tradingsymbol, exchange, trigger_type,
                     status, trigger_values, quantity, trigger_price,
                     limit_price, transaction_type, created_at, updated_at,
                     expires_at, gtt_data)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    user_id, gtt.get('id'),
                    condition.get('tradingsymbol', gtt.get('tradingsymbol', '')),
                    condition.get('exchange', gtt.get('exchange', 'NSE')),
                    gtt.get('type', 'single'),
                    gtt.get('status', 'active'),
                    json.dumps(condition.get('trigger_values', [])),
                    first_order.get('quantity', 0),
                    condition.get('trigger_values', [0])[0] if condition.get('trigger_values') else 0,
                    first_order.get('price', 0),
                    first_order.get('transaction_type', ''),
                    gtt.get('created_at', sync_time),
                    gtt.get('updated_at', sync_time),
                    gtt.get('expires_at', ''),
                    json.dumps(gtt)
                ))

            results['gtt_orders'] = len(gtt_orders)
        except Exception as e:
            results['gtt_error'] = str(e)

        db.commit()
        results['sync_time'] = sync_time
        results['success'] = True

        return jsonify(results)

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# EXECUTED ORDERS FOR TRADE JOURNAL
# ══════════════════════════════════════════════════════════════════════════════

@api_v2.route('/kite/executed-orders', methods=['GET'])
def get_executed_orders_for_journal():
    """
    Get executed (filled) orders from Kite cache for Trade Journal picker.
    Returns orders that can be selected and added to the journal.
    """
    user_id = get_user_id()
    db = get_db()

    # First try from cache
    orders = db.execute('''
        SELECT order_id, tradingsymbol, exchange, transaction_type,
               order_type, quantity, filled_quantity, average_price,
               placed_at, status, order_data
        FROM kite_orders_cache
        WHERE user_id = ? AND status = 'COMPLETE' AND filled_quantity > 0
        ORDER BY placed_at DESC
    ''', (user_id,)).fetchall()

    result = []
    for o in orders:
        order_data = json.loads(o['order_data']) if o['order_data'] else {}
        result.append({
            'order_id': o['order_id'],
            'symbol': o['tradingsymbol'],
            'exchange': o['exchange'],
            'transaction_type': o['transaction_type'],
            'order_type': o['order_type'],
            'quantity': o['quantity'],
            'filled_quantity': o['filled_quantity'],
            'average_price': o['average_price'],
            'placed_at': o['placed_at'],
            'product': order_data.get('product', ''),
            'tag': order_data.get('tag', '')
        })

    return jsonify({
        'success': True,
        'orders': result,
        'count': len(result)
    })


@api_v2.route('/orders/history', methods=['GET'])
def get_orders_history():
    """
    Get all cached orders (pending + executed) for the Orders screen.
    Groups by status: OPEN, COMPLETE, CANCELLED, REJECTED, etc.
    """
    user_id = get_user_id()
    db = get_db()

    orders = db.execute('''
        SELECT order_id, tradingsymbol, exchange, transaction_type,
               order_type, quantity, filled_quantity, average_price, price,
               trigger_price, placed_at, status, order_data
        FROM kite_orders_cache
        WHERE user_id = ?
        ORDER BY placed_at DESC
    ''', (user_id,)).fetchall()

    pending = []
    executed = []
    other = []

    for o in orders:
        order_dict = dict(o)
        if o['order_data']:
            try:
                extra = json.loads(o['order_data'])
                order_dict['product'] = extra.get('product', '')
                order_dict['tag'] = extra.get('tag', '')
            except:
                pass

        status = (o['status'] or '').upper()
        if status in ('OPEN', 'TRIGGER PENDING', 'PENDING'):
            pending.append(order_dict)
        elif status in ('COMPLETE', 'TRADED'):
            executed.append(order_dict)
        else:
            other.append(order_dict)

    # Also get active GTT orders (live from Kite + cached)
    gtt_list = []
    try:
        gtt_result = get_gtt_orders()
        if gtt_result.get('success'):
            gtt_list = gtt_result.get('gtts', [])
    except:
        # Fallback to cached GTT orders
        try:
            cached_gtts = db.execute('''
                SELECT trigger_id, tradingsymbol, trigger_type, status,
                       trigger_values, quantity, trigger_price, limit_price,
                       transaction_type, gtt_data
                FROM kite_gtt_cache WHERE user_id = ?
            ''', (user_id,)).fetchall()
            for g in cached_gtts:
                gtt_list.append({
                    'trigger_id': g['trigger_id'],
                    'tradingsymbol': g['tradingsymbol'],
                    'trigger_type': g['trigger_type'],
                    'status': g['status'],
                    'trigger_values': json.loads(g['trigger_values']) if g['trigger_values'] else [],
                    'quantity': g['quantity'],
                    'trigger_price': g['trigger_price'],
                    'transaction_type': g['transaction_type']
                })
        except:
            pass

    return jsonify({
        'success': True,
        'pending': pending,
        'executed': executed,
        'other': other,
        'gtt_orders': gtt_list,
        'summary': {
            'pending_count': len(pending),
            'executed_count': len(executed),
            'gtt_count': len(gtt_list),
            'total': len(orders)
        }
    })


# ══════════════════════════════════════════════════════════════════════════════
# DAILY OHLC UPDATE
# ══════════════════════════════════════════════════════════════════════════════

@api_v2.route('/data/update-daily', methods=['POST'])
def update_daily_ohlc():
    """
    Incremental daily OHLC update for stocks that already have cached data.
    Fetches last 2 days of data to fill gaps.
    Can also be triggered for specific symbols.
    """
    try:
        client = get_client()
        if not client:
            return jsonify({'error': 'Kite not connected'}), 400

        data = request.get_json() or {}
        symbols = data.get('symbols', None)

        db = get_db()

        # If no specific symbols, get all symbols that have cached data
        if not symbols:
            rows = db.execute('''
                SELECT DISTINCT symbol FROM stock_historical_data
            ''').fetchall()
            symbols = [r['symbol'] for r in rows]

        if not symbols:
            return jsonify({'message': 'No symbols to update', 'updated': 0})

        updated_count = 0
        errors = []

        from_date = (datetime.now() - timedelta(days=3)).strftime('%Y-%m-%d')
        to_date = datetime.now().strftime('%Y-%m-%d')

        for symbol in symbols:
            try:
                # Get instrument token from nse_instruments
                tradingsymbol = symbol.replace('NSE:', '')
                inst = db.execute('''
                    SELECT TOP 1 instrument_token FROM nse_instruments
                    WHERE tradingsymbol = ?
                ''', (tradingsymbol,)).fetchone()

                if not inst:
                    continue

                token = inst['instrument_token']

                # Fetch last 2-3 days of daily candles
                candles = client.historical_data(
                    token, from_date, to_date, 'day'
                )

                for candle in candles:
                    candle_date = candle['date'].strftime(
                        '%Y-%m-%d') if hasattr(candle['date'], 'strftime') else str(candle['date'])[:10]

                    db.execute('''
                        MERGE stock_historical_data AS target
                        USING (SELECT ? AS symbol, ? AS date) AS source
                        ON target.symbol = source.symbol AND target.date = source.date
                        WHEN MATCHED THEN
                            UPDATE SET [open] = ?, high = ?, low = ?, [close] = ?, volume = ?
                        WHEN NOT MATCHED THEN
                            INSERT (symbol, date, [open], high, low, [close], volume)
                            VALUES (?, ?, ?, ?, ?, ?, ?);
                    ''', (
                        symbol, candle_date,
                        candle['open'], candle['high'], candle['low'],
                        candle['close'], candle['volume'],
                        symbol, candle_date, candle['open'], candle['high'],
                        candle['low'], candle['close'], candle['volume']
                    ))

                updated_count += 1

            except Exception as e:
                errors.append({'symbol': symbol, 'error': str(e)})

        db.commit()

        # Update sync record
        db.execute('''
            MERGE stock_data_sync AS target
            USING (SELECT 'daily_ohlc_update' AS symbol) AS source
            ON target.symbol = source.symbol
            WHEN MATCHED THEN
                UPDATE SET last_sync = GETDATE(), sync_status = 'success'
            WHEN NOT MATCHED THEN
                INSERT (symbol, last_sync, sync_status, data_from, data_to)
                VALUES ('daily_ohlc_update', GETDATE(), 'success', ?, ?);
        ''', (from_date, to_date))
        db.commit()

        return jsonify({
            'success': True,
            'updated': updated_count,
            'total_symbols': len(symbols),
            'errors': errors if errors else None,
            'period': {'from': from_date, 'to': to_date}
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ══════════════════════════════════════════════════════════════════════════════
# HOLDINGS HISTORY
# ══════════════════════════════════════════════════════════════════════════════

@api_v2.route('/holdings/history', methods=['GET'])
def get_holdings_history():
    """
    Get historical holdings snapshots.

    Query params:
        days: number of days to look back (default 30)
        symbol: filter by specific symbol (optional)
    """
    user_id = get_user_id()
    days = request.args.get('days', 30, type=int)
    symbol = request.args.get('symbol', None)

    db = get_db()

    if symbol:
        snapshots = db.execute('''
            SELECT * FROM holdings_snapshot
            WHERE user_id = ? AND tradingsymbol = ?
              AND snapshot_date >= DATEADD(day, ?, GETDATE())
            ORDER BY snapshot_date DESC
        ''', (user_id, symbol, -days)).fetchall()
    else:
        snapshots = db.execute('''
            SELECT * FROM holdings_snapshot
            WHERE user_id = ?
              AND snapshot_date >= DATEADD(day, ?, GETDATE())
            ORDER BY snapshot_date DESC, tradingsymbol
        ''', (user_id, -days)).fetchall()

    return jsonify({
        'success': True,
        'snapshots': [dict(s) for s in snapshots],
        'count': len(snapshots)
    })


# ══════════════════════════════════════════════════════════════════════════════
# ACCOUNT SECTION - Orders/Positions/Holdings from Cache
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
# MISTAKES CRUD (Global - not per-user)
# ══════════════════════════════════════════════════════════════════════════════

@api_v2.route('/mistakes', methods=['GET'])
def get_mistakes():
    """List all active mistakes"""
    db = get_db()
    rows = db.execute('''
        SELECT * FROM mistakes WHERE is_active = 1 ORDER BY display_order, id
    ''').fetchall()
    return jsonify([dict(r) for r in rows])


@api_v2.route('/mistakes', methods=['POST'])
def create_mistake():
    """Create a new mistake"""
    data = request.get_json()
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'error': 'Name is required'}), 400

    db = get_db()
    # Get next display_order
    max_order = db.execute('SELECT MAX(display_order) AS mx FROM mistakes').fetchone()
    next_order = (max_order['mx'] or 0) + 1

    m_row = db.execute('''
        INSERT INTO mistakes (name, description, display_order)
        OUTPUT INSERTED.id
        VALUES (?, ?, ?)
    ''', (name, data.get('description', ''), next_order)).fetchone()
    db.commit()
    mid = int(m_row[0])
    return jsonify({'success': True, 'id': mid}), 201


@api_v2.route('/mistakes/<int:mistake_id>', methods=['PUT'])
def update_mistake(mistake_id):
    """Update a mistake"""
    data = request.get_json()
    db = get_db()
    db.execute('''
        UPDATE mistakes SET name = ?, description = ? WHERE id = ?
    ''', (data.get('name'), data.get('description', ''), mistake_id))
    db.commit()
    return jsonify({'success': True})


@api_v2.route('/mistakes/<int:mistake_id>', methods=['DELETE'])
def delete_mistake(mistake_id):
    """Soft-delete a mistake"""
    db = get_db()
    db.execute('UPDATE mistakes SET is_active = 0 WHERE id = ?', (mistake_id,))
    db.commit()
    return jsonify({'success': True})


# ══════════════════════════════════════════════════════════════════════════════
# STRATEGIES CRUD (Global - user_id IS NULL)
# ══════════════════════════════════════════════════════════════════════════════

@api_v2.route('/strategies/global', methods=['GET'])
def get_global_strategies():
    """List global strategies (user_id IS NULL)"""
    db = get_db()
    rows = db.execute('''
        SELECT * FROM strategies WHERE user_id IS NULL AND is_active = 1 ORDER BY id
    ''').fetchall()
    return jsonify([dict(r) for r in rows])


@api_v2.route('/strategies/global', methods=['POST'])
def create_global_strategy():
    """Create a global strategy"""
    data = request.get_json()
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'error': 'Name is required'}), 400

    db = get_db()
    s_row = db.execute('''
        INSERT INTO strategies (user_id, name, description, config)
        OUTPUT INSERTED.id
        VALUES (NULL, ?, ?, ?)
    ''', (name, data.get('description', ''), json.dumps(data.get('config', {})))).fetchone()
    db.commit()
    sid = int(s_row[0])
    return jsonify({'success': True, 'id': sid}), 201


@api_v2.route('/strategies/global/<int:strategy_id>', methods=['PUT'])
def update_global_strategy(strategy_id):
    """Update a global strategy"""
    data = request.get_json()
    db = get_db()
    db.execute('''
        UPDATE strategies SET name = ?, description = ?
        WHERE id = ? AND user_id IS NULL
    ''', (data.get('name'), data.get('description', ''), strategy_id))
    db.commit()
    return jsonify({'success': True})


@api_v2.route('/strategies/global/<int:strategy_id>', methods=['DELETE'])
def delete_global_strategy(strategy_id):
    """Soft-delete a global strategy"""
    db = get_db()
    db.execute('UPDATE strategies SET is_active = 0 WHERE id = ? AND user_id IS NULL', (strategy_id,))
    db.commit()
    return jsonify({'success': True})


# ══════════════════════════════════════════════════════════════════════════════
# TRADE BILL ENHANCEMENTS - Live CMP, ATR, Candle Pattern Detection
# ══════════════════════════════════════════════════════════════════════════════

@api_v2.route('/live-cmp/<symbol>', methods=['GET'])
def get_live_cmp(symbol):
    """
    Get live CMP from Kite API. ALWAYS tries Kite first for real-time price.
    Falls back to cached close only when Kite is not connected.
    """
    full_sym = symbol if ':' in symbol else f'NSE:{symbol}'

    # ALWAYS try to get live price from Kite first
    try:
        client = get_client()
        if client and client.kite and client.access_token:
            client._rate_limit()
            ltp_data = client.kite.ltp([full_sym])
            if ltp_data and full_sym in ltp_data:
                return jsonify({
                    'symbol': full_sym,
                    'cmp': ltp_data[full_sym]['last_price'],
                    'source': 'live',
                    'timestamp': datetime.now().isoformat()
                })
    except Exception as e:
        print(f"Live CMP error for {full_sym}: {e}")

    # Fallback to cached close price ONLY if Kite unavailable
    db = get_db()
    row = db.execute('''
        SELECT TOP 1 [close], date FROM stock_historical_data
        WHERE symbol = ? ORDER BY date DESC
    ''', (full_sym,)).fetchone()

    if row:
        return jsonify({
            'symbol': full_sym,
            'cmp': row['close'],
            'source': 'cache',
            'date': row['date']
        })

    return jsonify({'error': f'No data for {symbol}'}), 404


@api_v2.route('/live-cmp/batch', methods=['GET'])
def get_live_cmp_batch():
    """
    Get live CMP for multiple symbols at once.
    Query: ?symbols=RELIANCE,TCS,INFY
    Kite LTP API supports batch queries natively.
    """
    symbols_param = request.args.get('symbols', '')
    if not symbols_param:
        return jsonify({'error': 'symbols parameter required'}), 400

    symbols = [s.strip() for s in symbols_param.split(',') if s.strip()]
    if not symbols:
        return jsonify({'error': 'No valid symbols provided'}), 400

    full_symbols = [s if ':' in s else f'NSE:{s}' for s in symbols]
    result = {}

    # Try Kite first for live prices (batch)
    try:
        client = get_client()
        if client and client.kite and client.access_token:
            client._rate_limit()
            ltp_data = client.kite.ltp(full_symbols)
            for full_sym in full_symbols:
                bare = full_sym.replace('NSE:', '')
                if full_sym in ltp_data:
                    result[bare] = {
                        'cmp': ltp_data[full_sym]['last_price'],
                        'source': 'live'
                    }
    except Exception as e:
        logger.debug(f"Batch CMP live error: {e}")

    # Fallback for any missing symbols — use cached close
    db = get_db()
    for full_sym in full_symbols:
        bare = full_sym.replace('NSE:', '')
        if bare not in result:
            row = db.execute('''
                SELECT TOP 1 [close], date FROM stock_historical_data
                WHERE symbol = ? ORDER BY date DESC
            ''', (full_sym,)).fetchone()
            if row:
                result[bare] = {
                    'cmp': row['close'],
                    'source': 'cache'
                }

    return jsonify({
        'prices': result,
        'timestamp': datetime.now().isoformat()
    })


@api_v2.route('/stock-atr/<symbol>', methods=['GET'])
def get_stock_atr(symbol):
    """Get ATR from cached indicators"""
    db = get_db()
    full_sym = symbol if ':' in symbol else f'NSE:{symbol}'

    row = db.execute('''
        SELECT TOP 1 atr, date FROM stock_indicators_daily
        WHERE symbol = ? ORDER BY date DESC
    ''', (full_sym,)).fetchone()

    if row and row['atr']:
        return jsonify({
            'symbol': full_sym,
            'atr': round(row['atr'], 2),
            'date': row['date']
        })

    return jsonify({'error': f'No ATR data for {symbol}'}), 404


@api_v2.route('/candle-pattern/<symbol>', methods=['GET'])
def detect_candle_pattern(symbol):
    """
    Auto-detect candlestick patterns from last 3 candles of cached OHLC data.
    Also calculates conviction/confusing analysis per candle.
    Conviction: body > 50% of range. Confusing: body <= 50% of range.
    """
    db = get_db()
    full_sym = symbol if ':' in symbol else f'NSE:{symbol}'

    candles = db.execute('''
        SELECT TOP 3 date, [open], high, low, [close], volume
        FROM stock_historical_data
        WHERE symbol = ?
        ORDER BY date DESC
    ''', (full_sym,)).fetchall()

    if len(candles) < 2:
        return jsonify({'error': f'Need at least 2 candles for {symbol}'}), 404

    # Conviction/Confusing analysis per candle
    candle_analysis = []
    for c in candles:
        body = abs(c['close'] - c['open'])
        candle_range = c['high'] - c['low']
        ratio = body / candle_range if candle_range > 0 else 0
        candle_analysis.append({
            'date': c['date'],
            'open': c['open'], 'high': c['high'],
            'low': c['low'], 'close': c['close'],
            'body': round(body, 2),
            'range': round(candle_range, 2),
            'ratio': round(ratio, 4),
            'type': 'Conviction' if ratio > 0.5 else 'Confusing',
            'color': 'green' if c['close'] >= c['open'] else 'red'
        })

    # Pattern detection using candlestick_patterns service
    patterns_found = []
    try:
        import pandas as pd
        from services.candlestick_patterns import scan_patterns
        candles_rev = list(reversed(candles))
        df = pd.DataFrame([{
            'Open': c['open'], 'High': c['high'],
            'Low': c['low'], 'Close': c['close'],
            'Volume': c['volume']
        } for c in candles_rev])
        df.index = pd.to_datetime([c['date'] for c in candles_rev])

        detected = scan_patterns(df)
        if detected:
            patterns_found = detected
    except Exception as e:
        print(f"Pattern detection error: {e}")

    return jsonify({
        'symbol': full_sym,
        'candles': candle_analysis,
        'patterns': patterns_found,
        'candle_1_conviction': candle_analysis[0]['type'] if len(candle_analysis) > 0 else None,
        'candle_2_conviction': candle_analysis[1]['type'] if len(candle_analysis) > 1 else None
    })


@api_v2.route('/portfolio/context', methods=['GET'])
def get_portfolio_context():
    """
    Get combined positions + holdings context for Trade Bill and dashboard.
    Auto-fetches from Kite if cache is empty and Kite is connected.
    Shows: open positions, capital locked, total P/L, holdings value.
    """
    user_id = get_user_id()
    db = get_db()

    # Get positions from cache
    positions = db.execute('''
        SELECT tradingsymbol, product, quantity, average_price, last_price, pnl
        FROM kite_positions_cache
        WHERE user_id = ? AND quantity != 0
    ''', (user_id,)).fetchall()

    # Get holdings from cache
    holdings = db.execute('''
        SELECT tradingsymbol, quantity, average_price, last_price, pnl,
               day_change, day_change_percentage
        FROM kite_holdings_cache
        WHERE user_id = ?
    ''', (user_id,)).fetchall()

    # If cache is empty, try to sync directly from Kite
    if not positions and not holdings:
        try:
            client = get_client()
            if client and client.kite and client.access_token:
                # Fetch positions directly from Kite
                try:
                    client._rate_limit()
                    kite_positions = client.kite.positions()
                    net_positions = kite_positions.get('net', [])
                    positions_list = []
                    for pos in net_positions:
                        if pos.get('quantity', 0) != 0:
                            positions_list.append({
                                'tradingsymbol': pos.get('tradingsymbol', ''),
                                'product': pos.get('product', ''),
                                'quantity': pos.get('quantity', 0),
                                'average_price': pos.get('average_price', 0),
                                'last_price': pos.get('last_price', 0),
                                'pnl': pos.get('pnl', 0)
                            })
                    positions = positions_list
                except Exception as e:
                    print(f"Portfolio positions fetch error: {e}")

                # Fetch holdings directly from Kite
                try:
                    client._rate_limit()
                    kite_holdings = client.kite.holdings()
                    holdings_list = []
                    for h in kite_holdings:
                        holdings_list.append({
                            'tradingsymbol': h.get('tradingsymbol', ''),
                            'quantity': h.get('quantity', 0),
                            'average_price': h.get('average_price', 0),
                            'last_price': h.get('last_price', 0),
                            'pnl': h.get('pnl', 0),
                            'day_change': h.get('day_change', 0),
                            'day_change_percentage': h.get('day_change_percentage', 0)
                        })
                    holdings = holdings_list
                except Exception as e:
                    print(f"Portfolio holdings fetch error: {e}")
        except Exception as e:
            print(f"Portfolio auto-sync error: {e}")

    # Get account settings
    account = db.execute('''
        SELECT trading_capital FROM account_settings WHERE user_id = ?
    ''', (user_id,)).fetchone()
    capital = account['trading_capital'] if account else 500000

    # Calculate totals (handle both dict and DictRow objects)
    def get_val(row, key, default=0):
        try:
            return row[key] or default
        except (KeyError, TypeError):
            return default

    positions_value = sum(abs(get_val(p, 'quantity') * get_val(p, 'average_price')) for p in positions)
    positions_pnl = sum(get_val(p, 'pnl') for p in positions)
    holdings_value = sum(get_val(h, 'quantity') * get_val(h, 'last_price') for h in holdings)
    holdings_pnl = sum(get_val(h, 'pnl') for h in holdings)

    total_locked = positions_value + holdings_value
    capital_used_pct = (total_locked / capital * 100) if capital > 0 else 0

    # Convert to dicts for JSON serialization
    def to_dict(row):
        if isinstance(row, dict):
            return row
        return dict(row)

    return jsonify({
        'success': True,
        'positions': [to_dict(p) for p in positions],
        'holdings': [to_dict(h) for h in holdings],
        'summary': {
            'open_positions': len(positions),
            'total_holdings': len(holdings),
            'positions_value': round(positions_value, 2),
            'positions_pnl': round(positions_pnl, 2),
            'holdings_value': round(holdings_value, 2),
            'holdings_pnl': round(holdings_pnl, 2),
            'total_locked': round(total_locked, 2),
            'trading_capital': capital,
            'capital_used_pct': round(capital_used_pct, 2),
            'available_capital': round(capital - total_locked, 2)
        }
    })


@api_v2.route('/account/overview', methods=['GET'])
def get_account_overview():
    """
    Get complete account overview including cached orders, positions, holdings.
    Reads from SQL Server cache tables populated by /sync/all.
    """
    user_id = get_user_id()
    db = get_db()

    # Get cached orders
    orders = db.execute('''
        SELECT order_id, tradingsymbol, transaction_type, order_type,
               quantity, filled_quantity, average_price, status, placed_at
        FROM kite_orders_cache
        WHERE user_id = ?
        ORDER BY placed_at DESC
    ''', (user_id,)).fetchall()

    # Get cached positions
    positions = db.execute('''
        SELECT tradingsymbol, product, quantity, average_price,
               last_price, pnl, buy_value, sell_value
        FROM kite_positions_cache
        WHERE user_id = ? AND quantity != 0
    ''', (user_id,)).fetchall()

    # Get cached holdings
    holdings = db.execute('''
        SELECT tradingsymbol, isin, quantity, average_price,
               last_price, pnl, day_change, day_change_percentage
        FROM kite_holdings_cache
        WHERE user_id = ?
    ''', (user_id,)).fetchall()

    # Calculate totals
    total_positions_value = sum(
        abs(p['quantity'] * p['average_price']) for p in positions
    )
    total_positions_pnl = sum(p['pnl'] for p in positions)
    total_holdings_value = sum(
        h['quantity'] * h['last_price'] for h in holdings
    )
    total_holdings_pnl = sum(h['pnl'] for h in holdings)

    return jsonify({
        'success': True,
        'orders': [dict(o) for o in orders],
        'positions': [dict(p) for p in positions],
        'holdings': [dict(h) for h in holdings],
        'summary': {
            'total_orders': len(orders),
            'open_positions': len(positions),
            'total_holdings': len(holdings),
            'positions_value': total_positions_value,
            'positions_pnl': total_positions_pnl,
            'holdings_value': total_holdings_value,
            'holdings_pnl': total_holdings_pnl,
            'money_locked': total_positions_value
        }
    })


# ══════════════════════════════════════════════════════════════════════════
# TRADING WATCHLIST ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════

@api_v2.route('/trading-watchlist', methods=['GET'])
def get_trading_watchlist():
    """Get the trading watchlist with LTP and latest indicators per timeframe"""
    db = get_db()
    user_id = get_user_id()

    # Find the trading watchlist
    wl = db.execute('''
        SELECT id, name, symbols FROM watchlists
        WHERE user_id = ? AND is_trading_watchlist = 1
    ''', (user_id,)).fetchone()

    if not wl:
        return jsonify({'success': True, 'watchlist': None, 'symbols': [], 'data': []})

    raw_symbols = json.loads(wl['symbols']) if wl['symbols'] else []
    # Normalize all symbols to bare format (strip NSE: prefix)
    symbols = [s.replace('NSE:', '').strip().upper() for s in raw_symbols]

    # Fetch live LTP from Kite for all symbols at once (batch call)
    ltp_map = {}
    try:
        client = get_client()
        if client and client._authenticated and symbols:
            ltp_data = client.get_ltp(symbols)
            for sym_key, data in ltp_data.items():
                bare = sym_key.replace('NSE:', '').strip()
                ltp_map[bare] = data.get('last_price')
    except Exception as e:
        print(f"  LTP fetch for watchlist: {e}")

    # Build enriched data for each symbol
    enriched = []
    for sym in symbols:
        row = {'symbol': sym}

        # Get latest indicator per timeframe (1D, 75min, 15min)
        for tf in ['day', '75min', '15min']:
            ind = db.execute('''
                SELECT TOP 1 rsi, atr, impulse_color, kc_upper, kc_middle, kc_lower,
                       ema_13, ema_22, macd_histogram, candle_time
                FROM intraday_indicators
                WHERE symbol = ? AND timeframe = ?
                ORDER BY candle_time DESC
            ''', (sym, tf)).fetchone()

            if ind:
                row[f'{tf}_rsi'] = ind['rsi']
                row[f'{tf}_atr'] = ind['atr']
                row[f'{tf}_impulse'] = ind['impulse_color']
                row[f'{tf}_kc_upper'] = ind['kc_upper']
                row[f'{tf}_kc_middle'] = ind['kc_middle']
                row[f'{tf}_kc_lower'] = ind['kc_lower']
                row[f'{tf}_ema13'] = ind['ema_13']
                row[f'{tf}_ema22'] = ind['ema_22']
                row[f'{tf}_macd_hist'] = ind['macd_histogram']
                row[f'{tf}_time'] = str(ind['candle_time']) if ind['candle_time'] else None

        # Use live LTP if available, fallback to latest daily close
        if sym in ltp_map:
            row['ltp'] = ltp_map[sym]
        else:
            latest = db.execute('''
                SELECT TOP 1 [close] FROM intraday_ohlcv
                WHERE symbol = ? AND timeframe = 'day'
                ORDER BY candle_time DESC
            ''', (sym,)).fetchone()
            row['ltp'] = latest['close'] if latest else None

        # Count active alerts for this symbol
        alert_count = db.execute('''
            SELECT COUNT(*) as cnt FROM stock_alerts
            WHERE user_id = ? AND symbol = ? AND status = 'active'
        ''', (user_id, sym)).fetchone()
        row['active_alerts'] = alert_count['cnt'] if alert_count else 0

        enriched.append(row)

    return jsonify({
        'success': True,
        'watchlist': {'id': wl['id'], 'name': wl['name']},
        'symbols': symbols,
        'data': enriched
    })


@api_v2.route('/trading-watchlist', methods=['POST'])
def create_or_update_trading_watchlist():
    """Create or update the trading watchlist"""
    db = get_db()
    user_id = get_user_id()
    data = request.get_json()
    # Normalize all symbols to bare format (strip NSE: prefix)
    symbols = [s.replace('NSE:', '').strip().upper() for s in data.get('symbols', [])]
    name = data.get('name', 'Trading Watchlist')

    # Check if trading watchlist exists
    existing = db.execute('''
        SELECT id FROM watchlists
        WHERE user_id = ? AND is_trading_watchlist = 1
    ''', (user_id,)).fetchone()

    if existing:
        db.execute('''
            UPDATE watchlists SET symbols = ?, name = ?, auto_refresh = 1
            WHERE id = ?
        ''', (json.dumps(symbols), name, existing['id']))
        wl_id = existing['id']
    else:
        row = db.execute('''
            INSERT INTO watchlists (user_id, name, market, symbols, is_default, is_trading_watchlist, auto_refresh)
            OUTPUT INSERTED.id
            VALUES (?, ?, 'IN', ?, 0, 1, 1)
        ''', (user_id, name, json.dumps(symbols))).fetchone()
        wl_id = int(row[0])

    db.commit()
    return jsonify({'success': True, 'id': wl_id, 'symbols': symbols})


@api_v2.route('/trading-watchlist/add-symbol', methods=['POST'])
def add_trading_watchlist_symbol():
    """Add a single symbol to the trading watchlist"""
    db = get_db()
    user_id = get_user_id()
    data = request.get_json()
    symbol = data.get('symbol', '').strip()

    if not symbol:
        return jsonify({'error': 'Symbol is required'}), 400

    # Store clean symbol without exchange prefix
    symbol = symbol.replace('NSE:', '').strip().upper()

    # Get or create trading watchlist
    wl = db.execute('''
        SELECT id, symbols FROM watchlists
        WHERE user_id = ? AND is_trading_watchlist = 1
    ''', (user_id,)).fetchone()

    if wl:
        raw_symbols = json.loads(wl['symbols']) if wl['symbols'] else []
        # Normalize existing symbols: strip NSE: prefix, deduplicate
        symbols = list(dict.fromkeys(
            s.replace('NSE:', '').strip().upper() for s in raw_symbols
        ))
        if symbol not in symbols:
            symbols.append(symbol)
        # Always update to ensure normalized format is persisted
        db.execute('UPDATE watchlists SET symbols = ? WHERE id = ?',
                   (json.dumps(symbols), wl['id']))
        wl_id = wl['id']
    else:
        symbols = [symbol]
        row = db.execute('''
            INSERT INTO watchlists (user_id, name, market, symbols, is_default, is_trading_watchlist, auto_refresh)
            OUTPUT INSERTED.id
            VALUES (?, 'Trading Watchlist', 'IN', ?, 0, 1, 1)
        ''', (user_id, json.dumps(symbols))).fetchone()
        wl_id = int(row[0])

    db.commit()
    return jsonify({'success': True, 'id': wl_id, 'symbols': symbols})


@api_v2.route('/trading-watchlist/remove-symbol/<path:symbol>', methods=['DELETE'])
def remove_trading_watchlist_symbol(symbol):
    """Remove a symbol from the trading watchlist"""
    db = get_db()
    user_id = get_user_id()

    # Normalize to bare symbol
    bare_symbol = symbol.replace('NSE:', '').strip().upper()

    wl = db.execute('''
        SELECT id, symbols FROM watchlists
        WHERE user_id = ? AND is_trading_watchlist = 1
    ''', (user_id,)).fetchone()

    if not wl:
        return jsonify({'error': 'Trading watchlist not found'}), 404

    symbols = json.loads(wl['symbols']) if wl['symbols'] else []

    # Remove symbol (try bare and with NSE: prefix for backward compat)
    if bare_symbol in symbols:
        symbols.remove(bare_symbol)
    elif f'NSE:{bare_symbol}' in symbols:
        symbols.remove(f'NSE:{bare_symbol}')
    else:
        return jsonify({'error': f'Symbol {bare_symbol} not in watchlist'}), 404

    db.execute('UPDATE watchlists SET symbols = ? WHERE id = ?',
               (json.dumps(symbols), wl['id']))

    # Also deactivate any alerts for this symbol (check both formats)
    db.execute('''
        UPDATE stock_alerts SET status = 'paused'
        WHERE user_id = ? AND (symbol = ? OR symbol = ?) AND status = 'active'
    ''', (user_id, bare_symbol, f'NSE:{bare_symbol}'))

    db.commit()
    return jsonify({'success': True, 'symbols': symbols})


@api_v2.route('/trading-watchlist/symbols', methods=['GET'])
def get_trading_watchlist_symbols():
    """Lightweight endpoint — just symbol list for the engine"""
    db = get_db()
    user_id = get_user_id()

    wl = db.execute('''
        SELECT symbols FROM watchlists
        WHERE user_id = ? AND is_trading_watchlist = 1 AND auto_refresh = 1
    ''', (user_id,)).fetchone()

    if not wl:
        return jsonify({'success': True, 'symbols': []})

    symbols = json.loads(wl['symbols']) if wl['symbols'] else []
    return jsonify({'success': True, 'symbols': symbols})


# ══════════════════════════════════════════════════════════════════════════
# TIMEFRAME DATA ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════

@api_v2.route('/timeframe/refresh', methods=['POST'])
def refresh_timeframe_data():
    """Refresh multi-timeframe data for all watchlist symbols (or specific symbol)"""
    from services.timeframe_data import refresh_all_timeframes, refresh_symbol_timeframes
    data = request.get_json() or {}
    symbol = data.get('symbol')

    if symbol:
        # Refresh single symbol
        result = refresh_symbol_timeframes(symbol)
        return jsonify({'success': True, 'result': result})
    else:
        # Refresh all watchlist symbols
        db = get_db()
        user_id = get_user_id()
        wl = db.execute('''
            SELECT symbols FROM watchlists
            WHERE user_id = ? AND is_trading_watchlist = 1
        ''', (user_id,)).fetchone()

        if not wl:
            return jsonify({'error': 'No trading watchlist found'}), 404

        symbols = json.loads(wl['symbols']) if wl['symbols'] else []
        if not symbols:
            return jsonify({'error': 'Watchlist is empty'}), 400

        result = refresh_all_timeframes(symbols)
        return jsonify({'success': True, 'result': result})


@api_v2.route('/timeframe/indicators/<path:symbol>', methods=['GET'])
def get_timeframe_indicators(symbol):
    """Get latest indicators for a symbol across all timeframes"""
    from services.timeframe_data import get_latest_indicators

    result = {}
    for tf in ['day', '75min', '15min']:
        ind = get_latest_indicators(symbol, tf)
        if ind:
            result[tf] = ind

    return jsonify({'success': True, 'symbol': symbol, 'indicators': result})


@api_v2.route('/timeframe/ohlcv/<path:symbol>', methods=['GET'])
def get_timeframe_ohlcv(symbol):
    """Get stored OHLCV candles for a symbol and timeframe"""
    from services.timeframe_data import get_ohlcv_history

    timeframe = request.args.get('timeframe', 'day')
    limit = int(request.args.get('limit', 100))

    candles = get_ohlcv_history(symbol, timeframe, limit)
    return jsonify({'success': True, 'symbol': symbol, 'timeframe': timeframe, 'candles': candles})


@api_v2.route('/timeframe/status', methods=['GET'])
def get_timeframe_status():
    """Get data freshness status for all watchlist symbols"""
    db = get_db()
    user_id = get_user_id()

    wl = db.execute('''
        SELECT symbols FROM watchlists
        WHERE user_id = ? AND is_trading_watchlist = 1
    ''', (user_id,)).fetchone()

    if not wl:
        return jsonify({'success': True, 'symbols': []})

    symbols = json.loads(wl['symbols']) if wl['symbols'] else []
    status_list = []

    for sym in symbols:
        sym_status = {'symbol': sym}
        for tf in ['day', '75min', '15min']:
            row = db.execute('''
                SELECT TOP 1 candle_time FROM intraday_ohlcv
                WHERE symbol = ? AND timeframe = ?
                ORDER BY candle_time DESC
            ''', (sym, tf)).fetchone()
            sym_status[f'{tf}_latest'] = str(row['candle_time']) if row else None

            ind_row = db.execute('''
                SELECT TOP 1 candle_time FROM intraday_indicators
                WHERE symbol = ? AND timeframe = ?
                ORDER BY candle_time DESC
            ''', (sym, tf)).fetchone()
            sym_status[f'{tf}_ind_latest'] = str(ind_row['candle_time']) if ind_row else None

        status_list.append(sym_status)

    return jsonify({'success': True, 'status': status_list})


# ══════════════════════════════════════════════════════════════════════════
# ALERT CRUD ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════

@api_v2.route('/alerts', methods=['GET'])
def get_alerts():
    """Get all alerts for the user, optionally filtered by status or symbol"""
    db = get_db()
    user_id = get_user_id()
    status = request.args.get('status')
    symbol = request.args.get('symbol')

    query = 'SELECT * FROM stock_alerts WHERE user_id = ?'
    params = [user_id]

    if status:
        query += ' AND status = ?'
        params.append(status)
    if symbol:
        query += ' AND symbol = ?'
        params.append(symbol)

    query += ' ORDER BY created_at DESC'
    alerts = db.execute(query, tuple(params)).fetchall()
    return jsonify({'success': True, 'alerts': [dict(a) for a in alerts]})


@api_v2.route('/alerts/<int:alert_id>', methods=['GET'])
def get_alert(alert_id):
    """Get a single alert by ID"""
    db = get_db()
    user_id = get_user_id()

    alert = db.execute('''
        SELECT * FROM stock_alerts WHERE id = ? AND user_id = ?
    ''', (alert_id, user_id)).fetchone()

    if not alert:
        return jsonify({'error': 'Alert not found'}), 404

    return jsonify({'success': True, 'alert': dict(alert)})


@api_v2.route('/alerts', methods=['POST'])
def create_alert():
    """Create a new price alert"""
    db = get_db()
    user_id = get_user_id()
    data = request.get_json()

    symbol = data.get('symbol', '').strip()
    if not symbol:
        return jsonify({'error': 'Symbol is required'}), 400
    # Store bare symbol without exchange prefix (all trades are NSE)
    symbol = symbol.replace('NSE:', '').strip().upper()

    row = db.execute('''
        INSERT INTO stock_alerts (
            user_id, symbol, alert_name, direction,
            condition_type, condition_value, condition_operator,
            timeframe, candle_confirm, candle_pattern,
            auto_trade, stop_loss, target_price, quantity,
            cooldown_minutes, notes
        )
        OUTPUT INSERTED.id
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        user_id, symbol,
        data.get('alert_name', f'{symbol} Alert'),
        data.get('direction', 'LONG'),
        data.get('condition_type', 'price_below'),
        data.get('condition_value'),
        data.get('condition_operator', '<='),
        data.get('timeframe', '15min'),
        1 if data.get('candle_confirm') else 0,
        data.get('candle_pattern'),
        1 if data.get('auto_trade') else 0,
        data.get('stop_loss'),
        data.get('target_price'),
        data.get('quantity'),
        data.get('cooldown_minutes', 60),
        data.get('notes')
    )).fetchone()

    db.commit()
    alert_id = int(row[0])
    return jsonify({'success': True, 'id': alert_id}), 201


@api_v2.route('/alerts/<int:alert_id>', methods=['PUT'])
def update_alert(alert_id):
    """Update an existing alert"""
    db = get_db()
    user_id = get_user_id()
    data = request.get_json()

    # Verify ownership
    existing = db.execute('''
        SELECT id FROM stock_alerts WHERE id = ? AND user_id = ?
    ''', (alert_id, user_id)).fetchone()
    if not existing:
        return jsonify({'error': 'Alert not found'}), 404

    db.execute('''
        UPDATE stock_alerts SET
            alert_name = ?, direction = ?,
            condition_type = ?, condition_value = ?, condition_operator = ?,
            timeframe = ?, candle_confirm = ?, candle_pattern = ?,
            auto_trade = ?, stop_loss = ?, target_price = ?, quantity = ?,
            cooldown_minutes = ?, notes = ?, status = ?,
            updated_at = GETDATE()
        WHERE id = ?
    ''', (
        data.get('alert_name'),
        data.get('direction'),
        data.get('condition_type'),
        data.get('condition_value'),
        data.get('condition_operator'),
        data.get('timeframe'),
        1 if data.get('candle_confirm') else 0,
        data.get('candle_pattern'),
        1 if data.get('auto_trade') else 0,
        data.get('stop_loss'),
        data.get('target_price'),
        data.get('quantity'),
        data.get('cooldown_minutes'),
        data.get('notes'),
        data.get('status', 'active'),
        alert_id
    ))
    db.commit()
    return jsonify({'success': True})


@api_v2.route('/alerts/<int:alert_id>', methods=['DELETE'])
def delete_alert(alert_id):
    """Delete an alert"""
    db = get_db()
    user_id = get_user_id()

    existing = db.execute('''
        SELECT id FROM stock_alerts WHERE id = ? AND user_id = ?
    ''', (alert_id, user_id)).fetchone()
    if not existing:
        return jsonify({'error': 'Alert not found'}), 404

    db.execute('DELETE FROM stock_alerts WHERE id = ?', (alert_id,))
    db.commit()
    return jsonify({'success': True})


@api_v2.route('/alerts/<int:alert_id>/toggle', methods=['POST'])
def toggle_alert(alert_id):
    """Toggle alert status between active and paused"""
    db = get_db()
    user_id = get_user_id()

    alert = db.execute('''
        SELECT id, status FROM stock_alerts WHERE id = ? AND user_id = ?
    ''', (alert_id, user_id)).fetchone()
    if not alert:
        return jsonify({'error': 'Alert not found'}), 404

    new_status = 'paused' if alert['status'] == 'active' else 'active'
    db.execute('''
        UPDATE stock_alerts SET status = ?, updated_at = GETDATE() WHERE id = ?
    ''', (new_status, alert_id))
    db.commit()
    return jsonify({'success': True, 'status': new_status})


@api_v2.route('/alerts/history', methods=['GET'])
def get_alert_history():
    """Get alert trigger history"""
    db = get_db()
    user_id = get_user_id()
    limit = int(request.args.get('limit', 50))

    rows = db.execute('''
        SELECT TOP (?) ah.*, sa.alert_name, sa.condition_type, sa.condition_value
        FROM alert_history ah
        LEFT JOIN stock_alerts sa ON ah.alert_id = sa.id
        WHERE ah.user_id = ?
        ORDER BY ah.trigger_time DESC
    ''', (limit, user_id)).fetchall()

    return jsonify({'success': True, 'history': [dict(r) for r in rows]})


# ══════════════════════════════════════════════════════════════════════════
# MARKET ENGINE ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════

@api_v2.route('/engine/status', methods=['GET'])
def engine_status():
    """Get market engine status and stats"""
    from services.market_engine import get_engine_status
    return jsonify({'success': True, **get_engine_status()})


@api_v2.route('/engine/start', methods=['POST'])
def engine_start():
    """Start the market engine"""
    from services.market_engine import start_engine
    from flask import current_app
    data = request.get_json() or {}
    cycle_seconds = data.get('cycle_seconds', 300)
    started = start_engine(current_app._get_current_object(), cycle_seconds)
    if started:
        return jsonify({'success': True, 'message': 'Engine started'})
    return jsonify({'success': False, 'message': 'Engine already running'})


@api_v2.route('/engine/stop', methods=['POST'])
def engine_stop():
    """Stop the market engine"""
    from services.market_engine import stop_engine
    stopped = stop_engine()
    if stopped:
        return jsonify({'success': True, 'message': 'Engine stopped'})
    return jsonify({'success': False, 'message': 'Engine not running'})


@api_v2.route('/engine/refresh', methods=['POST'])
def engine_refresh():
    """Trigger an immediate engine cycle"""
    from services.market_engine import trigger_manual_refresh
    from flask import current_app
    triggered = trigger_manual_refresh(current_app._get_current_object())
    if triggered:
        return jsonify({'success': True, 'message': 'Refresh triggered'})
    return jsonify({'success': False, 'message': 'Engine not running. Start it first.'})


@api_v2.route('/engine/notifications', methods=['GET'])
def engine_notifications():
    """Get pending (unacknowledged) notifications"""
    from services.market_engine import get_pending_notifications
    return jsonify({'success': True, 'notifications': get_pending_notifications()})


@api_v2.route('/engine/notifications/acknowledge', methods=['POST'])
def engine_acknowledge():
    """Acknowledge a notification or all notifications"""
    from services.market_engine import acknowledge_notification, acknowledge_all_notifications
    data = request.get_json() or {}
    nid = data.get('id')

    if nid:
        ok = acknowledge_notification(nid)
        return jsonify({'success': ok})
    else:
        acknowledge_all_notifications()
        return jsonify({'success': True})


# ══════════════════════════════════════════════════════════════════════════════
# DAY HIGH/LOW AUTO-FILL & GRADE AUTO-CALCULATE
# ══════════════════════════════════════════════════════════════════════════════

@api_v2.route('/ohlcv/day-range/<path:symbol>', methods=['GET'])
def get_day_range(symbol):
    """
    Get day's high and low for a symbol on a given date.
    Query: ?date=2025-01-15
    Returns: {high, low, open, close}
    """
    symbol = symbol.replace('NSE:', '').strip().upper()
    date_str = request.args.get('date', '')
    db = get_db()

    if not date_str:
        return jsonify({'error': 'date parameter required'}), 400

    # Try daily timeframe first
    row = db.execute('''
        SELECT TOP 1 high, low, [open], [close]
        FROM intraday_ohlcv
        WHERE symbol = ? AND timeframe = 'day'
          AND CAST(candle_time AS DATE) = ?
        ORDER BY candle_time DESC
    ''', (symbol, date_str)).fetchone()

    if row:
        return jsonify({
            'high': row['high'], 'low': row['low'],
            'open': row['open'], 'close': row['close']
        })

    # Fallback: aggregate from 15min candles for the day
    agg = db.execute('''
        SELECT MAX(high) AS high, MIN(low) AS low,
               (SELECT TOP 1 [open] FROM intraday_ohlcv
                WHERE symbol = ? AND timeframe = '15min'
                  AND CAST(candle_time AS DATE) = ?
                ORDER BY candle_time ASC) AS [open],
               (SELECT TOP 1 [close] FROM intraday_ohlcv
                WHERE symbol = ? AND timeframe = '15min'
                  AND CAST(candle_time AS DATE) = ?
                ORDER BY candle_time DESC) AS [close]
        FROM intraday_ohlcv
        WHERE symbol = ? AND timeframe = '15min'
          AND CAST(candle_time AS DATE) = ?
    ''', (symbol, date_str, symbol, date_str, symbol, date_str)).fetchone()

    if agg and agg['high']:
        return jsonify({
            'high': agg['high'], 'low': agg['low'],
            'open': agg['open'], 'close': agg['close']
        })

    return jsonify({'error': 'No OHLCV data for this date'}), 404


@api_v2.route('/auto-grade', methods=['POST'])
def auto_calculate_grade():
    """
    Auto-calculate entry/exit grade based on position in day's range.
    For Long: A = bottom 33%, B = middle 33%, C = top 33%
    For Short: A = top 33%, B = middle 33%, C = bottom 33%

    Body: {price, day_high, day_low, direction}
    """
    data = request.get_json()
    price = data.get('price', 0)
    day_high = data.get('day_high', 0)
    day_low = data.get('day_low', 0)
    direction = data.get('direction', 'Long')

    if not day_high or not day_low or day_high <= day_low or not price:
        return jsonify({'grade': 'B'})  # Default to B if data missing

    day_range = day_high - day_low
    position = (price - day_low) / day_range  # 0 = bottom, 1 = top

    if direction == 'Long':
        # For Long entries: lower is better
        if position <= 0.33:
            grade = 'A'
        elif position <= 0.66:
            grade = 'B'
        else:
            grade = 'C'
    else:
        # For Short entries: higher is better
        if position >= 0.66:
            grade = 'A'
        elif position >= 0.33:
            grade = 'B'
        else:
            grade = 'C'

    return jsonify({'grade': grade, 'position_pct': round(position * 100, 1)})


# ══════════════════════════════════════════════════════════════════════════════
# CSV IMPORT / EXPORT
# ══════════════════════════════════════════════════════════════════════════════

@api_v2.route('/export/<entity>', methods=['GET'])
def export_csv(entity):
    """Export data as CSV. entity = trade-bills|trade-journal|watchlist|alerts"""
    import csv
    from io import StringIO
    db = get_db()
    user_id = get_user_id()
    output = StringIO()
    writer = csv.writer(output)

    if entity == 'trade-bills':
        rows = db.execute('''
            SELECT ticker, current_market_price, entry_price, stop_loss, target_price,
                   quantity, atr, candle_pattern, max_risk, risk_per_share, position_size,
                   risk_percent, risk_amount_currency, reward_amount_currency, risk_reward_ratio,
                   break_even, trailing_stop, is_filled, stop_entered, target_entered,
                   journal_entered, comments, status, created_at
            FROM trade_bills WHERE user_id = ? ORDER BY created_at DESC
        ''', (user_id,)).fetchall()
        writer.writerow(['ticker', 'cmp', 'entry_price', 'stop_loss', 'target_price',
                         'quantity', 'atr', 'candle_pattern', 'max_risk', 'risk_per_share',
                         'position_size', 'risk_percent', 'risk_amount', 'reward_amount',
                         'rr_ratio', 'break_even', 'trailing_stop', 'is_filled',
                         'stop_entered', 'target_entered', 'journal_entered', 'comments',
                         'status', 'created_at'])
        for r in rows:
            writer.writerow([r[k] for k in r.keys()])

    elif entity == 'trade-journal':
        rows = db.execute('''
            SELECT ticker, direction, status, journal_date, entry_price, quantity,
                   stop_loss, target_price, strategy, trade_grade, avg_entry, avg_exit,
                   gain_loss_amount, gain_loss_percent, total_shares, remaining_qty,
                   entry_tactic, exit_tactic, mistake, open_trade_comments,
                   followup_analysis, tv_link_entry, tv_link_exit, tv_link_result
            FROM trade_journal_v2 WHERE user_id = ? ORDER BY journal_date DESC
        ''', (user_id,)).fetchall()
        headers = ['ticker', 'direction', 'status', 'journal_date', 'entry_price',
                   'quantity', 'stop_loss', 'target_price', 'strategy', 'trade_grade',
                   'avg_entry', 'avg_exit', 'gain_loss_amount', 'gain_loss_percent',
                   'total_shares', 'remaining_qty', 'entry_tactic', 'exit_tactic',
                   'mistake', 'open_trade_comments', 'followup_analysis',
                   'tv_link_entry', 'tv_link_exit', 'tv_link_result']
        writer.writerow(headers)
        for r in rows:
            writer.writerow([r.get(k, '') for k in headers])

    elif entity == 'watchlist':
        rows = db.execute('''
            SELECT symbol FROM trading_watchlist WHERE user_id = ?
        ''', (user_id,)).fetchall()
        writer.writerow(['symbol'])
        for r in rows:
            writer.writerow([r['symbol']])

    elif entity == 'alerts':
        rows = db.execute('''
            SELECT symbol, alert_type, trigger_value, direction, status, notes
            FROM stock_alerts WHERE user_id = ? ORDER BY created_at DESC
        ''', (user_id,)).fetchall()
        writer.writerow(['symbol', 'alert_type', 'trigger_value', 'direction', 'status', 'notes'])
        for r in rows:
            writer.writerow([r[k] for k in r.keys()])
    else:
        return jsonify({'error': f'Unknown entity: {entity}'}), 400

    csv_content = output.getvalue()
    from flask import Response
    return Response(
        csv_content,
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={entity}_{datetime.now().strftime("%Y%m%d")}.csv'}
    )


@api_v2.route('/import/<entity>', methods=['POST'])
def import_csv(entity):
    """Import data from CSV. entity = trade-bills|trade-journal|watchlist|alerts"""
    import csv
    from io import StringIO
    db = get_db()
    user_id = get_user_id()

    csv_data = request.get_data(as_text=True)
    if not csv_data:
        return jsonify({'error': 'No CSV data provided'}), 400

    reader = csv.DictReader(StringIO(csv_data))
    imported = 0

    if entity == 'trade-bills':
        for row in reader:
            try:
                db.execute('''
                    INSERT INTO trade_bills (user_id, ticker, current_market_price, entry_price,
                        stop_loss, target_price, quantity, comments, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, GETDATE())
                ''', (user_id, row.get('ticker', ''), _float(row.get('cmp')),
                      _float(row.get('entry_price')), _float(row.get('stop_loss')),
                      _float(row.get('target_price')), _int(row.get('quantity')),
                      row.get('comments', ''), row.get('status', 'active')))
                imported += 1
            except Exception as e:
                print(f"Import error row: {e}")
        db.commit()

    elif entity == 'trade-journal':
        for row in reader:
            try:
                db.execute('''
                    INSERT INTO trade_journal_v2 (user_id, ticker, direction, status,
                        journal_date, entry_price, quantity, stop_loss, target_price,
                        strategy, trade_grade, open_trade_comments, followup_analysis)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (user_id, row.get('ticker', ''), row.get('direction', 'Long'),
                      row.get('status', 'open'), row.get('journal_date'),
                      _float(row.get('entry_price')), _int(row.get('quantity')),
                      _float(row.get('stop_loss')), _float(row.get('target_price')),
                      row.get('strategy', ''), row.get('trade_grade', ''),
                      row.get('open_trade_comments', ''), row.get('followup_analysis', '')))
                imported += 1
            except Exception as e:
                print(f"Import error row: {e}")
        db.commit()

    elif entity == 'watchlist':
        for row in reader:
            symbol = (row.get('symbol') or '').replace('NSE:', '').strip().upper()
            if symbol:
                try:
                    existing = db.execute(
                        'SELECT id FROM trading_watchlist WHERE user_id = ? AND symbol = ?',
                        (user_id, symbol)).fetchone()
                    if not existing:
                        db.execute(
                            'INSERT INTO trading_watchlist (user_id, symbol) VALUES (?, ?)',
                            (user_id, symbol))
                        imported += 1
                except Exception as e:
                    print(f"Import error: {e}")
        db.commit()

    elif entity == 'alerts':
        for row in reader:
            symbol = (row.get('symbol') or '').replace('NSE:', '').strip().upper()
            if symbol:
                try:
                    db.execute('''
                        INSERT INTO stock_alerts (user_id, symbol, alert_type, trigger_value,
                            direction, status, notes, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, GETDATE())
                    ''', (user_id, symbol, row.get('alert_type', 'price_above'),
                          _float(row.get('trigger_value')), row.get('direction', 'above'),
                          row.get('status', 'active'), row.get('notes', '')))
                    imported += 1
                except Exception as e:
                    print(f"Import error: {e}")
        db.commit()
    else:
        return jsonify({'error': f'Unknown entity: {entity}'}), 400

    return jsonify({'success': True, 'imported': imported})


def _float(v):
    """Safely parse float"""
    try:
        return float(v) if v else None
    except (ValueError, TypeError):
        return None


def _int(v):
    """Safely parse int"""
    try:
        return int(float(v)) if v else None
    except (ValueError, TypeError):
        return None
