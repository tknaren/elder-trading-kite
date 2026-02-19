"""
Elder Trading System - Database Migration v2 (SQL Server)
All schema is now created in database.py _init_db().
This module handles any incremental column additions for existing databases.
"""

import pyodbc


def _column_exists(cursor, table_name, column_name):
    """Check if a column exists in a table"""
    cursor.execute("""
        SELECT COUNT(*) AS cnt FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_NAME = ? AND COLUMN_NAME = ?
    """, (table_name, column_name))
    row = cursor.fetchone()
    return row[0] > 0


def migrate_database(connection_string: str = None):
    """Run database migrations to add new columns if missing"""

    if connection_string is None:
        from config import DatabaseConfig
        connection_string = DatabaseConfig.connection_string()

    conn = pyodbc.connect(connection_string, timeout=30)
    cursor = conn.cursor()

    success = 0
    skipped = 0
    errors = []

    # Define column additions: (table, column, sql_type_with_default)
    column_migrations = [
        ('trade_bills', 'order_id', 'NVARCHAR(100)'),
        ('trade_bills', 'signal_strength', 'INT'),
        ('trade_bills', 'grade', 'NVARCHAR(10)'),
        ('trade_bills', 'symbol', 'NVARCHAR(100)'),
        ('trade_bills', 'market', "NVARCHAR(10) DEFAULT 'IN'"),
        ('trade_bills', 'direction', "NVARCHAR(20) DEFAULT 'LONG'"),
        ('trade_bills', 'risk_amount', 'FLOAT'),
        ('trade_bills', 'position_value', 'FLOAT'),
        ('trade_bills', 'max_risk', 'FLOAT'),
        ('trade_bills', 'other_charges', 'FLOAT DEFAULT 0'),
        ('trade_log', 'ibkr_order_id', 'NVARCHAR(100)'),
        ('trade_log', 'ibkr_execution_id', 'NVARCHAR(100)'),
        ('trade_log', 'trade_bill_id', 'INT'),
        ('trade_log', 'synced_from_ibkr', 'BIT DEFAULT 0'),
        ('weekly_scans', 'screener_version', "NVARCHAR(20) DEFAULT '1.0'"),
        ('daily_scans', 'screener_version', "NVARCHAR(20) DEFAULT '1.0'"),
        ('account_settings', 'kite_api_key', 'NVARCHAR(200)'),
        ('account_settings', 'kite_api_secret', 'NVARCHAR(200)'),
        ('account_settings', 'kite_access_token', 'NVARCHAR(500)'),
        ('account_settings', 'kite_token_expiry', 'NVARCHAR(50)'),
        ('account_settings', 'last_data_refresh', 'NVARCHAR(50)'),
    ]

    for table, column, sql_type in column_migrations:
        try:
            if not _column_exists(cursor, table, column):
                cursor.execute(f"ALTER TABLE {table} ADD {column} {sql_type}")
                success += 1
            else:
                skipped += 1
        except pyodbc.Error as e:
            error_msg = str(e)
            if 'duplicate' in error_msg.lower() or 'already exists' in error_msg.lower():
                skipped += 1
            else:
                errors.append(f"{table}.{column} -> {error_msg}")

    conn.commit()

    # ── Data cleanup: Strip NSE: prefix from all symbol columns ──
    # All trades are NSE-only, so we store bare symbols (e.g., 'RELIANCE' not 'NSE:RELIANCE')
    # Kite API functions add the prefix internally when needed.
    data_cleanups = [
        ("UPDATE intraday_ohlcv SET symbol = REPLACE(symbol, 'NSE:', '') WHERE symbol LIKE 'NSE:%'",
         "intraday_ohlcv"),
        ("UPDATE intraday_indicators SET symbol = REPLACE(symbol, 'NSE:', '') WHERE symbol LIKE 'NSE:%'",
         "intraday_indicators"),
        ("UPDATE stock_alerts SET symbol = REPLACE(symbol, 'NSE:', '') WHERE symbol LIKE 'NSE:%'",
         "stock_alerts"),
        ("UPDATE alert_history SET symbol = REPLACE(symbol, 'NSE:', '') WHERE symbol LIKE 'NSE:%'",
         "alert_history"),
    ]

    cleanup_count = 0
    for sql, table_name in data_cleanups:
        try:
            cursor.execute(sql)
            rows_affected = cursor.rowcount
            if rows_affected > 0:
                cleanup_count += rows_affected
        except pyodbc.Error:
            pass  # Table may not exist yet

    # Also normalize watchlist JSON arrays to strip NSE: prefix
    try:
        cursor.execute("SELECT id, symbols FROM watchlists WHERE symbols LIKE '%NSE:%'")
        import json as _json
        for row in cursor.fetchall():
            wl_id = row[0]
            raw = _json.loads(row[1]) if row[1] else []
            cleaned = list(dict.fromkeys(
                s.replace('NSE:', '').strip().upper() for s in raw if s
            ))
            cursor.execute("UPDATE watchlists SET symbols = ? WHERE id = ?",
                           (_json.dumps(cleaned), wl_id))
            cleanup_count += 1
    except pyodbc.Error:
        pass

    if cleanup_count > 0:
        conn.commit()
        print(f"Data cleanup: {cleanup_count} rows normalized (NSE: prefix removed)")

    conn.close()

    print(f"Migration complete: {success} applied, {skipped} skipped, {len(errors)} errors")
    if errors:
        print("Errors:")
        for e in errors:
            print(f"  - {e}")

    return {
        'success': success,
        'skipped': skipped,
        'errors': errors
    }


if __name__ == '__main__':
    result = migrate_database()
    print(f"\nMigration result: {result}")
