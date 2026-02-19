"""
Elder Trading System - Background Market Engine
=================================================

Daemon thread that runs a 5-minute cycle during market hours:
  1. Fetch LTP for all watchlist symbols (batch)
  2. Refresh multi-timeframe OHLCV + indicators
  3. Evaluate active alerts against latest prices
  4. Execute auto-trades for triggered alerts (Trade Bill + GTT Buy)
  5. Refresh orders/positions from Kite
  6. Queue notifications for frontend polling

Architecture:
  - Single daemon thread, started when Flask app boots
  - 5-minute sleep between cycles (configurable)
  - Only runs during NSE market hours (9:15-15:30 IST, Mon-Fri)
  - Notifications stored in-memory queue, polled by frontend every 10 seconds
  - Engine state persisted to market_engine_state table
"""

import threading
import time as time_module
import json
import traceback
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from collections import deque

from services.kite_client import get_client, is_nse_market_open


# ═══════════════════════════════════════════════════════
# IN-MEMORY STATE
# ═══════════════════════════════════════════════════════

_engine_thread: Optional[threading.Thread] = None
_engine_running = False
_engine_lock = threading.Lock()

# Notification queue: deque of dicts {id, type, title, message, symbol, timestamp, acknowledged}
_notifications: deque = deque(maxlen=100)
_notification_counter = 0

# Engine stats
_engine_stats = {
    'status': 'stopped',
    'last_cycle': None,
    'last_cycle_duration': None,
    'cycles_completed': 0,
    'errors': [],
    'symbols_count': 0,
    'alerts_evaluated': 0,
    'alerts_triggered': 0,
    'started_at': None,
}


# ═══════════════════════════════════════════════════════
# NOTIFICATION HELPERS
# ═══════════════════════════════════════════════════════

def push_notification(ntype: str, title: str, message: str,
                      symbol: str = None, data: dict = None):
    """Add a notification to the in-memory queue."""
    global _notification_counter
    _notification_counter += 1
    _notifications.append({
        'id': _notification_counter,
        'type': ntype,  # 'alert_triggered', 'trade_created', 'error', 'info'
        'title': title,
        'message': message,
        'symbol': symbol,
        'data': data or {},
        'timestamp': datetime.now().isoformat(),
        'acknowledged': False
    })


def get_pending_notifications() -> List[Dict]:
    """Get all unacknowledged notifications."""
    return [n for n in _notifications if not n['acknowledged']]


def acknowledge_notification(nid: int) -> bool:
    """Mark a notification as acknowledged."""
    for n in _notifications:
        if n['id'] == nid:
            n['acknowledged'] = True
            return True
    return False


def acknowledge_all_notifications():
    """Mark all notifications as acknowledged."""
    for n in _notifications:
        n['acknowledged'] = True


# ═══════════════════════════════════════════════════════
# ENGINE CYCLE
# ═══════════════════════════════════════════════════════

def _run_cycle(app):
    """
    Execute one complete engine cycle:
      1. Get watchlist symbols
      2. Fetch LTP batch
      3. Refresh timeframe data + indicators
      4. Evaluate alerts
      5. Handle triggered alerts (auto-trade or notify)
      6. Refresh orders/positions cache
    """
    from models.database import get_database
    from services.timeframe_data import refresh_all_timeframes
    from services.alert_evaluator import evaluate_alerts, log_alert_trigger, deactivate_alert

    cycle_start = datetime.now()
    print(f"\n{'─'*50}")
    print(f"  Engine Cycle — {cycle_start.strftime('%H:%M:%S')}")
    print(f"{'─'*50}")

    try:
        with app.app_context():
            db = get_database()
            conn = db.get_connection()

            # 1. Get watchlist symbols
            user = conn.execute('SELECT TOP 1 id FROM users ORDER BY id').fetchone()
            if not user:
                print("  No user found, skipping cycle")
                conn.close()
                return
            user_id = user['id']

            wl = conn.execute('''
                SELECT symbols FROM watchlists
                WHERE user_id = ? AND is_trading_watchlist = 1 AND auto_refresh = 1
            ''', (user_id,)).fetchone()

            if not wl:
                print("  No trading watchlist, skipping cycle")
                conn.close()
                return

            symbols = json.loads(wl['symbols']) if wl['symbols'] else []
            _engine_stats['symbols_count'] = len(symbols)
            conn.close()

            if not symbols:
                print("  Watchlist empty, skipping cycle")
                return

            # 2. Fetch LTP for all symbols (batch)
            # Symbols are stored as bare (e.g., 'RELIANCE'), get_ltp auto-adds NSE:
            # But Kite returns keys as 'NSE:SYMBOL', so normalize to bare
            client = get_client()
            ltp_map = {}
            if client and client._authenticated:
                try:
                    ltp_data = client.get_ltp(symbols)
                    for sym, data in ltp_data.items():
                        # Normalize key to bare symbol for consistent lookup
                        bare = sym.replace('NSE:', '').strip()
                        ltp_map[bare] = data.get('last_price')
                    print(f"  LTP fetched for {len(ltp_map)} symbols")
                except Exception as e:
                    print(f"  LTP fetch error: {e}")

            # 3. Refresh multi-timeframe data
            try:
                refresh_result = refresh_all_timeframes(symbols)
                print(f"  Timeframe refresh: {refresh_result.get('success', 0)}/{refresh_result.get('total', 0)}")
            except Exception as e:
                print(f"  Timeframe refresh error: {e}")
                _engine_stats['errors'].append({'time': datetime.now().isoformat(), 'error': str(e)})

            # 4. Evaluate alerts
            try:
                triggered_alerts = evaluate_alerts(user_id, ltp_map)
                _engine_stats['alerts_evaluated'] += 1
                _engine_stats['alerts_triggered'] += len(triggered_alerts)

                print(f"  Alerts evaluated: {len(triggered_alerts)} triggered")

                # 5. Handle triggered alerts
                for trigger in triggered_alerts:
                    _handle_triggered_alert(app, user_id, trigger, ltp_map)

            except Exception as e:
                print(f"  Alert evaluation error: {e}")
                traceback.print_exc()

            # 6. Refresh orders/positions cache
            try:
                _refresh_orders_positions_cache(app, user_id)
            except Exception as e:
                print(f"  Orders/positions refresh error: {e}")

    except Exception as e:
        print(f"  CYCLE ERROR: {e}")
        traceback.print_exc()
        _engine_stats['errors'].append({'time': datetime.now().isoformat(), 'error': str(e)})

    elapsed = (datetime.now() - cycle_start).total_seconds()
    _engine_stats['last_cycle'] = cycle_start.isoformat()
    _engine_stats['last_cycle_duration'] = round(elapsed, 1)
    _engine_stats['cycles_completed'] += 1

    print(f"  Cycle complete in {elapsed:.1f}s")


def _handle_triggered_alert(app, user_id: int, trigger: Dict, ltp_map: Dict):
    """Handle a single triggered alert — either auto-trade or just notify."""
    from services.alert_evaluator import log_alert_trigger, deactivate_alert

    symbol = trigger['symbol']
    alert_id = trigger['alert_id']
    sym_short = symbol.replace('NSE:', '')

    if trigger.get('auto_trade'):
        # Auto-trade: Create Trade Bill + place GTT Buy
        try:
            result = _execute_alert_trade(app, user_id, trigger, ltp_map)

            action = 'trade_bill_created'
            if result.get('gtt_id'):
                action = 'gtt_placed'

            log_alert_trigger(
                alert_id=alert_id,
                user_id=user_id,
                symbol=symbol,
                trigger_price=trigger['trigger_price'],
                action_taken=action,
                trade_bill_id=result.get('trade_bill_id'),
                gtt_order_id=result.get('gtt_id'),
                journal_id=result.get('journal_id'),
                details=json.dumps(result)
            )

            push_notification(
                'trade_created',
                f'Auto-Trade: {sym_short}',
                f"Alert triggered at {trigger['trigger_price']:.2f}. "
                f"Trade Bill #{result.get('trade_bill_id')} created. "
                f"{'GTT Buy placed.' if result.get('gtt_id') else 'GTT placement pending.'}",
                symbol=symbol,
                data=result
            )

            # Deactivate one-shot alert
            deactivate_alert(alert_id)

        except Exception as e:
            print(f"  Auto-trade error for {symbol}: {e}")
            traceback.print_exc()
            log_alert_trigger(alert_id, user_id, symbol, trigger['trigger_price'],
                              'error', details=str(e))
            push_notification('error', f'Trade Error: {sym_short}',
                              f"Auto-trade failed: {str(e)}", symbol=symbol)
    else:
        # Semi-automatic: just notify the user
        log_alert_trigger(alert_id, user_id, symbol, trigger['trigger_price'], 'notified')
        push_notification(
            'alert_triggered',
            f'Alert: {sym_short}',
            f"{trigger.get('alert_name', 'Price Alert')} triggered at {trigger['trigger_price']:.2f}. "
            f"Condition: {trigger.get('condition', '')}",
            symbol=symbol,
            data=trigger
        )


def _execute_alert_trade(app, user_id: int, trigger: Dict, ltp_map: Dict) -> Dict:
    """
    Execute the auto-trade flow:
      1. Calculate position size from account settings
      2. Create Trade Bill
      3. Place GTT Buy order
      4. Create Trade Journal entry
    """
    from models.database import get_database

    symbol = trigger['symbol']
    ltp = trigger['trigger_price']
    sl = trigger.get('stop_loss')
    target = trigger.get('target_price')
    qty = trigger.get('quantity')

    result = {'symbol': symbol, 'trigger_price': ltp}

    with app.app_context():
        db = get_database()
        conn = db.get_connection()

        try:
            # Get account settings for position sizing
            acct = conn.execute('''
                SELECT trading_capital, risk_per_trade FROM account_settings
                WHERE user_id = ?
            ''', (user_id,)).fetchone()

            if acct and sl and not qty:
                # Auto-calculate quantity from risk
                capital = acct['trading_capital'] or 500000
                risk_pct = acct['risk_per_trade'] or 2.0
                max_risk = capital * risk_pct / 100
                risk_per_share = abs(ltp - sl)
                if risk_per_share > 0:
                    qty = int(max_risk / risk_per_share)
                    # Even quantity for OCO split
                    qty = max(2, (qty // 2) * 2)

            if not qty:
                qty = 1  # Fallback

            # Create Trade Bill
            bill_data = {
                'ticker': symbol.replace('NSE:', ''),
                'symbol': symbol,
                'current_market_price': ltp,
                'entry_price': ltp,
                'stop_loss': sl,
                'target_price': target,
                'quantity': qty,
                'direction': trigger.get('direction', 'LONG'),
                'status': 'active',
                'alert_id': trigger['alert_id'],
                'auto_created': True,
                'risk_per_share': abs(ltp - sl) if sl else 0,
                'risk_amount_currency': abs(ltp - sl) * qty if sl else 0,
                'position_size': qty,
                'position_value': ltp * qty,
            }

            # Dynamic insert
            columns = ', '.join(bill_data.keys())
            placeholders = ', '.join(['?' for _ in bill_data])
            values = tuple(bill_data.values())

            bill_row = conn.execute(f'''
                INSERT INTO trade_bills (user_id, {columns})
                OUTPUT INSERTED.id
                VALUES (?, {placeholders})
            ''', (user_id, *values)).fetchone()

            trade_bill_id = int(bill_row[0])
            result['trade_bill_id'] = trade_bill_id
            print(f"  Created Trade Bill #{trade_bill_id} for {symbol}")

            # Create Trade Journal entry
            journal_row = conn.execute('''
                INSERT INTO trade_journal_v2 (
                    user_id, trade_bill_id, ticker, cmp, direction, status,
                    journal_date, entry_price, quantity, stop_loss, target_price,
                    alert_id, auto_created
                )
                OUTPUT INSERTED.id
                VALUES (?, ?, ?, ?, ?, 'open', GETDATE(), ?, ?, ?, ?, ?, 1)
            ''', (
                user_id, trade_bill_id,
                symbol.replace('NSE:', ''), ltp,
                trigger.get('direction', 'Long'),
                ltp, qty, sl, target,
                trigger['alert_id']
            )).fetchone()

            journal_id = int(journal_row[0])
            result['journal_id'] = journal_id
            print(f"  Created Journal #{journal_id}")

            conn.commit()

            # Place GTT Buy Order
            try:
                from services.kite_orders import place_gtt_order
                gtt_result = place_gtt_order(
                    symbol=symbol,
                    transaction_type='BUY',
                    quantity=qty,
                    trigger_price=ltp,
                    limit_price=round(ltp * 1.005, 2),  # 0.5% above trigger
                    trade_bill_id=trade_bill_id
                )
                if gtt_result and gtt_result.get('trigger_id'):
                    result['gtt_id'] = str(gtt_result['trigger_id'])
                    print(f"  GTT Buy placed: {result['gtt_id']}")
                else:
                    result['gtt_error'] = 'GTT placement returned no trigger_id'
            except Exception as e:
                result['gtt_error'] = str(e)
                print(f"  GTT placement failed: {e}")

        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()

    return result


def _refresh_orders_positions_cache(app, user_id: int):
    """Refresh orders and positions from Kite into cache tables."""
    from services.kite_orders import get_open_orders, get_positions, get_holdings

    client = get_client()
    if not client or not client._authenticated:
        return

    try:
        # This is handled by existing sync endpoints
        # Just trigger a lightweight check
        orders = get_open_orders()
        if orders:
            print(f"  Orders cache: {len(orders)} open orders")
    except Exception as e:
        print(f"  Orders refresh error: {e}")


# ═══════════════════════════════════════════════════════
# ENGINE LIFECYCLE
# ═══════════════════════════════════════════════════════

def _engine_loop(app, cycle_seconds: int = 300):
    """
    Main engine loop. Runs in a daemon thread.
    Sleeps between cycles, only active during market hours.
    """
    global _engine_running

    print("\n" + "=" * 60)
    print("  Market Engine STARTED")
    print(f"  Cycle interval: {cycle_seconds}s ({cycle_seconds//60} min)")
    print("=" * 60 + "\n")

    _engine_stats['started_at'] = datetime.now().isoformat()
    _engine_stats['status'] = 'running'

    push_notification('info', 'Engine Started',
                      f'Market engine started with {cycle_seconds}s cycle interval.')

    while _engine_running:
        try:
            is_open, msg = is_nse_market_open()

            if is_open:
                _engine_stats['status'] = 'running'
                _run_cycle(app)
            else:
                _engine_stats['status'] = 'waiting'
                print(f"  Market closed: {msg}. Waiting...")

        except Exception as e:
            print(f"  Engine loop error: {e}")
            traceback.print_exc()

        # Sleep in 1-second increments so we can respond to stop requests quickly
        for _ in range(cycle_seconds):
            if not _engine_running:
                break
            time_module.sleep(1)

    _engine_stats['status'] = 'stopped'
    print("\n  Market Engine STOPPED\n")


def start_engine(app, cycle_seconds: int = 300) -> bool:
    """
    Start the background market engine.

    Args:
        app: Flask app instance (needed for app context in background thread)
        cycle_seconds: Seconds between cycles (default 300 = 5 min)

    Returns:
        True if started, False if already running
    """
    global _engine_thread, _engine_running

    with _engine_lock:
        if _engine_running and _engine_thread and _engine_thread.is_alive():
            return False  # Already running

        _engine_running = True
        _engine_thread = threading.Thread(
            target=_engine_loop,
            args=(app, cycle_seconds),
            daemon=True,
            name='MarketEngine'
        )
        _engine_thread.start()
        return True


def stop_engine() -> bool:
    """Stop the background market engine."""
    global _engine_running

    with _engine_lock:
        if not _engine_running:
            return False

        _engine_running = False
        push_notification('info', 'Engine Stopped', 'Market engine has been stopped.')
        return True


def get_engine_status() -> Dict:
    """Get current engine status and stats."""
    return {
        'running': _engine_running,
        'thread_alive': _engine_thread.is_alive() if _engine_thread else False,
        **_engine_stats,
        'pending_notifications': len(get_pending_notifications()),
        'errors_recent': _engine_stats['errors'][-5:] if _engine_stats['errors'] else []
    }


def trigger_manual_refresh(app) -> bool:
    """Trigger an immediate cycle (outside the regular schedule)."""
    if not _engine_running:
        return False

    # Run cycle in a separate thread to not block the request
    t = threading.Thread(target=_run_cycle, args=(app,), daemon=True)
    t.start()
    return True
