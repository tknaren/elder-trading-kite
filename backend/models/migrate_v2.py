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
