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
    market = data.get('market', 'US')
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

    scan_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]
    results['scan_id'] = scan_id

    return jsonify(results)


@api_v2.route('/screener/stock/<symbol>', methods=['GET'])
def analyze_stock_v2(symbol):
    """Analyze a single stock with v2 logic"""
    result = scan_stock_v2(symbol)
    if result:
        return jsonify(result)
    return jsonify({'error': f'Could not analyze {symbol}'}), 404


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
        "symbol": "AAPL",
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

    bill_id = db.execute('SELECT last_insert_rowid()').fetchone()[0]

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
# AUTO-SYNC TRADE LOG FROM IBKR
# ══════════════════════════════════════════════════════════════════════════════

@api_v2.route('/trade-log/sync-ibkr', methods=['POST'])
def sync_trade_log_from_ibkr():
    """
    Sync trade log with filled orders from IBKR

    This pulls executed trades from IBKR and creates/updates trade log entries
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
        side = 'Long' if trade['side'] == 'BOT' else 'Short'
        status = 'open' if trade['side'] == 'BOT' else 'closed'

        db.execute('''
            INSERT INTO trade_log (
                user_id, entry_date, symbol, strategy, direction,
                entry_price, shares, trade_costs, status, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            user_id, trade['execution_time'], trade['symbol'],
            'EL - Elder System', side, trade['price'],
            trade['quantity'], trade['commission'], status,
            f"Auto-synced from IBKR. Order ref: {trade['order_ref']}"
        ))
        synced += 1

    db.commit()

    return jsonify({
        'success': True,
        'synced': synced,
        'skipped': skipped,
        'total_ibkr_trades': len(result['trades']),
        'message': f'Synced {synced} new trades from IBKR ({skipped} already existed)'
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
        SELECT * FROM weekly_scans 
        WHERE user_id = ? 
        ORDER BY scan_date DESC LIMIT 1
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

    # Get open orders from IBKR
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
        'ibkr_connected': positions['success'],
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
        "symbol": "AAPL",
        "market": "US",
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
        market=data.get('market', 'US'),
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
        "symbols": ["AAPL", "MSFT", "GOOGL"],
        "market": "US",
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
        market=data.get('market', 'US'),
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
        market='US',
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
    market = request.args.get('market', 'US')
    
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
        "symbols": ["AAPL", "MSFT", ...],  // or "all" for all stocks
        "lookback_days": 180,
        "min_score": 5,
        "market": "US"
    }
    
    Returns signals sorted by date (newest first)
    """
    data = request.get_json() or {}
    
    symbols = data.get('symbols', [])
    lookback_days = data.get('lookback_days', 180)
    min_score = data.get('min_score', 5)
    market = data.get('market', 'US')
    
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


