# Elder Trading System

A local trading analysis application implementing Dr. Alexander Elder's Triple Screen methodology.

**Data Source: Kite Connect API (Zerodha)**
**Market: NSE (NIFTY 100)**
**Currency: ₹ (INR)**

## Features

- **Triple Screen Analysis**: Weekly trend + Daily entry timing
- **Configurable Indicators**: Choose from multiple indicators per category
- **Candlestick Pattern Recognition**: 20+ patterns detected automatically
- **Signal Strength Scoring**: 0-10 score with detailed breakdown
- **Trade Journal**: Track your trades with P&L statistics
- **Position Sizing Calculator**: Based on your risk parameters
- **Kite Connect Integration**: Place orders directly from Trade Bills
- **NSE Charges Calculator**: STT, GST, and all NSE charges included

## Prerequisites

### Kite Connect API

1. **Create Kite Connect App** at [Kite Connect](https://kite.trade/)
   - Login with your Zerodha credentials
   - Create a new app
   - Note down your API Key and API Secret

2. **Configure API Credentials**:
   - Set your API credentials in the application settings
   - You'll need to authorize the app on first use

## Quick Start

### 1. Setup Elder Trading System

```bash
cd backend
python -m venv venv
venv\Scripts\activate       # Windows
pip install -r requirements.txt
```

### 2. Run Elder Trading System

```bash
python app.py
```

### 3. Open Browser

Go to: **<http://localhost:5001>**

The application will connect to Kite Connect API for NSE market data.

## Project Structure

```
elder-trading-system/
└── backend/
    ├── app.py                    # Main application entry
    ├── requirements.txt          # Python dependencies
    ├── data/                     # Database (auto-created)
    │   └── elder_trading.db
    ├── templates/
    │   └── index.html            # Frontend UI
    ├── models/
    │   └── database.py           # Database operations
    ├── services/
    │   ├── indicators.py         # Technical indicator calculations
    │   ├── indicator_config.py   # Configurable indicator settings
    │   ├── candlestick_patterns.py  # Pattern recognition
    │   └── screener.py           # Stock screening logic
    └── routes/
        └── api.py                # REST API endpoints
```

## Indicator Configuration

### Current Setup (Elder's Original)

| Screen | Category | Indicator |
|--------|----------|-----------|
| Screen 1 | Trend | EMA 22 |
| Screen 1 | Momentum | MACD Histogram |
| Screen 2 | Oscillator | Force Index (2-EMA) |
| Screen 2 | Oscillator | Stochastic |
| Screen 2 | Volume | Force Index (13-EMA) |
| Screen 3 | Volatility | ATR |
| All | Impulse | Elder Impulse System |

### Available Alternatives

**TREND**: EMA 13, SMA 50, SMA 200
**MOMENTUM**: MACD Line, Rate of Change
**OSCILLATOR**: RSI, Williams %R, CCI
**VOLUME**: OBV, Volume SMA
**VOLATILITY**: Bollinger Width, Keltner Channel

## Signal Strength Scoring

| Points | Condition |
|--------|-----------|
| +2 | Weekly EMA strongly rising |
| +1 | Weekly MACD-H rising |
| +2 | Force Index < 0 (pullback) |
| +2 | Stochastic < 30 (oversold) |
| +1 | Price at/below EMA |
| +2 | Bullish divergence |
| +1 | Impulse GREEN |
| +2 | Strong bullish candlestick pattern |
| -2 | Impulse RED |

**Grades**: A (≥5), B (3-4), C (1-2), AVOID (≤0 or RED)

## Candlestick Patterns Detected

### High Reliability (5/5)

- Morning Star / Evening Star
- Three White Soldiers / Three Black Crows

### Good Reliability (4/5)

- Bullish/Bearish Engulfing
- Three Inside Up/Down
- Marubozu

### Moderate Reliability (3/5)

- Hammer / Shooting Star
- Piercing Line / Dark Cloud Cover
- Tweezer Top/Bottom
- Dragonfly/Gravestone Doji

## API Endpoints

### Screener

- `POST /api/screener/weekly` - Run weekly scan
- `POST /api/screener/daily` - Run daily scan
- `GET /api/screener/criteria` - Get grading criteria

### Indicators

- `GET /api/indicators/catalog` - All available indicators
- `GET /api/indicators/recommended` - Elder's recommendations

### Patterns

- `GET /api/patterns/catalog` - All candlestick patterns
- `GET /api/patterns/bullish` - Bullish patterns only

### Trading

- `GET /api/settings` - Account settings
- `GET /api/journal` - Trade journal entries
- `POST /api/journal` - Create journal entry

## Watchlists

### Indian Market (NIFTY 100)

RELIANCE, TCS, INFY, HDFCBANK, ICICIBANK, KOTAKBANK, HINDUNILVR, SBIN, BHARTIARTL, ITC, LT, AXISBANK, ASIANPAINT, MARUTI, TITAN, BAJFINANCE, NESTLEIND, ULTRACEMCO, SUNPHARMA, WIPRO, and 80 more...

**Total: 100 NSE stocks from NIFTY 100 index**  
**Symbol Format: NSE:SYMBOL (e.g., NSE:RELIANCE, NSE:TCS)**  
**Currency: ₹ (INR)**

## Tips

1. **Run after market close** - Best time for analysis
2. **Focus on A-trades** - Signal strength ≥ 5
3. **Check patterns** - Candlestick confirmation adds confidence
4. **Respect RED impulse** - Never buy against it
5. **Use position sizing** - 1-2% risk per trade

## Troubleshooting

### "Module not found" error

```bash
pip install -r requirements.txt
```

### "Port already in use"

```bash
# Change port in app.py or kill existing process
python app.py  # Uses port 5000 by default
```

### Yahoo Finance rate limiting

- Wait a few minutes between scans
- Reduce watchlist size for testing

## License

For personal use only. Based on Dr. Alexander Elder's methodologies from "Trading for a Living" and "Come to My Trading Room".
