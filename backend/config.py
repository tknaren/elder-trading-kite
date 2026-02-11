"""
Elder Trading System - Database Configuration
SQL Server connection settings for KiteTraderDb
"""

import os


class DatabaseConfig:
    """SQL Server connection configuration"""

    # SQL Server connection parameters
    SERVER = os.environ.get('DB_SERVER', 'NAREN')
    PORT = int(os.environ.get('DB_PORT', '1433'))
    DATABASE = os.environ.get('DB_DATABASE', 'KiteTraderDb')
    # Use 'windows' for Windows Authentication, or provide username/password for SQL Server auth
    USERNAME = os.environ.get('DB_USERNAME', 'windows')
    PASSWORD = os.environ.get('DB_PASSWORD', '')
    DRIVER = os.environ.get('DB_DRIVER', '{ODBC Driver 17 for SQL Server}')

    # Connection pool settings
    TIMEOUT = int(os.environ.get('DB_TIMEOUT', '30'))

    @classmethod
    def connection_string(cls):
        """Build pyodbc connection string"""
        # Try Windows Authentication first (more common in local dev)
        # If USERNAME is empty or 'windows', use Trusted_Connection
        if not cls.USERNAME or cls.USERNAME.lower() == 'windows':
            return (
                f"DRIVER={cls.DRIVER};"
                f"SERVER={cls.SERVER};"
                f"DATABASE={cls.DATABASE};"
                f"Trusted_Connection=yes;"
                f"TrustServerCertificate=yes;"
                f"Connection Timeout={cls.TIMEOUT};"
            )
        else:
            # Use SQL Server Authentication
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
        if not cls.USERNAME or cls.USERNAME.lower() == 'windows':
            auth_method = "Windows Authentication"
        else:
            auth_method = f"User: {cls.USERNAME}"

        return (
            f"Server: {cls.SERVER}, "
            f"Database: {cls.DATABASE}, "
            f"Auth: {auth_method}"
        )
