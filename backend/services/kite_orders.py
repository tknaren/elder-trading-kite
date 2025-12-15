"""
Elder Trading System - Kite Connect Order & Position Management
Handles order placement, GTT orders, position tracking, and P/L management

WORKFLOW:
1. Screener identifies A-grade setups
2. Trade Bill creates order parameters
3. This module places orders to Kite Connect
4. Trade Log tracks filled orders
5. Position Management tracks all open positions with live P/L

Order Types Supported:
- Regular Orders (LIMIT, MARKET, SL, SL-M)
- GTT (Good Till Triggered) - Single trigger
- GTT-OCO (One Cancels Other) - Stop Loss + Target

NSE Trading Hours: 9:15 AM - 3:30 PM IST
"""

from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from services.kite_client import get_client, is_nse_market_open, IST

# Kite Connect Order Constants
EXCHANGE_NSE = 'NSE'
EXCHANGE_BSE = 'BSE'

TRANSACTION_BUY = 'BUY'
TRANSACTION_SELL = 'SELL'

ORDER_TYPE_MARKET = 'MARKET'
ORDER_TYPE_LIMIT = 'LIMIT'
ORDER_TYPE_SL = 'SL'  # Stop Loss (with limit price)
ORDER_TYPE_SLM = 'SL-M'  # Stop Loss Market

PRODUCT_CNC = 'CNC'  # Cash and Carry (delivery)
PRODUCT_MIS = 'MIS'  # Intraday
PRODUCT_NRML = 'NRML'  # Normal (F&O)

VALIDITY_DAY = 'DAY'
VALIDITY_IOC = 'IOC'  # Immediate or Cancel
VALIDITY_TTL = 'TTL'  # Time to Live

# GTT Order Types
GTT_TYPE_SINGLE = 'single'
GTT_TYPE_OCO = 'two-leg'  # One Cancels Other


def check_kite_connection() -> tuple:
    """Check if Kite Connect is connected and authenticated"""
    client = get_client()

    if not client.api_key:
        return False, "Kite Connect not configured. Add API Key in Settings."

    if not client.access_token:
        return False, "Not logged in. Please login to Kite Connect."

    try:
        if client.check_auth():
            profile = client.get_profile()
            if profile:
                return True, f"Connected as {profile.get('user_name', 'User')}"
            return True, "Connected to Kite Connect"
        return False, "Session expired. Please login again."
    except Exception as e:
        return False, f"Connection error: {str(e)}"


def check_trading_hours() -> tuple:
    """Check if within NSE trading hours"""
    is_open, message = is_nse_market_open()
    return is_open, message


def get_account_info() -> Dict:
    """Get Kite account information"""
    client = get_client()

    if not client.check_auth():
        return {'success': False, 'error': 'Not authenticated'}

    try:
        profile = client.kite.profile()
        margins = client.kite.margins()

        equity_margin = margins.get('equity', {})

        return {
            'success': True,
            'user_id': profile.get('user_id'),
            'user_name': profile.get('user_name'),
            'email': profile.get('email'),
            'broker': profile.get('broker', 'ZERODHA'),
            'net_liquidation': equity_margin.get('net', 0),
            'available_cash': equity_margin.get('available', {}).get('cash', 0),
            'used_margin': equity_margin.get('utilised', {}).get('debits', 0),
            'available_margin': equity_margin.get('available', {}).get('live_balance', 0),
            'currency': 'INR'
        }
    except Exception as e:
        return {'success': False, 'error': str(e)}


def place_order(
    symbol: str,
    transaction_type: str,
    quantity: int,
    price: float = None,
    order_type: str = ORDER_TYPE_LIMIT,
    product: str = PRODUCT_CNC,
    trigger_price: float = None,
    validity: str = VALIDITY_DAY,
    tag: str = None
) -> Dict:
    """
    Place a regular order

    Args:
        symbol: Trading symbol (e.g., 'RELIANCE', 'TCS')
        transaction_type: 'BUY' or 'SELL'
        quantity: Number of shares (whole numbers only for NSE)
        price: Limit price (optional for MARKET orders)
        order_type: 'MARKET', 'LIMIT', 'SL', 'SL-M'
        product: 'CNC' (delivery), 'MIS' (intraday)
        trigger_price: Trigger price for SL orders
        validity: 'DAY' or 'IOC'
        tag: Optional order tag for tracking

    Returns:
        Order result with order_id
    """
    # Validate trading hours
    is_open, message = check_trading_hours()
    if not is_open:
        return {'success': False, 'error': f'Market closed: {message}'}

    client = get_client()
    if not client.check_auth():
        return {'success': False, 'error': 'Not authenticated with Kite Connect'}

    # Ensure whole shares (no fractional)
    quantity = int(quantity)
    if quantity <= 0:
        return {'success': False, 'error': 'Quantity must be a positive integer'}

    try:
        order_params = {
            'tradingsymbol': symbol.upper(),
            'exchange': EXCHANGE_NSE,
            'transaction_type': transaction_type,
            'quantity': quantity,
            'order_type': order_type,
            'product': product,
            'validity': validity
        }

        if order_type in [ORDER_TYPE_LIMIT, ORDER_TYPE_SL]:
            if price is None:
                return {'success': False, 'error': 'Price required for LIMIT/SL orders'}
            order_params['price'] = round(price, 2)

        if order_type in [ORDER_TYPE_SL, ORDER_TYPE_SLM]:
            if trigger_price is None:
                return {'success': False, 'error': 'Trigger price required for SL orders'}
            order_params['trigger_price'] = round(trigger_price, 2)

        if tag:
            order_params['tag'] = tag[:20]  # Max 20 chars

        order_id = client.kite.place_order(variety='regular', **order_params)

        return {
            'success': True,
            'order_id': order_id,
            'message': f'{transaction_type} order placed for {quantity} shares of {symbol}',
            'symbol': symbol,
            'quantity': quantity,
            'price': price,
            'order_type': order_type
        }

    except Exception as e:
        return {'success': False, 'error': str(e)}


def place_gtt_order(
    symbol: str,
    transaction_type: str,
    quantity: int,
    trigger_price: float,
    limit_price: float,
    product: str = PRODUCT_CNC
) -> Dict:
    """
    Place a GTT (Good Till Triggered) single-leg order

    GTT orders are valid for 1 year and trigger when price crosses trigger_price.
    Used for: Waiting for price to reach entry level

    Args:
        symbol: Trading symbol
        transaction_type: 'BUY' or 'SELL'
        quantity: Number of shares
        trigger_price: Price at which order triggers
        limit_price: Limit price for the order
        product: 'CNC' for delivery

    Returns:
        GTT order result with trigger_id
    """
    client = get_client()
    if not client.check_auth():
        return {'success': False, 'error': 'Not authenticated with Kite Connect'}

    quantity = int(quantity)
    if quantity <= 0:
        return {'success': False, 'error': 'Quantity must be a positive integer'}

    try:
        # Get current LTP for comparison
        ltp_data = client.get_ltp([f'NSE:{symbol}'])
        current_price = ltp_data.get(f'NSE:{symbol}', {}).get('last_price', 0)

        # Determine trigger type based on current price
        # For BUY: trigger when price goes DOWN to trigger_price (LTP >= trigger)
        # For SELL: trigger when price goes UP to trigger_price (LTP <= trigger)
        if transaction_type == TRANSACTION_BUY:
            trigger_type = 'single'
            condition = 'ltp_below' if trigger_price < current_price else 'ltp_above'
        else:
            trigger_type = 'single'
            condition = 'ltp_above' if trigger_price > current_price else 'ltp_below'

        trigger_id = client.kite.place_gtt(
            trigger_type=GTT_TYPE_SINGLE,
            tradingsymbol=symbol.upper(),
            exchange=EXCHANGE_NSE,
            trigger_values=[round(trigger_price, 2)],
            last_price=current_price,
            orders=[{
                'transaction_type': transaction_type,
                'quantity': quantity,
                'price': round(limit_price, 2),
                'order_type': ORDER_TYPE_LIMIT,
                'product': product
            }]
        )

        return {
            'success': True,
            'trigger_id': trigger_id,
            'message': f'GTT order placed: {transaction_type} {quantity} {symbol} when price reaches ₹{trigger_price}',
            'symbol': symbol,
            'trigger_price': trigger_price,
            'limit_price': limit_price,
            'quantity': quantity,
            'valid_until': (datetime.now() + timedelta(days=365)).strftime('%Y-%m-%d')
        }

    except Exception as e:
        return {'success': False, 'error': str(e)}


def place_gtt_oco(
    symbol: str,
    quantity: int,
    stop_loss_trigger: float,
    stop_loss_price: float,
    target_trigger: float,
    target_price: float,
    product: str = PRODUCT_CNC
) -> Dict:
    """
    Place a GTT-OCO (One Cancels Other) order for Stop Loss + Target

    This is the primary bracket strategy for NSE.
    Creates two triggers: if one executes, the other is cancelled.

    Use Case:
    - After buying stock, place GTT-OCO to manage the position
    - Stop Loss: Sell if price drops to stop_loss_trigger
    - Target: Sell if price rises to target_trigger

    Args:
        symbol: Trading symbol
        quantity: Number of shares to sell (should match position)
        stop_loss_trigger: Price at which SL order triggers
        stop_loss_price: Limit price for SL sell order
        target_trigger: Price at which target order triggers
        target_price: Limit price for target sell order
        product: 'CNC' for delivery

    Returns:
        GTT-OCO result with trigger_id
    """
    client = get_client()
    if not client.check_auth():
        return {'success': False, 'error': 'Not authenticated with Kite Connect'}

    quantity = int(quantity)
    if quantity <= 0:
        return {'success': False, 'error': 'Quantity must be a positive integer'}

    if stop_loss_trigger >= target_trigger:
        return {'success': False, 'error': 'Stop loss must be below target price'}

    try:
        # Get current LTP
        ltp_data = client.get_ltp([f'NSE:{symbol}'])
        current_price = ltp_data.get(f'NSE:{symbol}', {}).get('last_price', 0)

        if current_price == 0:
            return {'success': False, 'error': f'Could not get current price for {symbol}'}

        # OCO order: two SELL orders
        # Trigger 1: Stop Loss (triggers when price drops)
        # Trigger 2: Target (triggers when price rises)
        trigger_id = client.kite.place_gtt(
            trigger_type=GTT_TYPE_OCO,
            tradingsymbol=symbol.upper(),
            exchange=EXCHANGE_NSE,
            trigger_values=[round(stop_loss_trigger, 2), round(target_trigger, 2)],
            last_price=current_price,
            orders=[
                {
                    # Stop Loss Order (triggers on lower price)
                    'transaction_type': TRANSACTION_SELL,
                    'quantity': quantity,
                    'price': round(stop_loss_price, 2),
                    'order_type': ORDER_TYPE_LIMIT,
                    'product': product
                },
                {
                    # Target Order (triggers on higher price)
                    'transaction_type': TRANSACTION_SELL,
                    'quantity': quantity,
                    'price': round(target_price, 2),
                    'order_type': ORDER_TYPE_LIMIT,
                    'product': product
                }
            ]
        )

        return {
            'success': True,
            'trigger_id': trigger_id,
            'message': f'GTT-OCO placed for {symbol}: SL at ₹{stop_loss_trigger}, Target at ₹{target_trigger}',
            'symbol': symbol,
            'quantity': quantity,
            'stop_loss_trigger': stop_loss_trigger,
            'stop_loss_price': stop_loss_price,
            'target_trigger': target_trigger,
            'target_price': target_price,
            'valid_until': (datetime.now() + timedelta(days=365)).strftime('%Y-%m-%d')
        }

    except Exception as e:
        return {'success': False, 'error': str(e)}


def get_gtt_orders() -> Dict:
    """Get all GTT orders"""
    client = get_client()
    if not client.check_auth():
        return {'success': False, 'error': 'Not authenticated', 'gtts': []}

    try:
        gtts = client.kite.get_gtts()

        formatted = []
        for gtt in gtts:
            formatted.append({
                'trigger_id': gtt.get('id'),
                'symbol': gtt.get('tradingsymbol'),
                'exchange': gtt.get('exchange'),
                'trigger_type': gtt.get('trigger_type'),  # single or two-leg
                'trigger_values': gtt.get('condition', {}).get('trigger_values', []),
                'status': gtt.get('status'),
                'created_at': gtt.get('created_at'),
                'updated_at': gtt.get('updated_at'),
                'expires_at': gtt.get('expires_at'),
                'orders': gtt.get('orders', [])
            })

        return {
            'success': True,
            'gtts': formatted,
            'count': len(formatted)
        }

    except Exception as e:
        return {'success': False, 'error': str(e), 'gtts': []}


def cancel_gtt(trigger_id: int) -> Dict:
    """Cancel a GTT order"""
    client = get_client()
    if not client.check_auth():
        return {'success': False, 'error': 'Not authenticated'}

    try:
        client.kite.delete_gtt(trigger_id)
        return {
            'success': True,
            'message': f'GTT order {trigger_id} cancelled'
        }
    except Exception as e:
        return {'success': False, 'error': str(e)}


def get_open_orders() -> Dict:
    """Get all open/pending orders"""
    client = get_client()
    if not client.check_auth():
        return {'success': False, 'error': 'Not authenticated', 'orders': []}

    try:
        orders = client.kite.orders()

        # Filter for open orders
        open_orders = [o for o in orders if o.get('status') in
                      ['OPEN', 'PENDING', 'TRIGGER PENDING', 'AMO REQ RECEIVED']]

        formatted = []
        for o in open_orders:
            formatted.append({
                'order_id': o.get('order_id'),
                'symbol': o.get('tradingsymbol'),
                'exchange': o.get('exchange'),
                'transaction_type': o.get('transaction_type'),
                'quantity': o.get('quantity'),
                'price': o.get('price'),
                'trigger_price': o.get('trigger_price'),
                'order_type': o.get('order_type'),
                'product': o.get('product'),
                'status': o.get('status'),
                'filled_quantity': o.get('filled_quantity', 0),
                'pending_quantity': o.get('pending_quantity', 0),
                'order_timestamp': o.get('order_timestamp'),
                'tag': o.get('tag')
            })

        return {
            'success': True,
            'orders': formatted,
            'count': len(formatted)
        }

    except Exception as e:
        return {'success': False, 'error': str(e), 'orders': []}


def cancel_order(order_id: str, variety: str = 'regular') -> Dict:
    """Cancel an open order"""
    client = get_client()
    if not client.check_auth():
        return {'success': False, 'error': 'Not authenticated'}

    try:
        client.kite.cancel_order(variety=variety, order_id=order_id)
        return {
            'success': True,
            'message': f'Order {order_id} cancelled'
        }
    except Exception as e:
        return {'success': False, 'error': str(e)}


def modify_order(
    order_id: str,
    quantity: int = None,
    price: float = None,
    trigger_price: float = None,
    order_type: str = None,
    variety: str = 'regular'
) -> Dict:
    """Modify an open order"""
    client = get_client()
    if not client.check_auth():
        return {'success': False, 'error': 'Not authenticated'}

    try:
        params = {}
        if quantity is not None:
            params['quantity'] = int(quantity)
        if price is not None:
            params['price'] = round(price, 2)
        if trigger_price is not None:
            params['trigger_price'] = round(trigger_price, 2)
        if order_type is not None:
            params['order_type'] = order_type

        if not params:
            return {'success': False, 'error': 'No modifications specified'}

        client.kite.modify_order(variety=variety, order_id=order_id, **params)
        return {
            'success': True,
            'message': f'Order {order_id} modified'
        }
    except Exception as e:
        return {'success': False, 'error': str(e)}


def get_positions() -> Dict:
    """
    Get all open positions from Kite
    Returns positions with current market value and P/L
    """
    client = get_client()
    if not client.check_auth():
        return {'success': False, 'error': 'Not authenticated', 'positions': []}

    try:
        positions = client.kite.positions()

        # Combine day and net positions
        all_positions = positions.get('net', [])

        formatted = []
        total_unrealized_pnl = 0
        total_market_value = 0

        for pos in all_positions:
            if pos.get('quantity', 0) != 0:  # Only show non-zero positions
                quantity = pos.get('quantity', 0)
                avg_price = pos.get('average_price', 0)
                last_price = pos.get('last_price', 0)
                pnl = pos.get('pnl', 0)
                market_value = quantity * last_price

                formatted.append({
                    'symbol': pos.get('tradingsymbol'),
                    'exchange': pos.get('exchange'),
                    'quantity': quantity,
                    'avg_price': avg_price,
                    'last_price': last_price,
                    'market_value': market_value,
                    'unrealized_pnl': pnl,
                    'pnl_percent': round((pnl / (avg_price * abs(quantity))) * 100, 2) if avg_price > 0 and quantity != 0 else 0,
                    'product': pos.get('product'),
                    'day_change': pos.get('day_change', 0),
                    'day_change_percent': pos.get('day_change_percentage', 0),
                    'currency': 'INR'
                })

                total_unrealized_pnl += pnl
                total_market_value += market_value

        return {
            'success': True,
            'positions': formatted,
            'count': len(formatted),
            'total_unrealized_pnl': round(total_unrealized_pnl, 2),
            'total_market_value': round(total_market_value, 2)
        }

    except Exception as e:
        return {'success': False, 'error': str(e), 'positions': []}


def get_holdings() -> Dict:
    """Get all holdings (delivery positions)"""
    client = get_client()
    if not client.check_auth():
        return {'success': False, 'error': 'Not authenticated', 'holdings': []}

    try:
        holdings = client.kite.holdings()

        formatted = []
        total_investment = 0
        total_current_value = 0
        total_pnl = 0

        for h in holdings:
            quantity = h.get('quantity', 0)
            avg_price = h.get('average_price', 0)
            last_price = h.get('last_price', 0)
            investment = quantity * avg_price
            current_value = quantity * last_price
            pnl = current_value - investment

            formatted.append({
                'symbol': h.get('tradingsymbol'),
                'exchange': h.get('exchange'),
                'isin': h.get('isin'),
                'quantity': quantity,
                'avg_price': avg_price,
                'last_price': last_price,
                'investment': investment,
                'current_value': current_value,
                'pnl': pnl,
                'pnl_percent': round((pnl / investment) * 100, 2) if investment > 0 else 0,
                'day_change': h.get('day_change', 0),
                'day_change_percent': h.get('day_change_percentage', 0),
                'currency': 'INR'
            })

            total_investment += investment
            total_current_value += current_value
            total_pnl += pnl

        return {
            'success': True,
            'holdings': formatted,
            'count': len(formatted),
            'total_investment': round(total_investment, 2),
            'total_current_value': round(total_current_value, 2),
            'total_pnl': round(total_pnl, 2),
            'total_pnl_percent': round((total_pnl / total_investment) * 100, 2) if total_investment > 0 else 0
        }

    except Exception as e:
        return {'success': False, 'error': str(e), 'holdings': []}


def get_position_alerts(positions: List[Dict], trade_bills: List[Dict]) -> List[Dict]:
    """
    Generate alerts for positions that need attention

    Alerts:
    - Price approaching stop loss
    - Price above target (consider taking profit)
    - Position going against (losing more than expected)
    - Time-based (holding too long without progress)
    """
    alerts = []

    for pos in positions:
        symbol = pos.get('symbol')
        current_price = pos.get('last_price', 0)
        avg_price = pos.get('avg_price', 0)
        pnl_percent = pos.get('pnl_percent', 0)
        unrealized_pnl = pos.get('unrealized_pnl', 0)

        # Find matching trade bill for stop/target
        matching_bill = None
        for bill in trade_bills:
            bill_symbol = bill.get('ticker', '').replace('NSE:', '')
            if bill_symbol == symbol:
                matching_bill = bill
                break

        if matching_bill:
            stop_loss = matching_bill.get('stop_loss', 0)
            target = matching_bill.get('target_price', 0)

            # Alert: Near stop loss
            if stop_loss > 0:
                distance_to_stop = ((current_price - stop_loss) / current_price) * 100
                if distance_to_stop < 2:  # Within 2% of stop
                    alerts.append({
                        'symbol': symbol,
                        'type': 'STOP_APPROACHING',
                        'severity': 'HIGH',
                        'message': f'{symbol}: Price ₹{current_price:.2f} is {distance_to_stop:.1f}% from stop ₹{stop_loss:.2f}',
                        'action': 'Consider closing position or tightening stop',
                        'current_price': current_price,
                        'stop_loss': stop_loss
                    })

            # Alert: Above target
            if target > 0 and current_price >= target:
                alerts.append({
                    'symbol': symbol,
                    'type': 'TARGET_REACHED',
                    'severity': 'MEDIUM',
                    'message': f'{symbol}: Price ₹{current_price:.2f} reached target ₹{target:.2f}',
                    'action': 'Consider taking profits',
                    'current_price': current_price,
                    'target': target
                })

        # Alert: Significant loss
        if pnl_percent < -5:
            alerts.append({
                'symbol': symbol,
                'type': 'SIGNIFICANT_LOSS',
                'severity': 'HIGH',
                'message': f'{symbol}: Position down {abs(pnl_percent):.1f}% (₹{unrealized_pnl:.2f})',
                'action': 'Review position - consider cutting losses',
                'pnl_percent': pnl_percent,
                'unrealized_pnl': unrealized_pnl
            })

        # Alert: Strong gain (potential to lock in profits)
        if pnl_percent > 10:
            alerts.append({
                'symbol': symbol,
                'type': 'STRONG_GAIN',
                'severity': 'LOW',
                'message': f'{symbol}: Position up {pnl_percent:.1f}% (₹{unrealized_pnl:.2f})',
                'action': 'Consider trailing stop or partial profit-taking',
                'pnl_percent': pnl_percent,
                'unrealized_pnl': unrealized_pnl
            })

    # Sort by severity
    severity_order = {'HIGH': 0, 'MEDIUM': 1, 'LOW': 2}
    alerts.sort(key=lambda x: severity_order.get(x.get('severity', 'LOW'), 3))

    return alerts


def get_filled_trades(days_back: int = 7) -> Dict:
    """
    Get filled trades from Kite for auto-populating trade log
    """
    client = get_client()
    if not client.check_auth():
        return {'success': False, 'error': 'Not authenticated', 'trades': []}

    try:
        # Get order history
        orders = client.kite.orders()

        # Filter for completed orders within the time frame
        cutoff_date = datetime.now() - timedelta(days=days_back)

        formatted = []
        for order in orders:
            if order.get('status') == 'COMPLETE':
                order_time = order.get('order_timestamp')
                if order_time and order_time >= cutoff_date:
                    formatted.append({
                        'order_id': order.get('order_id'),
                        'symbol': order.get('tradingsymbol'),
                        'exchange': order.get('exchange'),
                        'transaction_type': order.get('transaction_type'),
                        'quantity': order.get('filled_quantity', order.get('quantity')),
                        'price': order.get('average_price', order.get('price')),
                        'execution_time': order_time.isoformat() if order_time else None,
                        'order_type': order.get('order_type'),
                        'product': order.get('product'),
                        'tag': order.get('tag')
                    })

        return {
            'success': True,
            'trades': formatted,
            'count': len(formatted)
        }

    except Exception as e:
        return {'success': False, 'error': str(e), 'trades': []}


def create_trade_from_bill(trade_bill: Dict) -> Dict:
    """
    Create and place order from a Trade Bill

    Since Kite doesn't support bracket orders directly,
    we place the entry order and then a GTT-OCO for SL/Target.

    Flow:
    1. Place entry order (LIMIT or MARKET)
    2. Once filled, place GTT-OCO for stop loss and target
    """
    required_fields = ['symbol', 'entry', 'stop_loss', 'target', 'quantity']
    for field in required_fields:
        if field not in trade_bill:
            return {'success': False, 'error': f'Missing required field: {field}'}

    symbol = trade_bill['symbol'].replace('NSE:', '')
    quantity = int(trade_bill['quantity'])
    entry_price = trade_bill['entry']
    stop_loss = trade_bill['stop_loss']
    target = trade_bill['target']

    # Validate trading hours
    is_open, message = check_trading_hours()
    if not is_open:
        return {'success': False, 'error': f'Market closed: {message}'}

    # Place entry order
    entry_result = place_order(
        symbol=symbol,
        transaction_type=TRANSACTION_BUY,
        quantity=quantity,
        price=entry_price,
        order_type=ORDER_TYPE_LIMIT,
        product=PRODUCT_CNC,
        tag='ELDER'
    )

    if not entry_result['success']:
        return entry_result

    # After entry, suggest placing GTT-OCO
    # Note: GTT-OCO should be placed after the order is filled
    return {
        'success': True,
        'order_id': entry_result['order_id'],
        'message': f"Entry order placed for {symbol} at ₹{entry_price}",
        'next_step': 'Place GTT-OCO for stop loss and target after entry fills',
        'suggested_gtt_oco': {
            'symbol': symbol,
            'quantity': quantity,
            'stop_loss_trigger': stop_loss,
            'stop_loss_price': stop_loss * 0.99,  # Slightly below trigger
            'target_trigger': target,
            'target_price': target * 0.99  # Slightly below target for better fill
        },
        'trade_bill_id': trade_bill.get('id'),
        'symbol': symbol
    }
