# Elder Trading System - Quick Start Guide

## How to Run the App Daily

### Method 1: Double-Click (Easiest)

1. Double-click **`start_server.bat`** in this folder
2. The browser will open automatically at <http://localhost:5001>
3. Keep the command window open while using the app
4. Press **Ctrl+C** in the command window to stop

### Method 2: Command Line

```bash
cd C:\Naren\Working\Source\GitHubRepo\Claude_Trade\elder-trading-kite\backend
python app.py
```

### Method 3: Create Desktop Shortcut

1. Right-click on **`start_server.bat`**
2. Select "Send to" → "Desktop (create shortcut)"
3. Double-click the desktop shortcut daily to start

## What You'll See

The app will open in your default browser with:

- **Screener**: Triple Screen Scanner for NSE NIFTY 100 stocks
- **Trade Bill**: Calculate entry/stop/target prices
- **Trade Journal**: Track your trades
- **P&L Tracker**: Monitor profit/loss
- **Account**: Manage your trading capital

## Requirements

- Python 3.8 or higher
- All packages from `requirements.txt` installed

If you get an error about missing packages, run:

```bash
pip install -r requirements.txt
```

## Accessing from Other Devices

The server runs on all network interfaces, so you can access it from:

- This computer: <http://localhost:5001>
- Other devices on your network: <http://192.168.0.39:5001> (check the startup message for your IP)

## Data Storage

All your data is stored in: `./data/elder_trading.db`

## Troubleshooting

**Port already in use?**

- Close any existing Flask servers
- Or change the port in `app.py` (line with `port=5001`)

**Blank page?**

- Clear your browser cache (Ctrl+Shift+Delete)
- Try accessing <http://127.0.0.1:5001> instead

**Browser doesn't open automatically?**

- Manually open your browser and go to <http://localhost:5001>
