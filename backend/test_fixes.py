"""
Unit Tests for Bug Fix Verification
=====================================

Tests all 5 bug fixes:
1. Settings - Load NIFTY 500 Instruments
2. Trade Bill - Ticker typeahead (instrument search)
3. Engine notification (no spam)
4. NSE: prefix removal (bare symbols everywhere)
5. Watchlist indicator loading

Run: python test_fixes.py
"""

import sys
import os
import json
import io

# Force UTF-8 output on Windows
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import create_app

# Track results
PASSED = 0
FAILED = 0
ERRORS = []


def test(name, passed, detail=""):
    global PASSED, FAILED, ERRORS
    if passed:
        PASSED += 1
        print(f"  ✅ {name}")
    else:
        FAILED += 1
        msg = f"  ❌ {name}" + (f" — {detail}" if detail else "")
        print(msg)
        ERRORS.append(msg)


def run_tests():
    global PASSED, FAILED, ERRORS

    app = create_app()
    client = app.test_client()

    print("\n" + "=" * 70)
    print("  UNIT TESTS — Bug Fix Verification")
    print("=" * 70)

    # ──────────────────────────────────────────────────────────────────
    # TEST 1: Settings — Instruments Search & Load
    # ──────────────────────────────────────────────────────────────────
    print("\n── Test 1: Instruments Search Endpoint ──")

    # 1a. Search for RELIANCE
    resp = client.get('/api/v2/instruments/search?q=RELIANCE&limit=5')
    test("instruments/search endpoint returns 200", resp.status_code == 200)

    data = resp.get_json()
    if isinstance(data, list):
        test("instruments/search returns array", True)
        if len(data) > 0:
            test("instruments/search finds RELIANCE", any('RELIANCE' in str(d) for d in data))
        else:
            test("instruments/search finds RELIANCE", False, "Empty result — need to load instruments first")
    else:
        test("instruments/search returns array", False, f"Got: {type(data)}")

    # 1b. Search for TCS
    resp = client.get('/api/v2/instruments/search?q=TCS&limit=5')
    test("instruments/search for TCS returns 200", resp.status_code == 200)

    # 1c. Instruments load endpoint exists (don't actually trigger — it's a heavy operation)
    resp = client.post('/api/v2/instruments/load',
                       data=json.dumps({}),
                       content_type='application/json')
    # Should either work or return a proper error (not 500)
    test("instruments/load endpoint responds (not 500)",
         resp.status_code != 500,
         f"Status: {resp.status_code}")

    # ──────────────────────────────────────────────────────────────────
    # TEST 2: Trading Watchlist — CRUD
    # ──────────────────────────────────────────────────────────────────
    print("\n── Test 2: Trading Watchlist CRUD ──")

    # 2a. Get watchlist (may or may not exist)
    resp = client.get('/api/v2/trading-watchlist')
    test("GET /trading-watchlist returns 200", resp.status_code == 200)
    data = resp.get_json()
    test("GET /trading-watchlist has success field", data.get('success') == True)

    # 2b. Add symbol (bare, no NSE: prefix)
    resp = client.post('/api/v2/trading-watchlist/add-symbol',
                       data=json.dumps({'symbol': 'RELIANCE'}),
                       content_type='application/json')
    test("add-symbol RELIANCE returns 200", resp.status_code == 200)
    data = resp.get_json()
    test("add-symbol returns success", data.get('success') == True)
    if data.get('symbols'):
        # Verify symbol stored as bare (no NSE: prefix)
        has_bare = 'RELIANCE' in data['symbols']
        has_prefixed = 'NSE:RELIANCE' in data['symbols']
        test("symbol stored as bare (no NSE: prefix)",
             has_bare and not has_prefixed,
             f"Symbols: {data['symbols']}")
    else:
        test("symbol stored as bare (no NSE: prefix)", False, "No symbols in response")

    # 2c. Add another symbol with NSE: prefix (should be stripped)
    resp = client.post('/api/v2/trading-watchlist/add-symbol',
                       data=json.dumps({'symbol': 'NSE:TCS'}),
                       content_type='application/json')
    test("add-symbol NSE:TCS returns 200", resp.status_code == 200)
    data = resp.get_json()
    if data.get('symbols'):
        has_bare_tcs = 'TCS' in data['symbols']
        has_prefixed_tcs = 'NSE:TCS' in data['symbols']
        test("NSE:TCS stored as bare TCS",
             has_bare_tcs and not has_prefixed_tcs,
             f"Symbols: {data['symbols']}")
    else:
        test("NSE:TCS stored as bare TCS", False, "No symbols in response")

    # 2d. Get watchlist again — verify enriched data
    resp = client.get('/api/v2/trading-watchlist')
    test("GET /trading-watchlist with symbols returns 200", resp.status_code == 200)
    data = resp.get_json()
    test("GET /trading-watchlist has data array", isinstance(data.get('data'), list))
    if data.get('data') and len(data['data']) > 0:
        first = data['data'][0]
        test("first row has symbol field", 'symbol' in first)
        test("first row symbol is bare format",
             first.get('symbol', '').find('NSE:') == -1,
             f"Got: {first.get('symbol')}")
        test("first row has ltp field", 'ltp' in first)
    else:
        test("watchlist data not empty", False, "No data rows returned")

    # 2e. Remove symbol
    resp = client.delete('/api/v2/trading-watchlist/remove-symbol/TCS')
    test("remove-symbol TCS returns 200", resp.status_code == 200)
    data = resp.get_json()
    test("remove-symbol returns success", data.get('success') == True)
    if data.get('symbols'):
        test("TCS removed from watchlist", 'TCS' not in data['symbols'])
    else:
        test("TCS removed from watchlist", True)  # empty list = removed

    # ──────────────────────────────────────────────────────────────────
    # TEST 3: Alert System — CRUD
    # ──────────────────────────────────────────────────────────────────
    print("\n── Test 3: Alert System CRUD ──")

    # 3a. Create alert with bare symbol
    alert_payload = {
        'symbol': 'RELIANCE',
        'alert_name': 'Test Alert',
        'direction': 'LONG',
        'condition_operator': '<=',
        'condition_value': 2500.0,
        'condition_type': 'price_level',
        'timeframe': '15min',
        'cooldown_minutes': 60,
        'auto_trade': False,
        'candle_confirm': False,
    }
    resp = client.post('/api/v2/alerts',
                       data=json.dumps(alert_payload),
                       content_type='application/json')
    test("create alert returns 201", resp.status_code == 201)
    data = resp.get_json()
    test("create alert returns success", data.get('success') == True)
    alert_id = data.get('id')
    test("create alert returns id", alert_id is not None)

    # 3b. Create alert with NSE: prefix (should be stripped)
    alert_payload2 = {**alert_payload, 'symbol': 'NSE:INFY', 'alert_name': 'NSE Prefix Test'}
    resp = client.post('/api/v2/alerts',
                       data=json.dumps(alert_payload2),
                       content_type='application/json')
    test("create alert with NSE: prefix returns 201", resp.status_code == 201)
    alert_id2 = resp.get_json().get('id')

    # 3c. Get alerts and verify symbol is bare
    resp = client.get('/api/v2/alerts')
    test("GET /alerts returns 200", resp.status_code == 200)
    data = resp.get_json()
    alerts_list = data.get('alerts', [])
    test("alerts list not empty", len(alerts_list) > 0)

    if alerts_list:
        # Find our test alerts
        test_alerts = [a for a in alerts_list if a.get('alert_name') in ('Test Alert', 'NSE Prefix Test')]
        for ta in test_alerts:
            symbol = ta.get('symbol', '')
            test(f"alert '{ta['alert_name']}' has bare symbol",
                 'NSE:' not in symbol,
                 f"Symbol: {symbol}")

    # 3d. Delete test alerts
    if alert_id:
        resp = client.delete(f'/api/v2/alerts/{alert_id}')
        test("delete alert returns 200", resp.status_code == 200)
    if alert_id2:
        resp = client.delete(f'/api/v2/alerts/{alert_id2}')
        test("delete alert2 returns 200", resp.status_code == 200)

    # ──────────────────────────────────────────────────────────────────
    # TEST 4: Engine Status — Not Auto-Started
    # ──────────────────────────────────────────────────────────────────
    print("\n── Test 4: Engine Status ──")

    resp = client.get('/api/v2/engine/status')
    test("GET /engine/status returns 200", resp.status_code == 200)
    data = resp.get_json()
    test("engine status has running field", 'running' in data)
    test("engine is NOT auto-started", data.get('running') == False,
         f"running={data.get('running')}")

    # 4b. Notifications should be empty (no auto-start spam)
    resp = client.get('/api/v2/engine/notifications')
    test("GET /engine/notifications returns 200", resp.status_code == 200)
    data = resp.get_json()
    notifs = data.get('notifications', [])
    # Filter for 'Engine Started' type notifications
    engine_start_notifs = [n for n in notifs if 'Engine Started' in n.get('title', '')]
    test("no 'Engine Started' notification spam",
         len(engine_start_notifs) == 0,
         f"Found {len(engine_start_notifs)} Engine Started notifications")

    # ──────────────────────────────────────────────────────────────────
    # TEST 5: Timeframe Data Refresh & Indicators
    # ──────────────────────────────────────────────────────────────────
    print("\n── Test 5: Timeframe Data & Indicators ──")

    # 5a. Refresh data for a single symbol (RELIANCE is in watchlist from Test 2)
    resp = client.post('/api/v2/timeframe/refresh',
                       data=json.dumps({'symbol': 'RELIANCE'}),
                       content_type='application/json')
    test("POST /timeframe/refresh for RELIANCE returns 200",
         resp.status_code == 200,
         f"Status: {resp.status_code}")
    data = resp.get_json()
    if data.get('success'):
        result = data.get('result', {})
        test("timeframe refresh returns symbol",
             result.get('symbol') == 'RELIANCE',
             f"Got symbol: {result.get('symbol')}")
        test("15min data refreshed", result.get('15min') == True)
        test("75min data refreshed", result.get('75min') == True)
        test("daily data refreshed", result.get('day') == True)
    else:
        error_msg = data.get('error', 'Unknown error')
        test("timeframe refresh succeeded", False, f"Error: {error_msg}")
        # Skip dependent tests
        test("15min data refreshed", False, "Skipped — refresh failed")
        test("75min data refreshed", False, "Skipped — refresh failed")
        test("daily data refreshed", False, "Skipped — refresh failed")

    # 5b. Get indicators for RELIANCE
    resp = client.get('/api/v2/timeframe/indicators/RELIANCE')
    test("GET /timeframe/indicators/RELIANCE returns 200",
         resp.status_code == 200)
    data = resp.get_json()
    if data.get('success') and data.get('indicators'):
        indicators = data['indicators']
        test("indicators has 'day' timeframe", 'day' in indicators)
        test("indicators has '75min' timeframe", '75min' in indicators)
        test("indicators has '15min' timeframe", '15min' in indicators)

        # Check day indicators have RSI, ATR, impulse
        if 'day' in indicators:
            day_ind = indicators['day']
            test("day indicators has RSI", day_ind.get('rsi') is not None,
                 f"rsi={day_ind.get('rsi')}")
            test("day indicators has ATR", day_ind.get('atr') is not None,
                 f"atr={day_ind.get('atr')}")
            test("day indicators has impulse", day_ind.get('impulse_color') is not None,
                 f"impulse={day_ind.get('impulse_color')}")
            test("day indicators has KC upper", day_ind.get('kc_upper') is not None,
                 f"kc_upper={day_ind.get('kc_upper')}")
    else:
        test("indicators returned", False, "No indicators data")

    # 5c. Get watchlist with indicators populated
    resp = client.get('/api/v2/trading-watchlist')
    data = resp.get_json()
    if data.get('data') and len(data['data']) > 0:
        first = data['data'][0]
        test("watchlist row has day_rsi", first.get('day_rsi') is not None,
             f"day_rsi={first.get('day_rsi')}")
        test("watchlist row has day_impulse", first.get('day_impulse') is not None,
             f"day_impulse={first.get('day_impulse')}")
        test("watchlist row has day_atr", first.get('day_atr') is not None,
             f"day_atr={first.get('day_atr')}")
        test("watchlist row has day_kc_upper", first.get('day_kc_upper') is not None,
             f"day_kc_upper={first.get('day_kc_upper')}")
        test("watchlist row has LTP",
             first.get('ltp') is not None and first.get('ltp') > 0,
             f"ltp={first.get('ltp')}")
    else:
        test("watchlist has data rows with indicators", False, "No data")

    # ──────────────────────────────────────────────────────────────────
    # TEST 6: Live CMP & Stock ATR Endpoints (Trade Bill dependencies)
    # ──────────────────────────────────────────────────────────────────
    print("\n── Test 6: Trade Bill Dependencies ──")

    # 6a. Live CMP — bare symbol
    resp = client.get('/api/v2/live-cmp/RELIANCE')
    test("GET /live-cmp/RELIANCE returns 200", resp.status_code == 200)
    data = resp.get_json()
    test("live-cmp has price", data.get('ltp') is not None or data.get('cmp') is not None,
         f"Data: {data}")

    # 6b. Stock ATR — bare symbol
    resp = client.get('/api/v2/stock-atr/RELIANCE')
    test("GET /stock-atr/RELIANCE returns 200", resp.status_code == 200)

    # 6c. Candle pattern — bare symbol
    resp = client.get('/api/v2/candle-pattern/RELIANCE')
    test("GET /candle-pattern/RELIANCE returns 200", resp.status_code == 200)

    # ──────────────────────────────────────────────────────────────────
    # TEST 7: Symbol Normalization in DB
    # ──────────────────────────────────────────────────────────────────
    print("\n── Test 7: Symbol Normalization Verification ──")

    # Verify no NSE: prefixed symbols in intraday tables
    with app.app_context():
        from models.database import get_database
        db = get_database()
        conn = db.get_connection()

        # Check intraday_ohlcv
        try:
            row = conn.execute('''
                SELECT COUNT(*) as cnt FROM intraday_ohlcv WHERE symbol LIKE 'NSE:%'
            ''').fetchone()
            nse_count = row['cnt'] if row else 0
            test("intraday_ohlcv has no NSE: prefixed symbols",
                 nse_count == 0,
                 f"Found {nse_count} rows with NSE: prefix")
        except Exception as e:
            test("intraday_ohlcv check", False, str(e))

        # Check intraday_indicators
        try:
            row = conn.execute('''
                SELECT COUNT(*) as cnt FROM intraday_indicators WHERE symbol LIKE 'NSE:%'
            ''').fetchone()
            nse_count = row['cnt'] if row else 0
            test("intraday_indicators has no NSE: prefixed symbols",
                 nse_count == 0,
                 f"Found {nse_count} rows with NSE: prefix")
        except Exception as e:
            test("intraday_indicators check", False, str(e))

        # Check stock_alerts
        try:
            row = conn.execute('''
                SELECT COUNT(*) as cnt FROM stock_alerts WHERE symbol LIKE 'NSE:%'
            ''').fetchone()
            nse_count = row['cnt'] if row else 0
            test("stock_alerts has no NSE: prefixed symbols",
                 nse_count == 0,
                 f"Found {nse_count} rows with NSE: prefix")
        except Exception as e:
            test("stock_alerts check", False, str(e))

        conn.close()

    # ──────────────────────────────────────────────────────────────────
    # Cleanup: Remove test data from watchlist
    # ──────────────────────────────────────────────────────────────────
    print("\n── Cleanup ──")
    resp = client.delete('/api/v2/trading-watchlist/remove-symbol/RELIANCE')
    test("cleanup: remove RELIANCE from watchlist", resp.status_code == 200)

    # ──────────────────────────────────────────────────────────────────
    # RESULTS
    # ──────────────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print(f"  RESULTS: {PASSED} passed, {FAILED} failed out of {PASSED + FAILED} tests")
    print("=" * 70)

    if ERRORS:
        print("\n  FAILURES:")
        for e in ERRORS:
            print(f"  {e}")

    print()
    return FAILED == 0


if __name__ == '__main__':
    success = run_tests()
    sys.exit(0 if success else 1)
