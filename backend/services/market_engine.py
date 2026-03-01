"""
Elder Trading System - Background Market Engine
=================================================

Daemon thread that runs at 15-minute aligned intervals during market hours:
  1. Fetch LTP for all watchlist symbols (batch)
  2. Refresh multi-timeframe OHLCV + indicators
  3. Evaluate active alerts against latest prices
  4. Execute auto-trades for triggered alerts (candle close entry + auto sizing)
  5. Monitor pending auto-trade orders for fill → place OCO Sell on execution
  6. Refresh orders/positions from Kite
  7. Queue notifications for frontend polling

Architecture:
  - Single daemon thread, started when Flask app boots
  - 15-min aligned scheduling: runs at :01, :16, :31, :46 past each hour
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

import pytz
IST = pytz.timezone('Asia/Kolkata')

# Schedule: 1 minute after each 15-min candle close
SCHEDULE_MINUTES = [1, 16, 31, 46]


def _get_next_schedule_time():
    """Calculate the next scheduled run time (IST)."""
    now = datetime.now(IST)
    current_minute = now.minute

    # Find next scheduled minute in current hour
    for sm in SCHEDULE_MINUTES:
        if sm > current_minute:
            return now.replace(minute=sm, second=0, microsecond=0)

    # Next hour, first schedule
    next_time = (now + timedelta(hours=1)).replace(minute=SCHEDULE_MINUTES[0], second=0, microsecond=0)
    return next_time


def _get_candle_close(symbol, timeframe='15min'):
    """
    Get the close price of the most recently completed candle.
    Reads from intraday_ohlcv table (populated by timeframe_data refresh).
    Fallback: returns None (caller should use LTP).
    """
    from models.database import get_database
    db = get_database()
    conn = db.get_connection()
    try:
        row = conn.execute('''
            SELECT TOP 1 close_price FROM intraday_ohlcv
            WHERE symbol = ? AND timeframe = ?
            ORDER BY datetime DESC
        ''', (symbol.replace('NSE:', ''), timeframe)).fetchone()
        if row:
            return row['close_price']
    except Exception as e:
        print(f"  Candle close lookup error for {symbol}: {e}")
    finally:
        conn.close()
    return None


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
      1. Get candle close price (or fallback to LTP)
      2. Calculate position size from AUTO-TRADE settings
      3. Create Trade Bill (auto_created = 1)
      4. Place NRML Limit Buy order at candle close price
      5. Track in auto_trade_orders (OCO placed later on fill)
      6. Create Trade Journal entry
    """
    from models.database import get_database

    symbol = trigger['symbol']
    ltp = trigger['trigger_price']
    sl = trigger.get('stop_loss')
    alert_timeframe = trigger.get('timeframe', '15min')

    result = {'symbol': symbol, 'trigger_price': ltp}

    with app.app_context():
        db = get_database()
        conn = db.get_connection()

        try:
            # Get auto-trade settings (separate from manual trading settings)
            acct = conn.execute('''
                SELECT trading_capital, risk_per_trade,
                       auto_trade_capital, auto_trade_sl_pct,
                       auto_trade_rr_ratio, auto_trade_max_trades
                FROM account_settings WHERE user_id = ?
            ''', (user_id,)).fetchone()

            at_capital = (acct['auto_trade_capital'] if acct else None) or 100000
            at_sl_pct = (acct['auto_trade_sl_pct'] if acct else None) or 1.0
            at_rr = (acct['auto_trade_rr_ratio'] if acct else None) or 2.0
            at_max = (acct['auto_trade_max_trades'] if acct else None) or 3

            # Check max auto trades limit
            active_auto = conn.execute('''
                SELECT COUNT(*) AS cnt FROM auto_trade_orders
                WHERE user_id = ? AND buy_status IN ('PENDING', 'COMPLETE')
                AND (oco_status IS NULL OR oco_status != 'TRIGGERED')
            ''', (user_id,)).fetchone()
            if active_auto and active_auto['cnt'] >= at_max:
                result['error'] = f'Max auto trades reached ({at_max})'
                print(f"  Max auto trades reached ({at_max}), skipping {symbol}")
                conn.close()
                return result

            # Use candle close price as entry (fallback to LTP)
            candle_close = _get_candle_close(symbol, alert_timeframe)
            entry = candle_close if candle_close else ltp
            result['entry_price'] = entry
            result['entry_source'] = 'candle_close' if candle_close else 'ltp'

            if not sl:
                result['error'] = 'No stop_loss configured in alert'
                conn.close()
                return result

            # Auto-calculate quantity from auto-trade settings
            risk_per_share = abs(entry - sl)
            if risk_per_share <= 0:
                result['error'] = 'Entry equals SL, cannot calculate risk'
                conn.close()
                return result

            max_risk = at_capital * at_sl_pct / 100
            qty = int(max_risk / risk_per_share)  # floor to whole number
            if qty < 1:
                qty = 1

            # Calculate target from RR ratio
            direction = trigger.get('direction', 'LONG')
            if direction.upper() == 'LONG':
                target = entry + risk_per_share * at_rr
            else:
                target = entry - risk_per_share * at_rr

            result['quantity'] = qty
            result['target'] = target

            # Create Trade Bill (auto_created = 1)
            bill_data = {
                'ticker': symbol.replace('NSE:', ''),
                'symbol': symbol,
                'current_market_price': ltp,
                'entry_price': entry,
                'stop_loss': sl,
                'target_price': target,
                'quantity': qty,
                'direction': direction,
                'status': 'active',
                'alert_id': trigger['alert_id'],
                'auto_created': 1,
                'risk_per_share': risk_per_share,
                'risk_amount_currency': risk_per_share * qty,
                'reward_amount_currency': risk_per_share * at_rr * qty,
                'risk_reward_ratio': at_rr,
                'position_size': qty,
                'position_value': entry * qty,
            }

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
            print(f"  Created Auto Trade Bill #{trade_bill_id} for {symbol} @ {entry}")

            # Create Trade Journal entry (pre-populated, entries added on fill)
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
                'Long' if direction.upper() == 'LONG' else 'Short',
                entry, qty, sl, target,
                trigger['alert_id']
            )).fetchone()

            journal_id = int(journal_row[0])
            result['journal_id'] = journal_id
            print(f"  Created Journal #{journal_id}")

            conn.commit()

            # Place NRML Limit Buy Order at candle close price
            buy_order_id = None
            try:
                from services.kite_orders import place_order
                nrml_result = place_order(
                    symbol=symbol.replace('NSE:', ''),
                    transaction_type='BUY',
                    quantity=qty,
                    price=entry,
                    order_type='LIMIT',
                    product='CNC'
                )
                if nrml_result and nrml_result.get('order_id'):
                    buy_order_id = str(nrml_result['order_id'])
                    result['order_id'] = buy_order_id
                    print(f"  NRML Buy placed: {buy_order_id} @ {entry}")
                else:
                    result['order_error'] = 'NRML order returned no order_id'
            except Exception as e:
                result['order_error'] = str(e)
                print(f"  NRML order failed: {e}")

            # Track in auto_trade_orders (OCO placed after fill confirmation)
            conn2 = db.get_connection()
            try:
                conn2.execute('''
                    INSERT INTO auto_trade_orders (
                        user_id, alert_id, trade_bill_id, journal_id,
                        symbol, buy_order_id, buy_status, buy_price,
                        quantity, stop_loss, target
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    user_id, trigger['alert_id'], trade_bill_id, journal_id,
                    symbol.replace('NSE:', ''),
                    buy_order_id, 'PENDING' if buy_order_id else 'FAILED',
                    entry, qty, sl, target
                ))
                conn2.commit()
            finally:
                conn2.close()

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

def _monitor_auto_trade_orders(app):
    """
    Check pending auto-trade buy orders for execution.
    When filled: place OCO Sell GTT (same qty both legs), update tracking.
    """
    from models.database import get_database

    with app.app_context():
        db = get_database()
        conn = db.get_connection()
        try:
            # Get pending orders that have a buy_order_id
            pending = conn.execute('''
                SELECT * FROM auto_trade_orders
                WHERE buy_status = 'PENDING' AND buy_order_id IS NOT NULL
            ''').fetchall()

            if not pending:
                return

            # Fetch order book from Kite
            client = get_client()
            if not client or not client._authenticated:
                return

            try:
                orders = client.kite.orders()
            except Exception as e:
                print(f"  Order fetch error: {e}")
                return

            order_map = {str(o['order_id']): o for o in orders}

            for ato in pending:
                kite_order = order_map.get(ato['buy_order_id'])
                if not kite_order:
                    continue

                status = kite_order.get('status', '')

                if status == 'COMPLETE':
                    fill_price = kite_order.get('average_price', ato['buy_price'])
                    print(f"  Auto-trade FILLED: {ato['symbol']} @ {fill_price}")

                    # 1. Place OCO Sell (same qty both legs)
                    oco_id = None
                    try:
                        from services.kite_orders import place_gtt_oco
                        oco_result = place_gtt_oco(
                            symbol=f"NSE:{ato['symbol']}",
                            quantity=ato['quantity'],
                            stop_loss_trigger=ato['stop_loss'],
                            stop_loss_price=ato['stop_loss'],
                            target_trigger=ato['target'],
                            target_price=ato['target']
                        )
                        if oco_result and oco_result.get('trigger_id'):
                            oco_id = str(oco_result['trigger_id'])
                            print(f"  OCO Sell placed: {oco_id}")
                    except Exception as e:
                        print(f"  OCO placement error: {e}")

                    # 2. Update auto_trade_orders tracking
                    conn.execute('''
                        UPDATE auto_trade_orders
                        SET buy_status = 'COMPLETE', fill_price = ?,
                            oco_trigger_id = ?, oco_status = ?,
                            updated_at = GETDATE()
                        WHERE id = ?
                    ''', (fill_price, oco_id, 'PLACED' if oco_id else None, ato['id']))

                    # 3. Create trade entry record with actual fill price
                    if ato['journal_id']:
                        try:
                            conn.execute('''
                                INSERT INTO trade_entries (
                                    journal_id, entry_datetime, entry_price, quantity,
                                    day_high, day_low
                                ) VALUES (?, GETDATE(), ?, ?, 0, 0)
                            ''', (ato['journal_id'], fill_price, ato['quantity']))

                            # Update journal with avg_entry from fill
                            conn.execute('''
                                UPDATE trade_journal_v2
                                SET avg_entry = ?, total_shares = ?, remaining_qty = ?,
                                    first_entry_date = GETDATE()
                                WHERE id = ?
                            ''', (fill_price, ato['quantity'], ato['quantity'], ato['journal_id']))
                        except Exception as e:
                            print(f"  Journal update error: {e}")

                    conn.commit()

                    push_notification(
                        'trade_filled',
                        f'Order Filled: {ato["symbol"]}',
                        f"Buy filled @ {fill_price:.2f}, Qty: {ato['quantity']}. "
                        f"{'OCO Sell placed.' if oco_id else 'OCO pending.'}",
                        symbol=ato['symbol']
                    )

                elif status in ('CANCELLED', 'REJECTED'):
                    conn.execute('''
                        UPDATE auto_trade_orders
                        SET buy_status = ?, updated_at = GETDATE()
                        WHERE id = ?
                    ''', (status, ato['id']))
                    conn.commit()

                    push_notification(
                        'order_failed',
                        f'Order {status}: {ato["symbol"]}',
                        f"Buy order {ato['buy_order_id']} was {status}.",
                        symbol=ato['symbol']
                    )

        except Exception as e:
            print(f"  Order monitoring error: {e}")
            traceback.print_exc()
        finally:
            conn.close()


def _engine_loop(app, cycle_seconds: int = 300):
    """
    Main engine loop with 15-min aligned scheduling.
    Runs at :01, :16, :31, :46 past each hour during market hours.
    cycle_seconds is kept for API compatibility but scheduling is now time-aligned.
    """
    global _engine_running

    print("\n" + "=" * 60)
    print("  Market Engine STARTED (15-min aligned)")
    print(f"  Schedule: runs at minutes {SCHEDULE_MINUTES} past each hour")
    print("=" * 60 + "\n")

    _engine_stats['started_at'] = datetime.now().isoformat()
    _engine_stats['status'] = 'running'

    push_notification('info', 'Engine Started',
                      f'Market engine started with 15-min aligned scheduling.')

    while _engine_running:
        try:
            is_open, msg = is_nse_market_open()

            if is_open:
                _engine_stats['status'] = 'running'
                _run_cycle(app)
                # Monitor pending auto-trade orders for fills
                _monitor_auto_trade_orders(app)
            else:
                _engine_stats['status'] = 'waiting'
                print(f"  Market closed: {msg}. Waiting...")

        except Exception as e:
            print(f"  Engine loop error: {e}")
            traceback.print_exc()

        # Sleep until next scheduled time
        try:
            next_time = _get_next_schedule_time()
            wait_seconds = max(1, (next_time - datetime.now(IST)).total_seconds())
            next_str = next_time.strftime('%H:%M:%S')
            print(f"  Next cycle at {next_str} IST ({int(wait_seconds)}s)")
        except Exception:
            wait_seconds = cycle_seconds  # Fallback

        # Sleep in 1-second increments for responsive shutdown
        for _ in range(int(wait_seconds)):
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
