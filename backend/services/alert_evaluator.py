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

import pytz

from models.database import get_database

IST = pytz.timezone('Asia/Kolkata')

# 75-min block close times in IST (hour, minute) — 1 min after each close
VALID_75MIN_SCHEDULE = {
    (10, 31),   # Block 1 closes 10:30
    (11, 46),   # Block 2 closes 11:45
    (13, 1),    # Block 3 closes 13:00
    (14, 16),   # Block 4 closes 14:15
    (15, 31),   # Block 5 closes 15:30
}

# 15-min candle close minutes — only evaluate 15-min alerts at these minutes
VALID_15MIN_MINUTES = {1, 16, 31, 46}


def evaluate_alerts(user_id: int, ltp_map: Dict[str, float],
                    candle_patterns: Dict[str, Dict] = None) -> List[Dict]:
    """
    Evaluate all active alerts for a user against current LTP values.
    Filters alerts by timeframe — 75-min alerts only evaluated at 75-min boundaries.

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

    # Current IST time for timeframe-aware filtering
    now_ist = datetime.now(IST)
    current_hm = (now_ist.hour, now_ist.minute)

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

            # Timeframe-aware filtering: only evaluate at matching candle boundaries
            tf = alert.get('timeframe', '15min')
            if tf == '75min' and current_hm not in VALID_75MIN_SCHEDULE:
                continue  # Not a 75-min boundary cycle
            if tf == '15min' and now_ist.minute not in VALID_15MIN_MINUTES:
                continue  # Not a 15-min boundary cycle
            # 5-min alerts evaluate at every cycle (no filtering needed)
            # Normalize to bare symbol for LTP lookup (LTP map now uses bare keys)
            bare_symbol = symbol.replace('NSE:', '').strip().upper()
            ltp = ltp_map.get(bare_symbol)

            if ltp is None:
                continue  # No price data for this symbol

            # Evaluate price condition
            condition = alert.get('condition_type', 'price_below')
            target_val = alert.get('condition_value')
            operator = alert.get('condition_operator', '<=')

            if target_val is None:
                continue

            price_met = _check_price_condition(ltp, target_val, operator)
            if not price_met:
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
                'condition_value': alert.get('condition_value'),
                'timeframe': alert.get('timeframe', '15min'),
                'trade_bill_id': alert.get('trade_bill_id'),
                'max_target_price': alert.get('max_target_price'),
                'min_quantity': alert.get('min_quantity'),
                'max_take_profit': alert.get('max_take_profit'),
                'exchange': alert.get('exchange', 'NSE'),
                'futures_trade_bill_id': alert.get('futures_trade_bill_id'),
            }
            triggered.append(trigger_info)

            # Update alert: increment trigger count, set last trigger time,
            # and mark as 'triggered' to prevent re-triggering (no cooldown needed)
            conn.execute('''
                UPDATE stock_alerts
                SET trigger_count = ISNULL(trigger_count, 0) + 1,
                    last_trigger_time = GETDATE(),
                    triggered_at = GETDATE(),
                    status = 'triggered'
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


def log_audit(user_id: int, action_type: str, source: str,
              symbol: str = None, details: str = None,
              status: str = 'success', related_id: int = None,
              related_type: str = None):
    """Log an action to the audit_log table."""
    db = get_database()
    conn = db.get_connection()
    try:
        conn.execute('''
            INSERT INTO audit_log
            (user_id, action_type, source, symbol, details, status,
             related_id, related_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (user_id, action_type, source, symbol, details, status,
              related_id, related_type))
        conn.commit()
    except Exception as e:
        print(f"  Audit log error: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
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
