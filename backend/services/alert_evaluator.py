"""
Elder Trading System - Alert Evaluator
=======================================

Evaluates user-defined price alerts against live market data.
Supports:
  - Price condition alerts (price <= X, price >= X, etc.)
  - Candle confirmation (optional: wait for a bullish/bearish candle pattern)
  - Cooldown periods (don't re-trigger within N minutes)
  - Auto-trade flag (triggers Trade Bill + GTT creation)

Called by the Market Engine on every 5-minute cycle.
"""

from datetime import datetime, timedelta
from typing import List, Dict, Optional
import json
import traceback

from models.database import get_database


def evaluate_alerts(user_id: int, ltp_map: Dict[str, float],
                    candle_patterns: Dict[str, Dict] = None) -> List[Dict]:
    """
    Evaluate all active alerts for a user against current LTP values.

    Args:
        user_id: User ID
        ltp_map: Dict mapping symbol → latest price (e.g. {'NSE:RELIANCE': 2850.5})
        candle_patterns: Optional dict mapping symbol → {pattern_name: conviction}
                         from candlestick pattern scan

    Returns:
        List of triggered alert dicts with action info
    """
    db = get_database()
    conn = db.get_connection()
    triggered = []

    try:
        # Fetch all active alerts for this user
        alerts = conn.execute('''
            SELECT * FROM stock_alerts
            WHERE user_id = ? AND status = 'active'
            ORDER BY symbol, id
        ''', (user_id,)).fetchall()

        now = datetime.now()

        for alert in alerts:
            alert = dict(alert)
            symbol = alert['symbol']
            # Normalize to bare symbol for LTP lookup (LTP map now uses bare keys)
            bare_symbol = symbol.replace('NSE:', '').strip().upper()
            ltp = ltp_map.get(bare_symbol)

            if ltp is None:
                continue  # No price data for this symbol

            # Check cooldown
            last_trigger = alert.get('last_trigger_time')
            cooldown = alert.get('cooldown_minutes', 60) or 60
            if last_trigger:
                if isinstance(last_trigger, str):
                    last_trigger = datetime.fromisoformat(last_trigger)
                if now - last_trigger < timedelta(minutes=cooldown):
                    continue  # Still in cooldown

            # Evaluate price condition
            condition = alert.get('condition_type', 'price_below')
            target_val = alert.get('condition_value')
            operator = alert.get('condition_operator', '<=')

            if target_val is None:
                continue

            price_met = _check_price_condition(ltp, target_val, operator)
            if not price_met:
                continue

            # Check candle confirmation if required
            candle_ok = True
            if alert.get('candle_confirm') and candle_patterns:
                sym_patterns = candle_patterns.get(symbol, {})
                required_pattern = alert.get('candle_pattern', '')
                if required_pattern:
                    # Check if any matching pattern found
                    candle_ok = any(
                        required_pattern.lower() in pname.lower()
                        for pname in sym_patterns.keys()
                    )
                else:
                    # Any bullish pattern for LONG, any bearish for SHORT
                    direction = alert.get('direction', 'LONG')
                    if direction == 'LONG':
                        candle_ok = any(
                            v.get('type') == 'bullish' or v.get('conviction') in ('high', 'medium')
                            for v in sym_patterns.values()
                        ) if sym_patterns else False
                    else:
                        candle_ok = any(
                            v.get('type') == 'bearish'
                            for v in sym_patterns.values()
                        ) if sym_patterns else False

            if not candle_ok:
                continue

            # Alert triggered!
            trigger_info = {
                'alert_id': alert['id'],
                'symbol': symbol,
                'alert_name': alert.get('alert_name', ''),
                'direction': alert.get('direction', 'LONG'),
                'trigger_price': ltp,
                'condition': f"LTP {operator} {target_val}",
                'auto_trade': bool(alert.get('auto_trade')),
                'stop_loss': alert.get('stop_loss'),
                'target_price': alert.get('target_price'),
                'quantity': alert.get('quantity'),
                'candle_confirmed': bool(alert.get('candle_confirm')),
            }
            triggered.append(trigger_info)

            # Update alert: increment trigger count, set last trigger time
            conn.execute('''
                UPDATE stock_alerts
                SET trigger_count = ISNULL(trigger_count, 0) + 1,
                    last_trigger_time = GETDATE(),
                    triggered_at = GETDATE()
                WHERE id = ?
            ''', (alert['id'],))

        conn.commit()
    except Exception as e:
        print(f"Error evaluating alerts: {e}")
        traceback.print_exc()
        conn.rollback()
    finally:
        conn.close()

    return triggered


def _check_price_condition(ltp: float, target: float, operator: str) -> bool:
    """Check if LTP meets the condition relative to target."""
    if operator == '<=':
        return ltp <= target
    elif operator == '>=':
        return ltp >= target
    elif operator == '<':
        return ltp < target
    elif operator == '>':
        return ltp > target
    elif operator == '==':
        return abs(ltp - target) < 0.05  # Small tolerance
    elif operator == 'crosses_above':
        # This would need previous price — simplified to >=
        return ltp >= target
    elif operator == 'crosses_below':
        return ltp <= target
    return False


def log_alert_trigger(alert_id: int, user_id: int, symbol: str,
                      trigger_price: float, action_taken: str,
                      trade_bill_id: int = None, gtt_order_id: str = None,
                      journal_id: int = None, details: str = None):
    """
    Log an alert trigger event to alert_history.

    Args:
        alert_id: The alert that was triggered
        user_id: User who owns the alert
        symbol: Stock symbol
        trigger_price: Price at time of trigger
        action_taken: 'notified', 'trade_bill_created', 'gtt_placed', 'user_cancelled'
        trade_bill_id: ID of auto-created trade bill (if any)
        gtt_order_id: GTT order ID (if any)
        journal_id: Journal entry ID (if any)
        details: Additional JSON details
    """
    db = get_database()
    conn = db.get_connection()

    try:
        conn.execute('''
            INSERT INTO alert_history
            (alert_id, user_id, symbol, trigger_price, action_taken,
             trade_bill_id, gtt_order_id, journal_id, details)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            alert_id, user_id, symbol, trigger_price, action_taken,
            trade_bill_id, gtt_order_id, journal_id, details
        ))
        conn.commit()
    except Exception as e:
        print(f"Error logging alert trigger: {e}")
        conn.rollback()
    finally:
        conn.close()


def deactivate_alert(alert_id: int):
    """Set an alert status to 'triggered' (one-shot deactivation)."""
    db = get_database()
    conn = db.get_connection()
    try:
        conn.execute('''
            UPDATE stock_alerts SET status = 'triggered', updated_at = GETDATE()
            WHERE id = ?
        ''', (alert_id,))
        conn.commit()
    except Exception as e:
        print(f"Error deactivating alert {alert_id}: {e}")
    finally:
        conn.close()
