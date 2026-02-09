"""
Elder Trading System - Enhanced API Routes v2
New endpoints for connected workflow: Screener → Trade Bill → Kite Connect → Trade Log → Position Management

Data Source: Kite Connect API (NSE)
Symbol Format: NSE:SYMBOL (e.g., NSE:RELIANCE, NSE:TCS)
"""

from flask import Blueprint, request, jsonify, g
from datetime import datetime, timedelta
import json

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
    return getattr(g, 'user_id', 1)


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
    Bulk load OHLCV data and pre-calculate all indicators for NIFTY 100.
    Called manually by user from Settings page after market close.

    This is the ONLY place where Kite API is called for historical data.
    All other features read from the database cache.
    """
    from services.screener_v2 import NIFTY_100, calculate_all_indicators, analyze_weekly_trend
    from services.kite_client import get_client, clear_session_cache

    client = get_client()
    if not client.check_auth():
        return jsonify({
            'success': False,
            'error': 'Not authenticated with Kite Connect. Please login first.'
        }), 401

    db = get_db()
    user_id = get_user_id()

    # Check last refresh date
    sync_info = db.execute('''
        SELECT MAX(last_updated) as last_refresh
        FROM stock_data_sync
    ''').fetchone()

    last_refresh = sync_info['last_refresh'] if sync_info else None
    today = datetime.now().strftime('%Y-%m-%d')

    # Check if already loaded today
    if last_refresh:
        last_date = last_refresh[:10]  # Extract date part
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

    symbols_loaded = 0
    symbols_failed = []
    total = len(NIFTY_100)

    for i, symbol in enumerate(NIFTY_100):
        try:
            exchange, tradingsymbol = client.parse_symbol(symbol)
            full_symbol = f"{exchange}:{tradingsymbol}"

            # Fetch 2 years of historical data
            hist = client.get_historical_data(full_symbol, interval='day', days=730)

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
                    float(row['Open']), float(row['High']), float(row['Low']), float(row['Close']), int(row['Volume']),
                    full_symbol, date_str,
                    float(row['Open']), float(row['High']), float(row['Low']), float(row['Close']), int(row['Volume'])
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


def _is_data_stale(last_refresh: str) -> bool:
    """Check if data is stale (not refreshed today)"""
    if not last_refresh:
        return True
    try:
        last_date = last_refresh[:10]
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
    kc_upper_3 = round(kc_middle + 3 * atr, 2) if atr else ohlcv['close'] * 1.03
    kc_lower_3 = round(kc_middle - 3 * atr, 2) if atr else ohlcv['close'] * 0.97

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

    db.execute('''
        INSERT INTO weekly_scans 
        (user_id, market, scan_date, week_start, week_end, results, summary)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (
        user_id, market, today,
        today - timedelta(days=today.weekday()),
        today + timedelta(days=6-today.weekday()),
        json.dumps(results['all_results']),
        json.dumps(results['summary'])
    ))
    db.commit()

    scan_id = db.execute('SELECT SCOPE_IDENTITY() AS id').fetchone()['id']
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

    quantity = int(data.get('quantity', 0))
    if quantity <= 0:
        return jsonify({'success': False, 'error': 'Quantity must be a positive integer'}), 400

    result = place_gtt_order(
        symbol=data['symbol'],
        transaction_type=data.get('transaction_type', TRANSACTION_BUY),
        quantity=quantity,
        trigger_price=data['trigger_price'],
        limit_price=data['limit_price']
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
    db.execute('''
        INSERT INTO trade_bills (
            user_id, symbol, market, direction, entry_price, stop_loss,
            target_price, quantity, position_value, risk_amount,
            risk_reward_ratio, signal_strength, grade, notes, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        user_id, symbol, 'US', 'LONG',
        entry, stop, screener_data['target'],
        quantity, position_value, risk_per_trade,
        screener_data['risk_reward_ratio'],
        screener_data['signal_strength'],
        screener_data['grade'],
        f"Created from screener. High-value signals: {', '.join(screener_data.get('high_value_signals', []))}",
        'PENDING'
    ))
    db.commit()

    bill_id = db.execute('SELECT SCOPE_IDENTITY() AS id').fetchone()['id']

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

    return jsonify(result)


@api_v2.route('/trade-journal', methods=['POST'])
def create_trade_journal():
    """Create a new trade journal entry"""
    db = get_db()
    user_id = get_user_id()
    data = request.get_json()

    cursor = db.execute('''
        INSERT INTO trade_journal_v2 (
            user_id, trade_bill_id, ticker, cmp, direction, status, journal_date,
            remaining_qty, order_type, mental_state, entry_price, quantity,
            target_price, stop_loss, rr_ratio, potential_loss, trailing_stop,
            new_target, potential_gain, target_a, target_b, target_c,
            entry_tactic, entry_reason, exit_tactic, exit_reason,
            open_trade_comments, followup_analysis
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        data.get('followup_analysis')
    ))
    db.commit()

    journal_id = int(db.execute('SELECT SCOPE_IDENTITY() AS id').fetchone()['id'])
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

    db.execute('''
        UPDATE trade_journal_v2 SET
            ticker = ?, cmp = ?, direction = ?, status = ?, journal_date = ?,
            remaining_qty = ?, order_type = ?, mental_state = ?,
            entry_price = ?, quantity = ?, target_price = ?, stop_loss = ?,
            rr_ratio = ?, potential_loss = ?, trailing_stop = ?, new_target = ?,
            potential_gain = ?, target_a = ?, target_b = ?, target_c = ?,
            entry_tactic = ?, entry_reason = ?, exit_tactic = ?, exit_reason = ?,
            first_entry_date = ?, last_exit_date = ?, total_shares = ?,
            avg_entry = ?, avg_exit = ?, trade_grade = ?,
            gain_loss_percent = ?, gain_loss_amount = ?,
            high_during_trade = ?, low_during_trade = ?,
            max_drawdown = ?, percent_captured = ?,
            open_trade_comments = ?, followup_analysis = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
    ''', (
        data.get('ticker'),
        data.get('cmp'),
        data.get('direction'),
        data.get('status'),
        data.get('journal_date'),
        data.get('remaining_qty'),
        data.get('order_type'),
        data.get('mental_state'),
        data.get('entry_price'),
        data.get('quantity'),
        data.get('target_price'),
        data.get('stop_loss'),
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
        data.get('first_entry_date'),
        data.get('last_exit_date'),
        data.get('total_shares'),
        data.get('avg_entry'),
        data.get('avg_exit'),
        data.get('trade_grade'),
        data.get('gain_loss_percent'),
        data.get('gain_loss_amount'),
        data.get('high_during_trade'),
        data.get('low_during_trade'),
        data.get('max_drawdown'),
        data.get('percent_captured'),
        data.get('open_trade_comments'),
        data.get('followup_analysis'),
        journal_id
    ))
    db.commit()

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

    cursor = db.execute('''
        INSERT INTO trade_entries (
            journal_id, entry_datetime, quantity, order_price, filled_price,
            slippage, commission, position_size, day_high, day_low, grade, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    ))
    db.commit()

    entry_id = int(db.execute('SELECT SCOPE_IDENTITY() AS id').fetchone()['id'])

    # Update journal totals
    _recalculate_journal_totals(db, journal_id)

    return jsonify({'success': True, 'id': entry_id}), 201


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

    cursor = db.execute('''
        INSERT INTO trade_exits (
            journal_id, exit_datetime, quantity, order_price, filled_price,
            slippage, commission, position_size, day_high, day_low, grade, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    ))
    db.commit()

    exit_id = int(db.execute('SELECT SCOPE_IDENTITY() AS id').fetchone()['id'])

    # Update journal totals
    _recalculate_journal_totals(db, journal_id)

    return jsonify({'success': True, 'id': exit_id}), 201


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
    _recalculate_journal_totals(db, journal_id)

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
    _recalculate_journal_totals(db, journal_id)

    return jsonify({'success': True})


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
    cursor = db.execute('''
        INSERT INTO trade_journal_v2 (
            user_id, trade_bill_id, ticker, cmp, direction, status, journal_date,
            entry_price, quantity, target_price, stop_loss, rr_ratio,
            potential_loss, potential_gain, target_a, target_b, target_c
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    ))
    db.commit()

    journal_id = int(db.execute('SELECT SCOPE_IDENTITY() AS id').fetchone()['id'])

    # Update trade bill to mark journal entered
    db.execute('''
        UPDATE trade_bills SET journal_entered = 1, updated_at = GETDATE()
        WHERE id = ?
    ''', (bill_id,))
    db.commit()

    return jsonify({'success': True, 'id': journal_id}), 201


def _recalculate_journal_totals(db, journal_id):
    """Recalculate journal aggregate fields from entries/exits"""
    # Get all entries
    entries = db.execute('''
        SELECT * FROM trade_entries WHERE journal_id = ? ORDER BY entry_datetime
    ''', (journal_id,)).fetchall()

    # Get all exits
    exits = db.execute('''
        SELECT * FROM trade_exits WHERE journal_id = ? ORDER BY exit_datetime
    ''', (journal_id,)).fetchall()

    # Calculate totals
    total_entry_shares = sum(e['quantity'] or 0 for e in entries)
    total_exit_shares = sum(e['quantity'] or 0 for e in exits)
    remaining_qty = total_entry_shares - total_exit_shares

    # Weighted average entry
    total_entry_value = sum((e['filled_price'] or e['order_price'] or 0) * (e['quantity'] or 0) for e in entries)
    avg_entry = total_entry_value / total_entry_shares if total_entry_shares > 0 else 0

    # Weighted average exit
    total_exit_value = sum((e['filled_price'] or e['order_price'] or 0) * (e['quantity'] or 0) for e in exits)
    avg_exit = total_exit_value / total_exit_shares if total_exit_shares > 0 else 0

    # First entry and last exit dates
    first_entry = entries[0]['entry_datetime'] if entries else None
    last_exit = exits[-1]['exit_datetime'] if exits else None

    # High/Low during trade
    all_highs = [e['day_high'] for e in entries if e['day_high']] + [e['day_high'] for e in exits if e['day_high']]
    all_lows = [e['day_low'] for e in entries if e['day_low']] + [e['day_low'] for e in exits if e['day_low']]
    high_during = max(all_highs) if all_highs else None
    low_during = min(all_lows) if all_lows else None

    # Gain/Loss calculation (for closed portion)
    gain_loss_amount = 0
    if total_exit_shares > 0 and avg_exit > 0 and avg_entry > 0:
        gain_loss_amount = (avg_exit - avg_entry) * total_exit_shares
        # Subtract commissions
        total_commission = sum((e['commission'] or 0) for e in entries) + sum((e['commission'] or 0) for e in exits)
        gain_loss_amount -= total_commission

    gain_loss_percent = (gain_loss_amount / (avg_entry * total_exit_shares) * 100) if (avg_entry and total_exit_shares) else 0

    # Status
    status = 'closed' if remaining_qty == 0 and total_entry_shares > 0 else 'open'

    # Update journal
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
