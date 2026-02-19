"""
Live Integration Tests — Bug Fix Verification
================================================

Tests against the RUNNING application at http://localhost:5001
which has an active Kite session.

Run: python test_live.py
"""

import sys
import io
import json
import urllib.request
import urllib.error
import time

# Force UTF-8 output on Windows
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

BASE = 'http://localhost:5001/api/v2'

PASSED = 0
FAILED = 0
ERRORS = []


def test(name, passed, detail=""):
    global PASSED, FAILED, ERRORS
    if passed:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED += 1
        msg = f"  [FAIL] {name}" + (f" -- {detail}" if detail else "")
        print(msg)
        ERRORS.append(msg)


def get(path, timeout=15):
    try:
        resp = urllib.request.urlopen(f'{BASE}{path}', timeout=timeout)
        return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {'_http_error': e.code, '_body': e.read().decode()}
    except Exception as e:
        return {'_error': str(e)}


def post(path, data=None, timeout=30):
    body = json.dumps(data or {}).encode()
    req = urllib.request.Request(
        f'{BASE}{path}',
        data=body,
        headers={'Content-Type': 'application/json'},
        method='POST'
    )
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {'_http_error': e.code, '_body': e.read().decode()}
    except Exception as e:
        return {'_error': str(e)}


def delete(path, timeout=10):
    req = urllib.request.Request(f'{BASE}{path}', method='DELETE')
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {'_http_error': e.code, '_body': e.read().decode()}
    except Exception as e:
        return {'_error': str(e)}


def run_tests():
    print("\n" + "=" * 70)
    print("  LIVE INTEGRATION TESTS -- Bug Fix Verification")
    print("  Server: http://localhost:5001")
    print("=" * 70)

    # ---- Test 1: Instruments Search ----
    print("\n-- Test 1: Instruments Search --")

    data = get('/instruments/search?q=RELIANCE&limit=5')
    is_list = isinstance(data, list)
    test("instruments/search returns array", is_list)
    if is_list and len(data) > 0:
        test("instruments/search finds RELIANCE",
             any('RELIANCE' in str(d) for d in data))
    else:
        test("instruments/search finds RELIANCE", False,
             "Empty result -- instruments may need loading")

    data = get('/instruments/search?q=TCS&limit=5')
    test("instruments/search for TCS returns data",
         isinstance(data, list),
         f"Got: {type(data)}")

    # ---- Test 2: Watchlist CRUD with bare symbols ----
    print("\n-- Test 2: Trading Watchlist CRUD --")

    data = get('/trading-watchlist')
    test("GET /trading-watchlist returns success", data.get('success') == True)

    # Add symbol (bare)
    data = post('/trading-watchlist/add-symbol', {'symbol': 'HDFCBANK'})
    test("add-symbol HDFCBANK returns success", data.get('success') == True)
    if data.get('symbols'):
        test("HDFCBANK stored as bare (no NSE: prefix)",
             'HDFCBANK' in data['symbols'] and 'NSE:HDFCBANK' not in data['symbols'],
             f"Symbols: {data['symbols']}")

    # Add symbol with NSE: prefix (should be stripped)
    data = post('/trading-watchlist/add-symbol', {'symbol': 'NSE:INFY'})
    test("add-symbol NSE:INFY returns success", data.get('success') == True)
    if data.get('symbols'):
        test("NSE:INFY stored as bare INFY",
             'INFY' in data['symbols'] and 'NSE:INFY' not in data['symbols'],
             f"Symbols: {data['symbols']}")

    # Get watchlist with live LTP
    data = get('/trading-watchlist')
    test("GET /trading-watchlist has data", isinstance(data.get('data'), list))
    if data.get('data') and len(data['data']) > 0:
        first = data['data'][0]
        test("watchlist row has bare symbol",
             'NSE:' not in (first.get('symbol') or ''),
             f"Got: {first.get('symbol')}")
        test("watchlist row has LTP from Kite",
             first.get('ltp') is not None and first.get('ltp') > 0,
             f"ltp={first.get('ltp')}")

    # Remove test symbols
    delete('/trading-watchlist/remove-symbol/HDFCBANK')
    delete('/trading-watchlist/remove-symbol/INFY')

    # ---- Test 3: Alert System ----
    print("\n-- Test 3: Alert System --")

    alert_data = {
        'symbol': 'SBIN',
        'alert_name': 'Live Test Alert',
        'direction': 'LONG',
        'condition_operator': '<=',
        'condition_value': 500.0,
        'condition_type': 'price_level',
        'timeframe': '15min',
        'cooldown_minutes': 60,
        'auto_trade': False,
        'candle_confirm': False,
    }
    data = post('/alerts', alert_data)
    test("create alert with bare symbol returns success",
         data.get('success') == True,
         f"Response: {data}")
    alert_id = data.get('id')

    # Create alert with NSE: prefix
    alert_data2 = {**alert_data, 'symbol': 'NSE:TCS', 'alert_name': 'NSE Prefix Test'}
    data = post('/alerts', alert_data2)
    test("create alert with NSE:TCS returns success", data.get('success') == True)
    alert_id2 = data.get('id')

    # Get alerts and verify bare symbols
    data = get('/alerts')
    if data.get('alerts'):
        test_alerts = [a for a in data['alerts']
                       if a.get('alert_name') in ('Live Test Alert', 'NSE Prefix Test')]
        for ta in test_alerts:
            sym = ta.get('symbol', '')
            test(f"alert '{ta['alert_name']}' stored as bare symbol",
                 'NSE:' not in sym,
                 f"Symbol: {sym}")

    # Cleanup
    if alert_id:
        delete(f'/alerts/{alert_id}')
    if alert_id2:
        delete(f'/alerts/{alert_id2}')

    # ---- Test 4: Engine Status ----
    print("\n-- Test 4: Engine Status --")

    data = get('/engine/status')
    test("engine status returns success", data.get('success') == True)
    test("engine has running field", 'running' in data)

    # Check notifications
    data = get('/engine/notifications')
    notifs = data.get('notifications', [])
    engine_start_spam = [n for n in notifs
                         if 'Engine Started' in n.get('title', '') and n.get('type') == 'info']
    # There should be at most 1 "Engine Started" notification (from manual start)
    test("no Engine Started notification spam",
         len(engine_start_spam) <= 1,
         f"Found {len(engine_start_spam)} Engine Started notifications")

    # ---- Test 5: Timeframe Refresh with Live Kite ----
    print("\n-- Test 5: Timeframe Data Refresh (Live Kite) --")

    # First add RELIANCE to watchlist
    post('/trading-watchlist/add-symbol', {'symbol': 'RELIANCE'})

    # Refresh data for single symbol
    print("  (Refreshing RELIANCE data -- this calls Kite API, may take 5-10s...)")
    data = post('/timeframe/refresh', {'symbol': 'RELIANCE'}, timeout=60)
    test("timeframe refresh returns success",
         data.get('success') == True,
         f"Response: {json.dumps(data)[:200]}")

    if data.get('success'):
        result = data.get('result', {})
        test("refresh returns bare symbol 'RELIANCE'",
             result.get('symbol') == 'RELIANCE',
             f"Got: {result.get('symbol')}")
        test("15min candles fetched", result.get('15min') == True,
             f"15min={result.get('15min')}")
        test("75min candles aggregated", result.get('75min') == True,
             f"75min={result.get('75min')}")
        test("daily candles fetched", result.get('day') == True,
             f"day={result.get('day')}")

        # Now check indicators
        data = get('/timeframe/indicators/RELIANCE')
        test("GET /timeframe/indicators/RELIANCE returns success",
             data.get('success') == True)

        if data.get('indicators'):
            ind = data['indicators']
            test("has day indicators", 'day' in ind)
            test("has 75min indicators", '75min' in ind)
            test("has 15min indicators", '15min' in ind)

            if 'day' in ind:
                d = ind['day']
                test("day RSI populated", d.get('rsi') is not None,
                     f"rsi={d.get('rsi')}")
                test("day ATR populated", d.get('atr') is not None,
                     f"atr={d.get('atr')}")
                test("day impulse populated", d.get('impulse_color') is not None,
                     f"impulse={d.get('impulse_color')}")
                test("day KC upper populated", d.get('kc_upper') is not None,
                     f"kc_upper={d.get('kc_upper')}")

        # Check watchlist now shows indicators
        data = get('/trading-watchlist')
        if data.get('data'):
            rel_row = next((r for r in data['data'] if r.get('symbol') == 'RELIANCE'), None)
            if rel_row:
                test("watchlist RELIANCE has day_rsi",
                     rel_row.get('day_rsi') is not None,
                     f"day_rsi={rel_row.get('day_rsi')}")
                test("watchlist RELIANCE has day_impulse",
                     rel_row.get('day_impulse') is not None,
                     f"day_impulse={rel_row.get('day_impulse')}")
                test("watchlist RELIANCE has day_atr",
                     rel_row.get('day_atr') is not None,
                     f"day_atr={rel_row.get('day_atr')}")
                test("watchlist RELIANCE has LTP",
                     rel_row.get('ltp') is not None and rel_row.get('ltp') > 0,
                     f"ltp={rel_row.get('ltp')}")
            else:
                test("watchlist contains RELIANCE row", False, "Not found in data")
    else:
        print("  (Skipping indicator tests -- refresh failed, Kite may not be authenticated)")

    # ---- Test 6: Trade Bill Dependencies ----
    print("\n-- Test 6: Trade Bill Dependencies --")

    # Live CMP
    data = get('/live-cmp/RELIANCE')
    test("live-cmp/RELIANCE returns data",
         data.get('ltp') is not None or data.get('cmp') is not None,
         f"Data: {data}")

    # Stock ATR
    data = get('/stock-atr/RELIANCE')
    test("stock-atr/RELIANCE returns 200",
         '_http_error' not in data,
         f"Data: {str(data)[:150]}")

    # Candle pattern
    data = get('/candle-pattern/RELIANCE')
    test("candle-pattern/RELIANCE returns 200",
         '_http_error' not in data,
         f"Data: {str(data)[:150]}")

    # Cleanup: keep RELIANCE in watchlist (user had it)

    # ---- RESULTS ----
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
