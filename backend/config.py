"""
Elder Trading System - Database Configuration
SQL Server connection settings for KiteTraderDb
"""

import os


class DatabaseConfig:
    """SQL Server connection configuration"""

    # SQL Server connection parameters
    SERVER = os.environ.get('DB_SERVER', 'localhost')
    PORT = int(os.environ.get('DB_PORT', '1433'))
    DATABASE = os.environ.get('DB_DATABASE', 'KiteTraderDb')
    USERNAME = os.environ.get('DB_USERNAME', 'sa')
    PASSWORD = os.environ.get('DB_PASSWORD', '')
    DRIVER = os.environ.get('DB_DRIVER', '{ODBC Driver 17 for SQL Server}')

    # Connection pool settings
    TIMEOUT = int(os.environ.get('DB_TIMEOUT', '30'))

    @classmethod
    def connection_string(cls):
        """Build pyodbc connection string"""
        return (
            f"DRIVER={cls.DRIVER};"
            f"SERVER={cls.SERVER},{cls.PORT};"
            f"DATABASE={cls.DATABASE};"
            f"UID={cls.USERNAME};"
            f"PWD={cls.PASSWORD};"
            f"TrustServerCertificate=yes;"
            f"Connection Timeout={cls.TIMEOUT};"
        )

    @classmethod
    def display_info(cls):
        """Display connection info (password masked)"""
        return (
            f"Server: {cls.SERVER}:{cls.PORT}, "
            f"Database: {cls.DATABASE}, "
            f"User: {cls.USERNAME}"
        )
