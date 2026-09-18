# ProtoBot V4

Ein quantitatives Execution- und HFT-Backtesting-Framework.

## Features
- **Diagnose Pipeline:** Tick-genaues Tracking von Orderbuch-Imbalance, Microprice & Regime Detection.
- **Execution Realismus:** Simuliert Order-Routing-Latenz, Partial Fills und VWAP-Berechnungen.
- **Risk Management:** Akkurate Trennung von Maker- (Take Profit) und Taker-Orders (Stop Loss, Entry). Verhindert Latenz-Overleverage durch In-Flight-Locks.

## Installation & Ausführung
1. Abhängigkeiten installieren: `pip install -r requirements.txt`
2. Bot starten: `python protobot_v4.py`
