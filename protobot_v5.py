import pandas as pd
import numpy as np
from dataclasses import dataclass, field
from typing import Optional, List, Dict
import unittest
import time

# ==========================================
# 1. KONFIGURATION & DATENSTRUKTUREN
# ==========================================

@dataclass
class Config:
    latency_ms: int = 30
    fee_maker: float = 0.0001
    fee_taker: float = 0.0004
    imbalance_window: int = 10
    score_threshold_buy: int = 80
    score_threshold_sell: int = 20
    # Dynamisches Risk & Sizing
    initial_equity: float = 10000.0
    risk_per_trade: float = 0.01  # 1% der Equity
    # Spread-adaptive Exits (HFT-geeignet)
    sl_spread_multiplier: float = 5.0
    tp_spread_multiplier: float = 15.0

@dataclass
class OrderbookTick:
    timestamp: float
    best_bid: float
    best_ask: float
    bid_vol: float
    ask_vol: float

@dataclass
class DiagnosticState:
    timestamp: float = 0.0
    liquidity_spread: float = 0.0
    imbalance: float = 0.0
    imbalance_momentum: float = 0.0
    microprice: float = 0.0
    regime: str = "WAITING"
    signal_score: int = 50 
    entry_signal: Optional[str] = None
    fill_status: str = "NONE"
    position_size: float = 0.0
    exit_signal: Optional[str] = None
    realized_pnl: float = 0.0
    
    def to_dict(self):
        return self.__dict__

# ==========================================
# 2. SIGNALQUALITÄT & REGIME DETECTION
# ==========================================

class SignalEngine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.imbalance_history = []
        
    def process_tick(self, tick: OrderbookTick, state: DiagnosticState) -> DiagnosticState:
        state.liquidity_spread = tick.best_ask - tick.best_bid
        total_vol = tick.bid_vol + tick.ask_vol
        
        state.imbalance = (tick.bid_vol - tick.ask_vol) / total_vol if total_vol > 0 else 0.0
        
        self.imbalance_history.append(state.imbalance)
        if len(self.imbalance_history) > self.cfg.imbalance_window:
            self.imbalance_history.pop(0)
            
        # FIX 4: Momentum wird erst berechnet, wenn das Lookback-Fenster gefüllt ist
        if len(self.imbalance_history) == self.cfg.imbalance_window:
            state.imbalance_momentum = self.imbalance_history[-1] - self.imbalance_history[0]
        else:
            state.imbalance_momentum = 0.0
            
        if total_vol > 0:
            state.microprice = (tick.best_bid * tick.ask_vol + tick.best_ask * tick.bid_vol) / total_vol
        else:
            state.microprice = (tick.best_bid + tick.best_ask) / 2
            
        # Regime Update
        if state.imbalance_momentum > 0.1 and state.imbalance > 0.2:
            state.regime = "BULLISH_MICRO_TREND"
        elif state.imbalance_momentum < -0.1 and state.imbalance < -0.2:
            state.regime = "BEARISH_MICRO_TREND"
        else:
            state.regime = "MEAN_REVERTING"
            
        # Score Normalisierung
        raw_score = 50 + (state.imbalance * 20) + (state.imbalance_momentum * 30)
        state.signal_score = int(max(0, min(100, raw_score)))
        
        # FIX 2: Regime Detection erzwingt Signal-Alignment
        if state.signal_score > self.cfg.score_threshold_buy and state.regime == "BULLISH_MICRO_TREND":
            state.entry_signal = "BUY"
        elif state.signal_score < self.cfg.score_threshold_sell and state.regime == "BEARISH_MICRO_TREND":
            state.entry_signal = "SELL"
        else:
            state.entry_signal = None
            
        return state

# ==========================================
# 3. BACKTEST-REALISMUS & EXECUTION
# ==========================================

class ExecutionEngine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.pending_orders = []
        self.position = 0.0
        self.entry_price = 0.0
        self.equity = cfg.initial_equity
        
    def calculate_position_size(self, price: float) -> float:
        """Dynamisches Sizing basierend auf Equity und Risk"""
        notional_risk = self.equity * self.cfg.risk_per_trade
        # Vereinfachtes HFT Modell: Size = Risk / Price (in echt abhängig vom Stop-Level)
        return round(notional_risk / price, 4) if price > 0 else 0.0

    def manage_risk(self, tick: OrderbookTick, state: DiagnosticState):
        if self.position == 0 or len(self.pending_orders) > 0 or self.entry_price <= 0:
            return 
            
        spread = tick.best_ask - tick.best_bid
        
        # Adaptive Stops & Targets basierend auf aktuellem Orderbuch-Spread
        sl_distance = spread * self.cfg.sl_spread_multiplier
        tp_distance = spread * self.cfg.tp_spread_multiplier
        
        if self.position > 0:
            sl_price = self.entry_price - sl_distance
            tp_price = self.entry_price + tp_distance
            # Trigger conditions
            if tick.best_bid <= sl_price:
                state.exit_signal = "STOP_LOSS"
                self.route_order(tick, -self.position, "TAKER", 0.0, state, bypass_latency=True)
            elif tick.best_bid >= tp_price:
                state.exit_signal = "TAKE_PROFIT"
                # Post Limit Sell at current best_ask
                self.route_order(tick, -self.position, "MAKER", tick.best_ask, state, bypass_latency=False)
        else:
            sl_price = self.entry_price + sl_distance
            tp_price = self.entry_price - tp_distance
            if tick.best_ask >= sl_price:
                state.exit_signal = "STOP_LOSS"
                self.route_order(tick, -self.position, "TAKER", 0.0, state, bypass_latency=True)
            elif tick.best_ask <= tp_price:
                state.exit_signal = "TAKE_PROFIT"
                self.route_order(tick, -self.position, "MAKER", tick.best_bid, state, bypass_latency=False)

    def route_order(self, tick: OrderbookTick, size: float, order_type: str, limit_price: float, state: DiagnosticState, bypass_latency: bool = False):
        # FIX 1: limit_price dem Order-Objekt hinzugefügt
        order_time = tick.timestamp if bypass_latency else tick.timestamp + (self.cfg.latency_ms / 1000.0)
        self.pending_orders.append({
            "time": order_time, 
            "size": size, 
            "type": order_type,
            "limit_price": limit_price
        })
        state.fill_status = f"ROUTED ({order_type} @ {limit_price if limit_price else 'MKT'})"

    def process_fills(self, tick: OrderbookTick, state: DiagnosticState):
        executable = [o for o in self.pending_orders if tick.timestamp >= o["time"]]
        
        for order in executable:
            # FIX 1: Reale Fill-Bedingung für Maker-Orders (Adverse Selection Check)
            if order["type"] == "MAKER":
                if order["size"] > 0 and tick.best_ask > order["limit_price"]:
                    continue # Kein Fill, Markt hat Limit nicht erreicht (Warten auf Verkäufer)
                if order["size"] < 0 and tick.best_bid < order["limit_price"]:
                    continue # Kein Fill, Markt hat Limit nicht erreicht (Warten auf Käufer)
            
            self._execute_market(tick, order, state)
            
        # Ungefüllte Orders (z.B. Maker Limits) bleiben im Orderbuch
        self.pending_orders = [o for o in self.pending_orders if o not in executable or (o in executable and o["size"] != 0)]

    def _execute_market(self, tick: OrderbookTick, order: dict, state: DiagnosticState):
        size = order["size"]
        available_vol = tick.ask_vol if size > 0 else tick.bid_vol
        fill_size = min(abs(size), available_vol) * np.sign(size) if available_vol > 0 else 0.0
            
        if fill_size == 0:
            return  
            
        # Update Restgröße in der ausführenden Order
        order["size"] -= fill_size
        if abs(order["size"]) > 1e-8:
            state.fill_status = f"PARTIAL_FILL ({abs(fill_size)}/{abs(size)})"
        else:
            state.fill_status = "FULL_FILL"
            
        if order["type"] == "TAKER":
            slippage = 0.5 * (abs(fill_size) / available_vol) * (tick.best_ask - tick.best_bid)
            exec_price = (tick.best_ask + slippage) if fill_size > 0 else (tick.best_bid - slippage)
            fee_rate = self.cfg.fee_taker
        else:
            # Maker füllt zum Limit-Preis
            exec_price = order["limit_price"]
            fee_rate = self.cfg.fee_maker

        fee = abs(fill_size) * exec_price * fee_rate
        
        # FIX 3: Sauberes Flip-Handling (Split in Close und Open Legs)
        closing_size = 0.0
        opening_size = fill_size
        
        is_closing_direction = (self.position > 0 and fill_size < 0) or (self.position < 0 and fill_size > 0)
        
        if is_closing_direction:
            if abs(fill_size) <= abs(self.position):
                closing_size = fill_size
                opening_size = 0.0
            else:
                closing_size = -self.position
                opening_size = fill_size - closing_size
                
        # 1. Close Leg verarbeiten
        if closing_size != 0:
            pnl = (exec_price - self.entry_price) * abs(closing_size) * (1 if self.position > 0 else -1)
            net_pnl = pnl - (abs(closing_size) * exec_price * fee_rate)
            state.realized_pnl += net_pnl
            self.equity += net_pnl
            self.position += closing_size
            if abs(self.position) < 1e-8:
                self.position = 0.0
                self.entry_price = 0.0

        # 2. Open Leg verarbeiten (VWAP Entry Update)
        if opening_size != 0:
            old_value = self.position * self.entry_price
            new_value = opening_size * exec_price
            self.position += opening_size
            self.entry_price = abs((old_value + new_value) / self.position)
            
            entry_fee = abs(opening_size) * exec_price * fee_rate
            state.realized_pnl -= entry_fee
            self.equity -= entry_fee
            
        state.position_size = self.position

# ==========================================
# 4. BOT ORCHESTRATOR & INTERFACES
# ==========================================

class DataFeedBase:
    """Basisklasse für echte Exchange-Feeds (z.B. CCXT Websockets)"""
    def __init__(self):
        self.callbacks = []
    def on_tick(self, tick: OrderbookTick):
        for cb in self.callbacks:
            cb(tick)

class ProtoBotV5:
    def __init__(self, config: Config):
        self.cfg = config
        self.signal_engine = SignalEngine(config)
        self.execution_engine = ExecutionEngine(config)
        self.diagnostics_log = []
        
    def tick_callback(self, tick: OrderbookTick):
        state = DiagnosticState(timestamp=tick.timestamp)
        
        state = self.signal_engine.process_tick(tick, state)
        self.execution_engine.process_fills(tick, state)
        self.execution_engine.manage_risk(tick, state)
        
        if state.entry_signal and self.execution_engine.position == 0 and len(self.execution_engine.pending_orders) == 0:
            size = self.execution_engine.calculate_position_size(tick.microprice)
            if state.entry_signal == "SELL": size = -size
            
            if size != 0:
                self.execution_engine.route_order(tick, size, "TAKER", 0.0, state)
            
        state.position_size = self.execution_engine.position
        self.diagnostics_log.append(state)

# ==========================================
# 5. UNIT TESTS (Validation Layer)
# ==========================================

class TestProtoBot(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(latency_ms=0, fee_maker=0.0, fee_taker=0.0, initial_equity=10000.0)
        self.exec_engine = ExecutionEngine(self.cfg)
        self.state = DiagnosticState()

    def test_position_flipping(self):
        """Testet ob PnL und VWAP bei einem Close-And-Reverse exakt stimmen"""
        tick1 = OrderbookTick(1.0, 100, 101, 10, 10)
        # Entry Long 10 @ 101
        self.exec_engine._execute_market(tick1, {"size": 10.0, "type": "TAKER"}, self.state)
        self.assertEqual(self.exec_engine.position, 10.0)
        self.assertEqual(self.exec_engine.entry_price, 101.0)
        
        # Exit Long 10 UND Flip Short 5 -> Order Size -15 @ Bid 105
        tick2 = OrderbookTick(2.0, 105, 106, 20, 10)
        self.exec_engine._execute_market(tick2, {"size": -15.0, "type": "TAKER"}, self.state)
        
        # Realized PnL: 10 * (105 - 101) = 40
        self.assertEqual(self.state.realized_pnl, 40.0)
        # Neue Position: -5 Short @ 105
        self.assertEqual(self.exec_engine.position, -5.0)
        self.assertEqual(self.exec_engine.entry_price, 105.0)

    def test_maker_fill_adverse_selection(self):
        """Testet ob Maker Limits passiv im Buch liegen bis der Preis triggert"""
        # Order pending: Maker Buy @ 100
        self.exec_engine.pending_orders.append({"time": 0.0, "size": 1.0, "type": "MAKER", "limit_price": 100.0})
        
        # Tick 1: Ask ist 102. Order darf nicht gefüllt werden.
        tick_no_fill = OrderbookTick(1.0, 101, 102, 10, 10)
        self.exec_engine.process_fills(tick_no_fill, self.state)
        self.assertEqual(self.exec_engine.position, 0.0)
        self.assertEqual(len(self.exec_engine.pending_orders), 1)
        
        # Tick 2: Ask fällt auf 100 (Jemand verkauft aktiv in unser Bid)
        tick_fill = OrderbookTick(2.0, 99, 100, 10, 10)
        self.exec_engine.process_fills(tick_fill, self.state)
        self.assertEqual(self.exec_engine.position, 1.0)
        self.assertEqual(self.exec_engine.entry_price, 100.0)
        self.assertEqual(len(self.exec_engine.pending_orders), 0)

if __name__ == "__main__":
    print("Führe strukturelle Unit Tests aus...")
    unittest.main(argv=['first-arg-is-ignored'], exit=False)
