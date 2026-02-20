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
    # TEST 8: Trade Journal CRUD (SCOPE_IDENTITY fix)
    # ──────────────────────────────────────────────────────────────────
    print("\n── Test 8: Trade Journal CRUD (OUTPUT INSERTED.id fix) ──")

    # 8a. Create a new trade journal
    journal_data = {
        'ticker': 'TESTSTOCK',
        'journal_date': '2024-01-15',
        'direction': 'Long',
        'strategy': 'EL - Elder System',
        'stop_loss': 100.0,
        'target_price': 120.0,
        'entry_tactic': 'Limit Order',
        'entry_reason': 'Test entry'
    }
    resp = client.post('/api/v2/trade-journal',
                       data=json.dumps(journal_data),
                       content_type='application/json')
    test("POST /trade-journal returns 201", resp.status_code == 201)
    data = resp.get_json()
    test("create journal returns success", data.get('success') == True)
    journal_id = data.get('id')
    test("create journal returns valid id (OUTPUT INSERTED.id works)",
         journal_id is not None and isinstance(journal_id, int) and journal_id > 0,
         f"Got id: {journal_id}")

    if journal_id:
        # 8b. Add entry leg
        entry_data = {
            'entry_datetime': '2024-01-15T10:30:00',
            'quantity': 10,
            'order_price': 110.0,
            'filled_price': 110.25,
            'slippage': 0.25,
            'commission': 15.0,
            'position_size': 1102.50,
            'grade': 'A'
        }
        resp = client.post(f'/api/v2/trade-journal/{journal_id}/entry',
                           data=json.dumps(entry_data),
                           content_type='application/json')
        test("POST /trade-journal/{id}/entry returns 201", resp.status_code == 201)
        data = resp.get_json()
        test("add entry returns success", data.get('success') == True)
        entry_id = data.get('id')
        test("add entry returns valid id",
             entry_id is not None and isinstance(entry_id, int) and entry_id > 0,
             f"Got entry_id: {entry_id}")

        # 8c. Add exit leg
        exit_data = {
            'exit_datetime': '2024-01-16T14:00:00',
            'quantity': 10,
            'order_price': 118.0,
            'filled_price': 117.80,
            'slippage': 0.20,
            'commission': 15.0,
            'position_size': 1178.00,
            'grade': 'B'
        }
        resp = client.post(f'/api/v2/trade-journal/{journal_id}/exit',
                           data=json.dumps(exit_data),
                           content_type='application/json')
        test("POST /trade-journal/{id}/exit returns 201", resp.status_code == 201)
        data = resp.get_json()
        test("add exit returns success", data.get('success') == True)
        exit_id = data.get('id')
        test("add exit returns valid id",
             exit_id is not None and isinstance(exit_id, int) and exit_id > 0,
             f"Got exit_id: {exit_id}")

        # 8d. Get journal and verify totals recalculated
        resp = client.get(f'/api/v2/trade-journal/{journal_id}')
        test("GET /trade-journal/{id} returns 200", resp.status_code == 200)
        data = resp.get_json()
        test("journal has entries", len(data.get('entries', [])) > 0)
        test("journal has exits", len(data.get('exits', [])) > 0)
        test("journal avg_entry calculated", data.get('avg_entry') is not None and data.get('avg_entry') > 0,
             f"avg_entry={data.get('avg_entry')}")
        test("journal status auto-closed", data.get('status') == 'closed',
             f"status={data.get('status')}")
        test("journal gain_loss_amount calculated", data.get('gain_loss_amount') is not None,
             f"gain_loss={data.get('gain_loss_amount')}")

        # 8e. Delete the test journal
        resp = client.delete(f'/api/v2/trade-journal/{journal_id}')
        test("DELETE /trade-journal/{id} returns 200", resp.status_code == 200)
    else:
        # Skip dependent tests
        for _ in range(11):
            test("trade journal dependent test", False, "Skipped — journal creation failed")

    # ──────────────────────────────────────────────────────────────────
    # TEST 9: Load Market Data Endpoint
    # ──────────────────────────────────────────────────────────────────
    print("\n── Test 9: Load Market Data ──")

    # 9a. Data load endpoint exists and doesn't crash (401 = no Kite auth, which is fine)
    resp = client.post('/api/v2/data/load',
                       data=json.dumps({}),
                       content_type='application/json')
    test("POST /data/load does not return 500",
         resp.status_code != 500,
         f"Status: {resp.status_code}")

    # 9b. Data status endpoint
    resp = client.get('/api/v2/data/status')
    test("GET /data/status returns 200", resp.status_code == 200)
    data = resp.get_json()
    test("data/status has symbols_cached field", 'symbols_cached' in data)

    # ──────────────────────────────────────────────────────────────────
    # Test 10: CMP Endpoint (always live from Kite)
    # ──────────────────────────────────────────────────────────────────
    print("\n── Test 10: Live CMP Endpoint ──")

    resp = client.get('/api/v2/live-cmp/RELIANCE')
    test("GET /live-cmp/RELIANCE returns 200 or 404",
         resp.status_code in (200, 404),
         f"Status: {resp.status_code}")
    data = resp.get_json()
    if resp.status_code == 200:
        test("CMP response has 'cmp' field", 'cmp' in data)
        test("CMP response has 'source' field", 'source' in data)
        test("CMP source is 'live' or 'cache'",
             data.get('source') in ('live', 'cache'),
             f"Source: {data.get('source')}")
    else:
        test("CMP 404 has error message", 'error' in data)

    # ──────────────────────────────────────────────────────────────────
    # Test 11: GTT Order Endpoint (parameter validation)
    # ──────────────────────────────────────────────────────────────────
    print("\n── Test 11: GTT Order Parameter Validation ──")

    # Test missing quantity
    resp = client.post('/api/v2/kite/gtt',
                       data=json.dumps({
                           'symbol': 'RELIANCE',
                           'trigger_price': 2500,
                           'limit_price': 2510
                       }),
                       content_type='application/json')
    test("GTT with qty=0 returns 400", resp.status_code == 400)

    # Test accepts both 'price' and 'limit_price'
    resp = client.post('/api/v2/kite/gtt',
                       data=json.dumps({
                           'symbol': 'RELIANCE',
                           'transaction_type': 'BUY',
                           'trigger_price': 2500,
                           'price': 2510,  # Frontend sends 'price'
                           'quantity': 10
                       }),
                       content_type='application/json')
    # Should NOT return 400 for missing limit_price (accepts 'price' as fallback)
    test("GTT accepts 'price' instead of 'limit_price'",
         resp.status_code != 400 or 'limit_price' not in (resp.get_json() or {}).get('error', ''),
         f"Status: {resp.status_code}")

    # Test NRML order endpoint
    resp = client.post('/api/v2/kite/orders/nrml',
                       data=json.dumps({
                           'symbol': 'RELIANCE',
                           'transaction_type': 'BUY',
                           'quantity': 10,
                           'price': 2500,
                           'order_type': 'LIMIT'
                       }),
                       content_type='application/json')
    test("NRML order endpoint responds (not 500)",
         resp.status_code != 500,
         f"Status: {resp.status_code}")

    # ──────────────────────────────────────────────────────────────────
    # Test 12: Portfolio Context Endpoint
    # ──────────────────────────────────────────────────────────────────
    print("\n── Test 12: Portfolio Context ──")

    resp = client.get('/api/v2/portfolio/context')
    test("GET /portfolio/context returns 200", resp.status_code == 200)
    data = resp.get_json()
    test("portfolio has 'positions' array", 'positions' in data and isinstance(data['positions'], list))
    test("portfolio has 'holdings' array", 'holdings' in data and isinstance(data['holdings'], list))
    test("portfolio has 'summary' object", 'summary' in data and isinstance(data['summary'], dict))
    summary = data.get('summary', {})
    test("summary has trading_capital", 'trading_capital' in summary)
    test("summary has available_capital", 'available_capital' in summary)
    test("summary has capital_used_pct", 'capital_used_pct' in summary)

    # ──────────────────────────────────────────────────────────────────
    # Test 13: Orders History Endpoint (with GTT)
    # ──────────────────────────────────────────────────────────────────
    print("\n── Test 13: Orders History with GTT ──")

    resp = client.get('/api/v2/orders/history')
    test("GET /orders/history returns 200", resp.status_code == 200)
    data = resp.get_json()
    test("orders history has 'pending' array", 'pending' in data)
    test("orders history has 'executed' array", 'executed' in data)
    test("orders history has 'gtt_orders' array", 'gtt_orders' in data)
    test("orders history has 'summary' object", 'summary' in data)
    summary = data.get('summary', {})
    test("summary has gtt_count", 'gtt_count' in summary)

    # ──────────────────────────────────────────────────────────────────
    # Test 14: Sync All Endpoint
    # ──────────────────────────────────────────────────────────────────
    print("\n── Test 14: Sync All Endpoint ──")

    resp = client.post('/api/v2/sync/all',
                       data=json.dumps({}),
                       content_type='application/json')
    test("POST /sync/all returns 400 (no Kite auth) or 200",
         resp.status_code in (200, 400),
         f"Status: {resp.status_code}")
    data = resp.get_json()
    if resp.status_code == 400:
        test("sync/all error mentions Kite", 'kite' in (data.get('error', '') or '').lower() or 'connected' in (data.get('error', '') or '').lower())

    # ──────────────────────────────────────────────────────────────────
    # Test 15: Database Schema Check (cache tables)
    # ──────────────────────────────────────────────────────────────────
    print("\n── Test 15: Database Schema Verification ──")

    from models.database import get_database
    db_inst = get_database()
    conn = db_inst.get_connection()

    # Check kite_orders_cache has user_id column
    row = conn.execute("""
        SELECT COUNT(*) AS cnt FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_NAME = 'kite_orders_cache' AND COLUMN_NAME = 'user_id'
    """).fetchone()
    test("kite_orders_cache has user_id column", row['cnt'] > 0)

    # Check kite_orders_cache has tradingsymbol column
    row = conn.execute("""
        SELECT COUNT(*) AS cnt FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_NAME = 'kite_orders_cache' AND COLUMN_NAME = 'tradingsymbol'
    """).fetchone()
    test("kite_orders_cache has tradingsymbol column", row['cnt'] > 0)

    # Check kite_gtt_cache table exists
    row = conn.execute("""
        SELECT OBJECT_ID('kite_gtt_cache', 'U') AS obj_id
    """).fetchone()
    test("kite_gtt_cache table exists", row['obj_id'] is not None)

    # Check kite_positions_cache has user_id
    row = conn.execute("""
        SELECT COUNT(*) AS cnt FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_NAME = 'kite_positions_cache' AND COLUMN_NAME = 'user_id'
    """).fetchone()
    test("kite_positions_cache has user_id column", row['cnt'] > 0)

    # Check kite_holdings_cache has user_id
    row = conn.execute("""
        SELECT COUNT(*) AS cnt FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_NAME = 'kite_holdings_cache' AND COLUMN_NAME = 'user_id'
    """).fetchone()
    test("kite_holdings_cache has user_id column", row['cnt'] > 0)

    conn.close()

    # ──────────────────────────────────────────────────────────────────
    # Test 16: Trade Bill Create + Update (v1 API)
    # ──────────────────────────────────────────────────────────────────
    print("\n── Test 16: Trade Bill Create + Update ──")

    bill_data = {
        'ticker': 'RELIANCE',
        'current_market_price': 2500.0,
        'entry_price': 2450.0,
        'stop_loss': 2400.0,
        'target_price': 2600.0,
        'quantity': 10,
        'risk_per_share': 50.0,
        'position_size': 24500.0,
        'risk_percent': 2.0,
        'risk_amount_currency': 500.0,
        'reward_amount_currency': 1500.0,
        'risk_reward_ratio': 3.0,
        'break_even': 2450.0,
        'is_filled': True,
        'stop_entered': False,
        'target_entered': False,
        'journal_entered': False,
        'comments': 'Test trade bill',
        'atr': 45.5,
        'candle_pattern': 'Bullish Engulfing',
        'candle_1_conviction': 'Strong',
        'candle_2_conviction': 'Weak'
    }

    resp = client.post('/api/trade-bills', json=bill_data)
    test("POST /trade-bills returns 201", resp.status_code == 201)
    data = resp.get_json()
    test("create trade bill returns success", data.get('success') == True)
    test("create trade bill returns id", data.get('id') is not None and data.get('id') > 0)
    bill_id = data.get('id')

    if bill_id:
        # Update the bill
        update_data = {
            'ticker': 'RELIANCE',
            'entry_price': 2460.0,
            'stop_loss': 2410.0,
            'is_filled': True,
            'stop_entered': True,
            'comments': 'Updated test bill'
        }
        resp = client.put(f'/api/trade-bills/{bill_id}', json=update_data)
        test("PUT /trade-bills returns 200", resp.status_code == 200)
        data = resp.get_json()
        test("update trade bill returns success", data.get('success') == True)

        # Get the bill to verify atr/candle columns saved
        resp = client.get(f'/api/trade-bills/{bill_id}')
        test("GET /trade-bills/{id} returns 200", resp.status_code == 200)
        bill = resp.get_json()
        test("trade bill has atr field", bill.get('atr') is not None)
        test("trade bill has candle_pattern field", bill.get('candle_pattern') == 'Bullish Engulfing')

        # Create journal from bill
        resp = client.post(f'/api/v2/trade-journal/from-bill/{bill_id}')
        test("POST /trade-journal/from-bill returns 201", resp.status_code == 201)
        jdata = resp.get_json()
        test("from-bill returns success", jdata.get('success') == True)
        test("from-bill returns journal id", jdata.get('id') is not None)

        # Cleanup
        if jdata.get('id'):
            client.delete(f'/api/v2/trade-journal/{jdata["id"]}')
        client.delete(f'/api/trade-bills/{bill_id}')

    # ──────────────────────────────────────────────────────────────────
    # Test 17: Order Cancel/Modify Endpoints Exist
    # ──────────────────────────────────────────────────────────────────
    print("\n── Test 17: Order Cancel/Modify Endpoints ──")

    # These will return 400/404 since no real Kite auth, but should NOT return 500/405
    resp = client.delete('/api/v2/kite/orders/fake-order-id')
    test("DELETE /kite/orders/{id} returns 400 (not 405/500)", resp.status_code in [200, 400, 404])

    resp = client.put('/api/v2/kite/orders/fake-order-id',
                      json={'quantity': 10, 'price': 100.0})
    test("PUT /kite/orders/{id} returns 400 (not 405/500)", resp.status_code in [200, 400, 404])

    resp = client.delete('/api/v2/kite/gtt/99999')
    test("DELETE /kite/gtt/{id} returns 400 (not 405/500)", resp.status_code in [200, 400, 404])

    # ──────────────────────────────────────────────────────────────────
    # Test 18: Instruments Search (Local DB Only)
    # ──────────────────────────────────────────────────────────────────
    print("\n── Test 18: Instruments Search (Local Only) ──")

    resp = client.get('/api/v2/instruments/search?q=RELIANCE&limit=5')
    test("instruments/search returns 200", resp.status_code == 200)
    data = resp.get_json()
    # Should return array or message about loading from Settings
    is_valid = isinstance(data, list) or (isinstance(data, dict) and 'message' in data)
    test("instruments/search returns array or load message", is_valid)

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
