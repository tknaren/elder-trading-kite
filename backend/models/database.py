"""
Elder Trading System - Database Models
SQL Server database operations and schema management via pyodbc
"""

import pyodbc
import json
import os
from datetime import datetime
from typing import Optional, List, Dict


class DictRow:
    """Wrapper that makes pyodbc rows behave like sqlite3.Row (dict-like access)"""

    def __init__(self, cursor_description, row):
        self._columns = [col[0] for col in cursor_description]
        self._row = row

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._row[key]
        try:
            idx = self._columns.index(key)
            return self._row[idx]
        except ValueError:
            raise KeyError(key)

    def __contains__(self, key):
        return key in self._columns

    def keys(self):
        return self._columns

    def values(self):
        return list(self._row)

    def items(self):
        return list(zip(self._columns, self._row))

    def __iter__(self):
        return iter(self._columns)

    def __len__(self):
        return len(self._columns)

    def __repr__(self):
        return repr(dict(self))


class DictCursor:
    """Wraps pyodbc cursor to return DictRow objects"""

    def __init__(self, cursor):
        self._cursor = cursor
        self.lastrowid = None

    def execute(self, sql, params=None):
        if params:
            self._cursor.execute(sql, params)
        else:
            self._cursor.execute(sql)
        return self

    def fetchone(self):
        row = self._cursor.fetchone()
        if row is None:
            return None
        return DictRow(self._cursor.description, row)

    def fetchall(self):
        rows = self._cursor.fetchall()
        if not rows:
            return []
        desc = self._cursor.description
        return [DictRow(desc, row) for row in rows]

    @property
    def rowcount(self):
        return self._cursor.rowcount

    @property
    def description(self):
        return self._cursor.description

    def close(self):
        self._cursor.close()


class DictConnection:
    """Wraps pyodbc connection to produce DictCursor and provide sqlite3-like API"""

    def __init__(self, conn):
        self._conn = conn

    def cursor(self):
        return DictCursor(self._conn.cursor())

    def execute(self, sql, params=None):
        cursor = DictCursor(self._conn.cursor())
        cursor.execute(sql, params)
        return cursor

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


class Database:
    """Database connection manager for SQL Server"""

    def __init__(self, connection_string: str = None):
        if connection_string is None:
            from config import DatabaseConfig
            connection_string = DatabaseConfig.connection_string()

        self.connection_string = connection_string
        self._init_db()

    def get_connection(self):
        """Get database connection with dict-row support"""
        conn = pyodbc.connect(self.connection_string, timeout=30)
        return DictConnection(conn)

    def _init_db(self):
        """Initialize database schema"""
        conn = self.get_connection()

        # Users table
        conn.execute("""
            IF OBJECT_ID('users', 'U') IS NULL
            CREATE TABLE users (
                id INT IDENTITY(1,1) PRIMARY KEY,
                username NVARCHAR(200) NOT NULL UNIQUE,
                password_hash NVARCHAR(500) NOT NULL,
                created_at DATETIME2 DEFAULT GETDATE()
            )
        """)

        # Account settings
        conn.execute("""
            IF OBJECT_ID('account_settings', 'U') IS NULL
            CREATE TABLE account_settings (
                id INT IDENTITY(1,1) PRIMARY KEY,
                user_id INT NOT NULL,
                account_name NVARCHAR(200) NOT NULL,
                market NVARCHAR(10) DEFAULT 'IN',
                trading_capital FLOAT NOT NULL,
                risk_per_trade FLOAT DEFAULT 2.0,
                max_monthly_drawdown FLOAT DEFAULT 6.0,
                target_rr FLOAT DEFAULT 2.0,
                max_open_positions INT DEFAULT 5,
                currency NVARCHAR(10) DEFAULT 'INR',
                broker NVARCHAR(100) DEFAULT 'Zerodha',
                kite_api_key NVARCHAR(200),
                kite_api_secret NVARCHAR(200),
                kite_access_token NVARCHAR(500),
                kite_token_expiry NVARCHAR(50),
                last_data_refresh NVARCHAR(50),
                created_at DATETIME2 DEFAULT GETDATE(),
                updated_at DATETIME2 DEFAULT GETDATE(),
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)

        # Strategies
        conn.execute("""
            IF OBJECT_ID('strategies', 'U') IS NULL
            CREATE TABLE strategies (
                id INT IDENTITY(1,1) PRIMARY KEY,
                user_id INT NOT NULL,
                name NVARCHAR(200) NOT NULL,
                description NVARCHAR(MAX),
                is_active BIT DEFAULT 1,
                config NVARCHAR(MAX) NOT NULL,
                created_at DATETIME2 DEFAULT GETDATE(),
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)

        # APGAR parameters
        conn.execute("""
            IF OBJECT_ID('apgar_parameters', 'U') IS NULL
            CREATE TABLE apgar_parameters (
                id INT IDENTITY(1,1) PRIMARY KEY,
                strategy_id INT NOT NULL,
                parameter_name NVARCHAR(200) NOT NULL,
                parameter_label NVARCHAR(200) NOT NULL,
                options NVARCHAR(MAX) NOT NULL,
                display_order INT DEFAULT 0,
                FOREIGN KEY (strategy_id) REFERENCES strategies(id)
            )
        """)

        # Weekly scans
        conn.execute("""
            IF OBJECT_ID('weekly_scans', 'U') IS NULL
            CREATE TABLE weekly_scans (
                id INT IDENTITY(1,1) PRIMARY KEY,
                user_id INT NOT NULL,
                market NVARCHAR(10) NOT NULL,
                scan_date DATE NOT NULL,
                week_start DATE NOT NULL,
                week_end DATE NOT NULL,
                results NVARCHAR(MAX) NOT NULL,
                summary NVARCHAR(MAX),
                screener_version NVARCHAR(20) DEFAULT '1.0',
                created_at DATETIME2 DEFAULT GETDATE(),
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)

        # Daily scans
        conn.execute("""
            IF OBJECT_ID('daily_scans', 'U') IS NULL
            CREATE TABLE daily_scans (
                id INT IDENTITY(1,1) PRIMARY KEY,
                user_id INT NOT NULL,
                weekly_scan_id INT NOT NULL,
                market NVARCHAR(10) NOT NULL,
                scan_date DATE NOT NULL,
                results NVARCHAR(MAX) NOT NULL,
                screener_version NVARCHAR(20) DEFAULT '1.0',
                created_at DATETIME2 DEFAULT GETDATE(),
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (weekly_scan_id) REFERENCES weekly_scans(id)
            )
        """)

        # Trade setups
        conn.execute("""
            IF OBJECT_ID('trade_setups', 'U') IS NULL
            CREATE TABLE trade_setups (
                id INT IDENTITY(1,1) PRIMARY KEY,
                user_id INT NOT NULL,
                daily_scan_id INT,
                symbol NVARCHAR(100) NOT NULL,
                market NVARCHAR(10) NOT NULL,
                strategy_id INT,
                apgar_score INT,
                apgar_details NVARCHAR(MAX),
                entry_price FLOAT,
                stop_loss FLOAT,
                target_price FLOAT,
                position_size INT,
                risk_amount FLOAT,
                status NVARCHAR(50) DEFAULT 'pending',
                created_at DATETIME2 DEFAULT GETDATE(),
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)

        # Trade journal
        conn.execute("""
            IF OBJECT_ID('trade_journal', 'U') IS NULL
            CREATE TABLE trade_journal (
                id INT IDENTITY(1,1) PRIMARY KEY,
                user_id INT NOT NULL,
                trade_setup_id INT,
                symbol NVARCHAR(100) NOT NULL,
                market NVARCHAR(10) NOT NULL,
                direction NVARCHAR(20) DEFAULT 'LONG',
                entry_date DATE,
                entry_price FLOAT,
                exit_date DATE,
                exit_price FLOAT,
                position_size INT,
                stop_loss FLOAT,
                target_price FLOAT,
                pnl FLOAT,
                pnl_percent FLOAT,
                fees FLOAT DEFAULT 0,
                strategy_id INT,
                apgar_score INT,
                notes NVARCHAR(MAX),
                lessons_learned NVARCHAR(MAX),
                grade NVARCHAR(10),
                status NVARCHAR(50) DEFAULT 'open',
                created_at DATETIME2 DEFAULT GETDATE(),
                updated_at DATETIME2 DEFAULT GETDATE(),
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)

        # Daily checklist
        conn.execute("""
            IF OBJECT_ID('daily_checklist', 'U') IS NULL
            CREATE TABLE daily_checklist (
                id INT IDENTITY(1,1) PRIMARY KEY,
                user_id INT NOT NULL,
                checklist_date DATE NOT NULL,
                items NVARCHAR(MAX) NOT NULL,
                completed_at DATETIME2,
                FOREIGN KEY (user_id) REFERENCES users(id),
                CONSTRAINT UQ_checklist_user_date UNIQUE(user_id, checklist_date)
            )
        """)

        # Watchlists
        conn.execute("""
            IF OBJECT_ID('watchlists', 'U') IS NULL
            CREATE TABLE watchlists (
                id INT IDENTITY(1,1) PRIMARY KEY,
                user_id INT NOT NULL,
                name NVARCHAR(200) NOT NULL,
                market NVARCHAR(10) NOT NULL,
                symbols NVARCHAR(MAX) NOT NULL,
                is_default BIT DEFAULT 0,
                created_at DATETIME2 DEFAULT GETDATE(),
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)

        # Trade Bills
        conn.execute("""
            IF OBJECT_ID('trade_bills', 'U') IS NULL
            CREATE TABLE trade_bills (
                id INT IDENTITY(1,1) PRIMARY KEY,
                user_id INT NOT NULL,
                ticker NVARCHAR(100) NOT NULL,
                current_market_price FLOAT,
                entry_price FLOAT,
                stop_loss FLOAT,
                target_price FLOAT,
                quantity FLOAT,
                upper_channel FLOAT,
                lower_channel FLOAT,
                target_pips FLOAT,
                stop_loss_pips FLOAT,
                max_qty_for_risk FLOAT,
                overnight_charges FLOAT DEFAULT 0,
                other_charges FLOAT DEFAULT 0,
                max_risk FLOAT,
                risk_per_share FLOAT,
                position_size FLOAT,
                risk_percent FLOAT,
                channel_height FLOAT,
                potential_gain FLOAT,
                target_1_1_c FLOAT,
                target_1_2_b FLOAT,
                target_1_3_a FLOAT,
                risk_amount_currency FLOAT,
                reward_amount_currency FLOAT,
                risk_reward_ratio FLOAT,
                break_even FLOAT,
                trailing_stop FLOAT,
                is_filled BIT DEFAULT 0,
                stop_entered BIT DEFAULT 0,
                target_entered BIT DEFAULT 0,
                journal_entered BIT DEFAULT 0,
                comments NVARCHAR(MAX),
                status NVARCHAR(50) DEFAULT 'active',
                order_id NVARCHAR(100),
                signal_strength INT,
                grade NVARCHAR(10),
                symbol NVARCHAR(100),
                market NVARCHAR(10) DEFAULT 'IN',
                direction NVARCHAR(20) DEFAULT 'LONG',
                risk_amount FLOAT,
                position_value FLOAT,
                created_at DATETIME2 DEFAULT GETDATE(),
                updated_at DATETIME2 DEFAULT GETDATE(),
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)

        # Trade Log
        conn.execute("""
            IF OBJECT_ID('trade_log', 'U') IS NULL
            CREATE TABLE trade_log (
                id INT IDENTITY(1,1) PRIMARY KEY,
                user_id INT NOT NULL,
                entry_date NVARCHAR(50),
                symbol NVARCHAR(100) NOT NULL,
                strategy NVARCHAR(200),
                direction NVARCHAR(20) DEFAULT 'Long',
                entry_price FLOAT,
                shares INT,
                stop_loss FLOAT,
                take_profit FLOAT,
                exit_date NVARCHAR(50),
                exit_price FLOAT,
                trade_costs FLOAT DEFAULT 0,
                gross_pnl FLOAT,
                net_pnl FLOAT,
                planned_rrr FLOAT,
                actual_rrr FLOAT,
                r_value FLOAT,
                account_change_pct FLOAT,
                mistake NVARCHAR(MAX),
                discipline_rating INT DEFAULT 8,
                notes NVARCHAR(MAX),
                status NVARCHAR(50) DEFAULT 'open',
                ibkr_order_id NVARCHAR(100),
                ibkr_execution_id NVARCHAR(100),
                trade_bill_id INT,
                synced_from_ibkr BIT DEFAULT 0,
                created_at DATETIME2 DEFAULT GETDATE(),
                updated_at DATETIME2 DEFAULT GETDATE(),
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)

        # Indicator Filters Config
        conn.execute("""
            IF OBJECT_ID('indicator_filters', 'U') IS NULL
            CREATE TABLE indicator_filters (
                id INT IDENTITY(1,1) PRIMARY KEY,
                user_id INT NOT NULL UNIQUE,
                config NVARCHAR(MAX) NOT NULL,
                updated_at DATETIME2 DEFAULT GETDATE(),
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)

        # Favorite Stocks
        conn.execute("""
            IF OBJECT_ID('favorite_stocks', 'U') IS NULL
            CREATE TABLE favorite_stocks (
                id INT IDENTITY(1,1) PRIMARY KEY,
                user_id INT NOT NULL,
                symbol NVARCHAR(100) NOT NULL,
                market NVARCHAR(10) NOT NULL,
                notes NVARCHAR(MAX),
                created_at DATETIME2 DEFAULT GETDATE(),
                updated_at DATETIME2 DEFAULT GETDATE(),
                CONSTRAINT UQ_fav_user_symbol_market UNIQUE(user_id, symbol, market),
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)
        conn.execute("""
            IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_favorite_user_market')
            CREATE INDEX idx_favorite_user_market ON favorite_stocks(user_id, market)
        """)

        # Historical OHLCV data
        conn.execute("""
            IF OBJECT_ID('stock_historical_data', 'U') IS NULL
            CREATE TABLE stock_historical_data (
                id INT IDENTITY(1,1) PRIMARY KEY,
                symbol NVARCHAR(100) NOT NULL,
                date NVARCHAR(50) NOT NULL,
                [open] FLOAT NOT NULL,
                high FLOAT NOT NULL,
                low FLOAT NOT NULL,
                [close] FLOAT NOT NULL,
                volume BIGINT NOT NULL,
                created_at DATETIME2 DEFAULT GETDATE(),
                CONSTRAINT UQ_ohlcv_symbol_date UNIQUE(symbol, date)
            )
        """)
        conn.execute("""
            IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_symbol_date')
            CREATE INDEX idx_symbol_date ON stock_historical_data(symbol, date)
        """)

        # Cached Indicator Values (Daily)
        conn.execute("""
            IF OBJECT_ID('stock_indicators_daily', 'U') IS NULL
            CREATE TABLE stock_indicators_daily (
                id INT IDENTITY(1,1) PRIMARY KEY,
                symbol NVARCHAR(100) NOT NULL,
                date NVARCHAR(50) NOT NULL,
                [close] FLOAT NOT NULL,
                ema_22 FLOAT,
                ema_50 FLOAT,
                ema_100 FLOAT,
                ema_200 FLOAT,
                macd_line FLOAT,
                macd_signal FLOAT,
                macd_histogram FLOAT,
                rsi FLOAT,
                stochastic FLOAT,
                stoch_d FLOAT,
                atr FLOAT,
                force_index FLOAT,
                kc_upper FLOAT,
                kc_middle FLOAT,
                kc_lower FLOAT,
                created_at DATETIME2 DEFAULT GETDATE(),
                CONSTRAINT UQ_daily_symbol_date UNIQUE(symbol, date)
            )
        """)
        conn.execute("""
            IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_daily_symbol_date')
            CREATE INDEX idx_daily_symbol_date ON stock_indicators_daily(symbol, date)
        """)

        # Cached Indicator Values (Weekly)
        conn.execute("""
            IF OBJECT_ID('stock_indicators_weekly', 'U') IS NULL
            CREATE TABLE stock_indicators_weekly (
                id INT IDENTITY(1,1) PRIMARY KEY,
                symbol NVARCHAR(100) NOT NULL,
                week_end_date NVARCHAR(50) NOT NULL,
                [close] FLOAT NOT NULL,
                ema_22 FLOAT,
                ema_50 FLOAT,
                ema_100 FLOAT,
                ema_200 FLOAT,
                macd_line FLOAT,
                macd_signal FLOAT,
                macd_histogram FLOAT,
                created_at DATETIME2 DEFAULT GETDATE(),
                CONSTRAINT UQ_weekly_symbol_date UNIQUE(symbol, week_end_date)
            )
        """)
        conn.execute("""
            IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_weekly_symbol_date')
            CREATE INDEX idx_weekly_symbol_date ON stock_indicators_weekly(symbol, week_end_date)
        """)

        # Track indicator calculation progress
        conn.execute("""
            IF OBJECT_ID('stock_indicator_sync', 'U') IS NULL
            CREATE TABLE stock_indicator_sync (
                id INT IDENTITY(1,1) PRIMARY KEY,
                symbol NVARCHAR(100) NOT NULL UNIQUE,
                last_updated DATETIME2 DEFAULT GETDATE(),
                last_daily_date NVARCHAR(50),
                last_weekly_date NVARCHAR(50),
                daily_record_count INT DEFAULT 0,
                weekly_record_count INT DEFAULT 0,
                ohlcv_latest_date NVARCHAR(50)
            )
        """)

        # Track last update for each stock (OHLCV data)
        conn.execute("""
            IF OBJECT_ID('stock_data_sync', 'U') IS NULL
            CREATE TABLE stock_data_sync (
                id INT IDENTITY(1,1) PRIMARY KEY,
                symbol NVARCHAR(100) NOT NULL UNIQUE,
                last_updated DATETIME2 DEFAULT GETDATE(),
                earliest_date NVARCHAR(50),
                latest_date NVARCHAR(50),
                record_count INT DEFAULT 0
            )
        """)

        # Trade journal v2
        conn.execute("""
            IF OBJECT_ID('trade_journal_v2', 'U') IS NULL
            CREATE TABLE trade_journal_v2 (
                id INT IDENTITY(1,1) PRIMARY KEY,
                user_id INT NOT NULL,
                trade_bill_id INT,
                ticker NVARCHAR(100) NOT NULL,
                cmp FLOAT,
                direction NVARCHAR(20) DEFAULT 'Long',
                status NVARCHAR(50) DEFAULT 'open',
                journal_date DATE DEFAULT GETDATE(),
                remaining_qty INT DEFAULT 0,
                order_type NVARCHAR(50),
                mental_state NVARCHAR(200),
                entry_price FLOAT,
                quantity INT,
                target_price FLOAT,
                stop_loss FLOAT,
                rr_ratio FLOAT,
                potential_loss FLOAT,
                trailing_stop FLOAT,
                new_target FLOAT,
                potential_gain FLOAT,
                target_a FLOAT,
                target_b FLOAT,
                target_c FLOAT,
                entry_tactic NVARCHAR(MAX),
                entry_reason NVARCHAR(MAX),
                exit_tactic NVARCHAR(MAX),
                exit_reason NVARCHAR(MAX),
                first_entry_date NVARCHAR(50),
                last_exit_date NVARCHAR(50),
                total_shares INT,
                avg_entry FLOAT,
                avg_exit FLOAT,
                trade_grade NVARCHAR(10),
                gain_loss_percent FLOAT,
                gain_loss_amount FLOAT,
                high_during_trade FLOAT,
                low_during_trade FLOAT,
                max_drawdown FLOAT,
                percent_captured FLOAT,
                open_trade_comments NVARCHAR(MAX),
                followup_analysis NVARCHAR(MAX),
                created_at DATETIME2 DEFAULT GETDATE(),
                updated_at DATETIME2 DEFAULT GETDATE(),
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (trade_bill_id) REFERENCES trade_bills(id)
            )
        """)

        # Trade entries
        conn.execute("""
            IF OBJECT_ID('trade_entries', 'U') IS NULL
            CREATE TABLE trade_entries (
                id INT IDENTITY(1,1) PRIMARY KEY,
                journal_id INT NOT NULL,
                entry_datetime NVARCHAR(50),
                quantity INT,
                order_price FLOAT,
                filled_price FLOAT,
                slippage FLOAT,
                commission FLOAT,
                position_size FLOAT,
                day_high FLOAT,
                day_low FLOAT,
                grade NVARCHAR(10),
                notes NVARCHAR(MAX),
                created_at DATETIME2 DEFAULT GETDATE(),
                FOREIGN KEY (journal_id) REFERENCES trade_journal_v2(id) ON DELETE CASCADE
            )
        """)

        # Trade exits
        conn.execute("""
            IF OBJECT_ID('trade_exits', 'U') IS NULL
            CREATE TABLE trade_exits (
                id INT IDENTITY(1,1) PRIMARY KEY,
                journal_id INT NOT NULL,
                exit_datetime NVARCHAR(50),
                quantity INT,
                order_price FLOAT,
                filled_price FLOAT,
                slippage FLOAT,
                commission FLOAT,
                position_size FLOAT,
                day_high FLOAT,
                day_low FLOAT,
                grade NVARCHAR(10),
                notes NVARCHAR(MAX),
                created_at DATETIME2 DEFAULT GETDATE(),
                FOREIGN KEY (journal_id) REFERENCES trade_journal_v2(id) ON DELETE CASCADE
            )
        """)

        # Positions tracking
        conn.execute("""
            IF OBJECT_ID('positions', 'U') IS NULL
            CREATE TABLE positions (
                id INT IDENTITY(1,1) PRIMARY KEY,
                user_id INT NOT NULL,
                symbol NVARCHAR(100) NOT NULL,
                conid INT,
                quantity INT NOT NULL,
                avg_price FLOAT NOT NULL,
                current_price FLOAT,
                market_value FLOAT,
                unrealized_pnl FLOAT,
                realized_pnl FLOAT,
                pnl_percent FLOAT,
                stop_loss FLOAT,
                target FLOAT,
                trade_bill_id INT,
                entry_date NVARCHAR(50),
                last_updated DATETIME2 DEFAULT GETDATE(),
                status NVARCHAR(50) DEFAULT 'open',
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (trade_bill_id) REFERENCES trade_bills(id)
            )
        """)

        # Position alerts
        conn.execute("""
            IF OBJECT_ID('position_alerts', 'U') IS NULL
            CREATE TABLE position_alerts (
                id INT IDENTITY(1,1) PRIMARY KEY,
                user_id INT NOT NULL,
                position_id INT NOT NULL,
                symbol NVARCHAR(100) NOT NULL,
                alert_type NVARCHAR(100) NOT NULL,
                severity NVARCHAR(20) NOT NULL,
                message NVARCHAR(MAX) NOT NULL,
                action_suggested NVARCHAR(MAX),
                is_read BIT DEFAULT 0,
                created_at DATETIME2 DEFAULT GETDATE(),
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (position_id) REFERENCES positions(id)
            )
        """)

        # Kite orders tracking
        conn.execute("""
            IF OBJECT_ID('kite_orders', 'U') IS NULL
            CREATE TABLE kite_orders (
                id INT IDENTITY(1,1) PRIMARY KEY,
                user_id INT NOT NULL,
                order_id NVARCHAR(100) NOT NULL,
                symbol NVARCHAR(100) NOT NULL,
                exchange NVARCHAR(20) DEFAULT 'NSE',
                transaction_type NVARCHAR(20) NOT NULL,
                order_type NVARCHAR(20) NOT NULL,
                quantity INT NOT NULL,
                price FLOAT,
                trigger_price FLOAT,
                product NVARCHAR(20) DEFAULT 'CNC',
                status NVARCHAR(50) DEFAULT 'pending',
                trade_bill_id INT,
                filled_quantity INT DEFAULT 0,
                filled_price FLOAT,
                submitted_at DATETIME2 DEFAULT GETDATE(),
                filled_at DATETIME2,
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (trade_bill_id) REFERENCES trade_bills(id)
            )
        """)

        # GTT orders tracking
        conn.execute("""
            IF OBJECT_ID('kite_gtt_orders', 'U') IS NULL
            CREATE TABLE kite_gtt_orders (
                id INT IDENTITY(1,1) PRIMARY KEY,
                user_id INT NOT NULL,
                trigger_id INT NOT NULL,
                symbol NVARCHAR(100) NOT NULL,
                exchange NVARCHAR(20) DEFAULT 'NSE',
                trigger_type NVARCHAR(50) NOT NULL,
                trigger_values NVARCHAR(MAX),
                quantity INT NOT NULL,
                status NVARCHAR(50) DEFAULT 'active',
                trade_bill_id INT,
                created_at DATETIME2 DEFAULT GETDATE(),
                triggered_at DATETIME2,
                expires_at NVARCHAR(50),
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (trade_bill_id) REFERENCES trade_bills(id)
            )
        """)

        # NSE Instruments (for typeahead)
        conn.execute("""
            IF OBJECT_ID('nse_instruments', 'U') IS NULL
            CREATE TABLE nse_instruments (
                id INT IDENTITY(1,1) PRIMARY KEY,
                instrument_token INT,
                exchange NVARCHAR(20) DEFAULT 'NSE',
                tradingsymbol NVARCHAR(100) NOT NULL,
                name NVARCHAR(500),
                segment NVARCHAR(50),
                exchange_token NVARCHAR(50),
                instrument_type NVARCHAR(20),
                lot_size INT DEFAULT 1,
                tick_size FLOAT DEFAULT 0.05,
                last_updated DATETIME2 DEFAULT GETDATE()
            )
        """)
        conn.execute("""
            IF NOT EXISTS (SELECT * FROM sys.indexes WHERE name = 'idx_nse_tradingsymbol')
            CREATE INDEX idx_nse_tradingsymbol ON nse_instruments(tradingsymbol)
        """)

        # Kite orders cache (synced from Kite API)
        conn.execute("""
            IF OBJECT_ID('kite_orders_cache', 'U') IS NULL
            CREATE TABLE kite_orders_cache (
                id INT IDENTITY(1,1) PRIMARY KEY,
                order_id NVARCHAR(100) NOT NULL,
                symbol NVARCHAR(100) NOT NULL,
                exchange NVARCHAR(20) DEFAULT 'NSE',
                transaction_type NVARCHAR(20),
                order_type NVARCHAR(20),
                quantity INT,
                price FLOAT,
                trigger_price FLOAT,
                average_price FLOAT,
                filled_quantity INT DEFAULT 0,
                pending_quantity INT DEFAULT 0,
                product NVARCHAR(20),
                status NVARCHAR(50),
                tag NVARCHAR(50),
                order_timestamp DATETIME2,
                exchange_timestamp DATETIME2,
                synced_at DATETIME2 DEFAULT GETDATE()
            )
        """)

        # Kite positions cache
        conn.execute("""
            IF OBJECT_ID('kite_positions_cache', 'U') IS NULL
            CREATE TABLE kite_positions_cache (
                id INT IDENTITY(1,1) PRIMARY KEY,
                symbol NVARCHAR(100) NOT NULL,
                exchange NVARCHAR(20) DEFAULT 'NSE',
                quantity INT,
                average_price FLOAT,
                last_price FLOAT,
                pnl FLOAT,
                product NVARCHAR(20),
                day_change FLOAT,
                day_change_percentage FLOAT,
                synced_at DATETIME2 DEFAULT GETDATE()
            )
        """)

        # Kite holdings cache
        conn.execute("""
            IF OBJECT_ID('kite_holdings_cache', 'U') IS NULL
            CREATE TABLE kite_holdings_cache (
                id INT IDENTITY(1,1) PRIMARY KEY,
                symbol NVARCHAR(100) NOT NULL,
                exchange NVARCHAR(20) DEFAULT 'NSE',
                isin NVARCHAR(50),
                quantity INT,
                average_price FLOAT,
                last_price FLOAT,
                pnl FLOAT,
                day_change FLOAT,
                day_change_percentage FLOAT,
                synced_at DATETIME2 DEFAULT GETDATE()
            )
        """)

        # Holdings daily snapshot
        conn.execute("""
            IF OBJECT_ID('holdings_snapshot', 'U') IS NULL
            CREATE TABLE holdings_snapshot (
                id INT IDENTITY(1,1) PRIMARY KEY,
                snapshot_date DATE NOT NULL,
                symbol NVARCHAR(100) NOT NULL,
                exchange NVARCHAR(20) DEFAULT 'NSE',
                quantity INT,
                avg_price FLOAT,
                last_price FLOAT,
                investment FLOAT,
                current_value FLOAT,
                pnl FLOAT,
                pnl_percent FLOAT,
                created_at DATETIME2 DEFAULT GETDATE(),
                CONSTRAINT UQ_holdings_snap UNIQUE(snapshot_date, symbol)
            )
        """)

        conn.commit()
        conn.close()

        # Initialize default data
        self._init_defaults()

    def _init_defaults(self):
        """Initialize default user, strategies, and watchlists"""
        conn = self.get_connection()

        # Check if defaults exist
        cursor = conn.execute('SELECT COUNT(*) AS cnt FROM users')
        row = cursor.fetchone()
        if row['cnt'] == 0:
            from werkzeug.security import generate_password_hash

            # Create default user and get ID using OUTPUT clause
            user_id_row = conn.execute("""
                INSERT INTO users (username, password_hash)
                OUTPUT INSERTED.id
                VALUES (?, ?)
            """, ('default', generate_password_hash('elder2024'))).fetchone()
            user_id = int(user_id_row[0])

            # Create default strategy
            elder_config = {
                "name": "Elder Triple Screen",
                "timeframes": {"screen1": "weekly", "screen2": "daily"},
                "indicators": {
                    "weekly": ["EMA_22", "MACD_Histogram"],
                    "daily": ["Force_Index_2", "Stochastic_14", "EMA_22", "Impulse"]
                }
            }

            strategy_id_row = conn.execute("""
                INSERT INTO strategies (user_id, name, description, config)
                OUTPUT INSERTED.id
                VALUES (?, ?, ?, ?)
            """, (user_id, 'Elder Triple Screen',
                  "Dr. Alexander Elder's Triple Screen Trading System",
                  json.dumps(elder_config))).fetchone()
            strategy_id = int(strategy_id_row[0])

            # APGAR parameters
            apgar_params = [
                ('weekly_ema', 'Weekly EMA (22) Slope', [
                    {"score": 2, "label": "Strongly Rising"},
                    {"score": 1, "label": "Rising"},
                    {"score": 0, "label": "Flat/Falling"}
                ]),
                ('weekly_macd', 'Weekly MACD-Histogram', [
                    {"score": 2, "label": "Rising + Divergence"},
                    {"score": 1, "label": "Rising"},
                    {"score": 0, "label": "Falling"}
                ]),
                ('force_index', 'Daily Force Index (2-EMA)', [
                    {"score": 2, "label": "Below Zero + Uptick"},
                    {"score": 1, "label": "Below Zero"},
                    {"score": 0, "label": "Above Zero"}
                ]),
                ('stochastic', 'Daily Stochastic', [
                    {"score": 2, "label": "Below 30 (Oversold)"},
                    {"score": 1, "label": "30-50"},
                    {"score": 0, "label": "Above 50"}
                ]),
                ('price_ema', 'Price vs 22-Day EMA', [
                    {"score": 2, "label": "At or Below EMA"},
                    {"score": 1, "label": "Slightly Above (<2%)"},
                    {"score": 0, "label": "Far Above"}
                ])
            ]

            for i, (name, label, options) in enumerate(apgar_params):
                conn.execute("""
                    INSERT INTO apgar_parameters
                    (strategy_id, parameter_name, parameter_label, options, display_order)
                    VALUES (?, ?, ?, ?, ?)
                """, (strategy_id, name, label, json.dumps(options), i))

            # Default watchlist - NIFTY 100 with NSE: format
            nifty_100 = [
                'NSE:RELIANCE', 'NSE:TCS', 'NSE:HDFCBANK', 'NSE:INFY', 'NSE:ICICIBANK',
                'NSE:HINDUNILVR', 'NSE:SBIN', 'NSE:BHARTIARTL', 'NSE:ITC', 'NSE:KOTAKBANK',
                'NSE:LT', 'NSE:AXISBANK', 'NSE:ASIANPAINT', 'NSE:MARUTI', 'NSE:TITAN',
                'NSE:SUNPHARMA', 'NSE:ULTRACEMCO', 'NSE:BAJFINANCE', 'NSE:WIPRO', 'NSE:HCLTECH',
                'NSE:POWERGRID', 'NSE:NTPC', 'NSE:M&M', 'NSE:JSWSTEEL',
                'NSE:BAJAJFINSV', 'NSE:ONGC', 'NSE:TATASTEEL', 'NSE:ADANIENT', 'NSE:COALINDIA',
                'NSE:GRASIM', 'NSE:TECHM', 'NSE:HINDALCO', 'NSE:INDUSINDBK', 'NSE:DRREDDY',
                'NSE:APOLLOHOSP', 'NSE:CIPLA', 'NSE:EICHERMOT', 'NSE:NESTLEIND', 'NSE:DIVISLAB',
                'NSE:BRITANNIA', 'NSE:BPCL', 'NSE:ADANIPORTS', 'NSE:TATACONSUM', 'NSE:HEROMOTOCO',
                'NSE:SBILIFE', 'NSE:HDFCLIFE', 'NSE:BAJAJ-AUTO', 'NSE:SHRIRAMFIN', 'NSE:LTIM',
                'NSE:ABB', 'NSE:ACC', 'NSE:ADANIGREEN', 'NSE:ADANIPOWER', 'NSE:AMBUJACEM',
                'NSE:ATGL', 'NSE:AUROPHARMA', 'NSE:BANKBARODA', 'NSE:BEL', 'NSE:BERGEPAINT',
                'NSE:BOSCHLTD', 'NSE:CANBK', 'NSE:CHOLAFIN', 'NSE:COLPAL', 'NSE:DLF',
                'NSE:GAIL', 'NSE:GODREJCP', 'NSE:HAL', 'NSE:HAVELLS', 'NSE:ICICIPRULI',
                'NSE:IDEA', 'NSE:IGL', 'NSE:INDHOTEL', 'NSE:INDIGO', 'NSE:IOC',
                'NSE:IRCTC', 'NSE:JINDALSTEL', 'NSE:JSWENERGY', 'NSE:LICI', 'NSE:LUPIN',
                'NSE:MARICO', 'NSE:MAXHEALTH', 'NSE:MPHASIS', 'NSE:NAUKRI', 'NSE:NHPC',
                'NSE:OBEROIRLTY', 'NSE:OFSS', 'NSE:PAGEIND', 'NSE:PFC', 'NSE:PIDILITIND',
                'NSE:PNB', 'NSE:POLYCAB', 'NSE:RECLTD', 'NSE:SRF', 'NSE:TATAPOWER',
                'NSE:TORNTPHARM', 'NSE:TRENT', 'NSE:UNIONBANK', 'NSE:VBL'
            ]

            conn.execute("""
                INSERT INTO watchlists (user_id, name, market, symbols, is_default)
                VALUES (?, ?, ?, ?, ?)
            """, (user_id, 'NIFTY 100', 'IN', json.dumps(nifty_100), 1))

            # Default account settings
            conn.execute("""
                INSERT INTO account_settings
                (user_id, account_name, market, trading_capital, currency, broker)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (user_id, 'Trading Account', 'IN', 500000, 'INR', 'Zerodha'))

            conn.commit()

        conn.close()

    # ========== TRADE BILLS METHODS ==========
    def create_trade_bill(self, user_id: int, data: Dict) -> int:
        """Create a new trade bill"""
        conn = self.get_connection()
        cursor = conn.cursor()

        # Auto-calculate derived fields
        if 'entry_price' in data and 'stop_loss' in data and 'quantity' in data:
            risk_per_share = (data['entry_price'] -
                              data['stop_loss']) * data['quantity']
            data['risk_per_share'] = abs(
                data['entry_price'] - data['stop_loss'])
            data['risk_amount_currency'] = risk_per_share

        if 'target_price' in data and 'entry_price' in data and 'quantity' in data:
            potential_gain = (data['target_price'] -
                              data['entry_price']) * data['quantity']
            data['potential_gain'] = potential_gain

        if 'risk_amount_currency' in data and 'reward_amount_currency' in data:
            if data['risk_amount_currency'] > 0:
                data['risk_reward_ratio'] = data['reward_amount_currency'] / \
                    data['risk_amount_currency']

        # Build insert statement dynamically
        columns = ', '.join(data.keys())
        placeholders = ', '.join(['?' for _ in data])
        values = tuple(data.values())

        trade_bill_id_row = cursor.execute(f"""
            INSERT INTO trade_bills (user_id, {columns})
            OUTPUT INSERTED.id
            VALUES (?, {placeholders})
        """, (user_id, *values)).fetchone()

        conn.commit()
        trade_bill_id = int(trade_bill_id_row[0])
        conn.close()
        return trade_bill_id

    def get_trade_bill(self, trade_bill_id: int) -> Optional[Dict]:
        """Get a specific trade bill"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM trade_bills WHERE id = ?',
                       (trade_bill_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    def get_trade_bills(self, user_id: int, status: str = None) -> List[Dict]:
        """Get all trade bills for a user"""
        conn = self.get_connection()
        cursor = conn.cursor()

        if status:
            cursor.execute("""
                SELECT * FROM trade_bills
                WHERE user_id = ? AND status = ?
                ORDER BY created_at DESC
            """, (user_id, status))
        else:
            cursor.execute("""
                SELECT * FROM trade_bills
                WHERE user_id = ?
                ORDER BY created_at DESC
            """, (user_id,))

        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def update_trade_bill(self, trade_bill_id: int, data: Dict) -> bool:
        """Update a trade bill"""
        conn = self.get_connection()
        cursor = conn.cursor()

        data['updated_at'] = datetime.now().isoformat()

        set_clause = ', '.join([f'{k} = ?' for k in data.keys()])
        values = tuple(data.values())

        cursor.execute(f"""
            UPDATE trade_bills
            SET {set_clause}
            WHERE id = ?
        """, (*values, trade_bill_id))

        conn.commit()
        success = cursor.rowcount > 0
        conn.close()
        return success

    def delete_trade_bill(self, trade_bill_id: int) -> bool:
        """Delete a trade bill"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM trade_bills WHERE id = ?',
                       (trade_bill_id,))
        conn.commit()
        success = cursor.rowcount > 0
        conn.close()
        return success

    def calculate_trade_metrics(self, entry_price: float, stop_loss: float,
                                target_price: float, quantity: float,
                                account_capital: float, risk_percent: float) -> Dict:
        """Calculate trade metrics and position sizing"""
        metrics = {}

        metrics['risk_per_share'] = abs(entry_price - stop_loss)
        metrics['stop_loss_pips'] = metrics['risk_per_share']
        metrics['target_pips'] = abs(target_price - entry_price)
        metrics['potential_gain'] = metrics['target_pips'] * quantity

        if metrics['risk_per_share'] > 0:
            metrics['risk_reward_ratio'] = metrics['target_pips'] / \
                metrics['risk_per_share']

        max_risk_amount = (account_capital * risk_percent) / 100
        metrics['max_qty_for_risk'] = max_risk_amount / \
            metrics['risk_per_share'] if metrics['risk_per_share'] > 0 else 0
        metrics['position_size'] = quantity
        metrics['risk_amount_currency'] = quantity * metrics['risk_per_share']
        metrics['break_even'] = entry_price

        return metrics


# Singleton instance
_db_instance = None


def get_database() -> Database:
    """Get database singleton instance"""
    global _db_instance
    if _db_instance is None:
        _db_instance = Database()
    return _db_instance
