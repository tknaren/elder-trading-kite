"""
Elder Trading System - Local Application v2
Enhanced with connected workflow: Screener -> Trade Bill -> Kite Connect -> Trade Log -> Positions

Run with: python app.py
Access at: http://localhost:5001

Data Source: Kite Connect API (Zerodha)
Database: SQL Server (KiteTraderDb)
Market: NSE (NIFTY 100)
Symbol Format: NSE:SYMBOL (e.g., NSE:RELIANCE, NSE:TCS)
"""

from routes.api_v2 import api_v2
from routes.api import api
from routes.screener_api import screener_routes
from models.database import Database, get_database
from config import DatabaseConfig
from flask import Flask, render_template
import os
import sys

# Add backend directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def create_app():
    """Create and configure the Flask application"""
    app = Flask(__name__)

    # Configuration for local development
    app.config['SECRET_KEY'] = 'elder-trading-local-dev-key'
    # Disable caching for development
    app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0
    app.config['TEMPLATES_AUTO_RELOAD'] = True  # Auto-reload templates

    # SQL Server connection string from config
    conn_str = DatabaseConfig.connection_string()
    app.config['DB_CONNECTION_STRING'] = conn_str

    # Register API blueprints
    app.register_blueprint(api, url_prefix='/api')      # Original API
    app.register_blueprint(api_v2, url_prefix='/api/v2')  # Enhanced v2 API
    # Additional screeners
    app.register_blueprint(screener_routes, url_prefix='/api/v2/screener')

    # Initialize database (creates tables and default data)
    with app.app_context():
        db = Database(conn_str)
        app.config['DB'] = db

        # Run migrations for v2 features
        try:
            from models.migrate_v2 import migrate_database
            migrate_database(conn_str)
        except Exception as e:
            print(f"Migration note: {e}")

    # Serve frontend
    @app.route('/')
    def index():
        from flask import make_response
        response = make_response(render_template('index.html'))
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response

    return app


# Create application instance
app = create_app()

if __name__ == '__main__':
    import webbrowser
    import threading

    print("\n" + "="*50)
    print("  Elder Trading System - Local Server")
    print("="*50)
    print(f"\n  Database: SQL Server ({DatabaseConfig.display_info()})")
    print("  Open in browser: http://localhost:5001")
    print("  Market: NSE (NIFTY 100)")
    print("  Broker: Kite Connect (Zerodha)")
    print("\n  Press Ctrl+C to stop the server")
    print("="*50 + "\n")

    # Auto-open browser after 2 seconds
    def open_browser():
        import time
        time.sleep(2)
        webbrowser.open('http://localhost:5001')

    threading.Thread(target=open_browser, daemon=True).start()

    app.run(
        host='0.0.0.0',
        port=5001,
        debug=False,  # Disable debug for standalone use
        use_reloader=False
    )
