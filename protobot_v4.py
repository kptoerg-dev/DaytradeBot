import pandas as pd
import numpy as np
from dataclasses import dataclass
from typing import Optional
import time

# ==========================================
# 1. DATENSTRUKTUREN & DIAGNOSE-STATE
# ==========================================

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
    regime: str = "NEUTRAL"
    signal_score: int = 50 
    entry_signal: Optional[str] = None
    fill_status: str = "NONE"
    position_size: float = 0.0
    exit_signal: Optional[str] = None
    realized_pnl: float = 0.0
    
    def print_diagnostics(self):
        print(f"\n--- DIAGNOSTICS @ {self.timestamp:.3f} ---")
        print(f"↓ Liquidity / Spread : {self.liquidity_spread:.4f}")
        print(f"↓ Imbalance          : {self.imbalance:.2f}")
        print(f"↓ Imbalance Momentum : {self.imbalance_momentum:.4f}")
        print(f"↓ Microprice         : {self.microprice:.4f}")
        print(f"↓ Short-term Regime  : {self.regime}")
        print(f"↓ Signal Score       : {self.signal_score}/100")
        if self.entry_signal: print(f"↓ Entry              : {self.entry_signal}")
        print(f"↓ Fill / Latency     : {self.fill_status}")
        print(f"↓ Position Management: Size={self.position_size}")
        if self.exit_signal: print(f"↓ Exit               : {self.exit_signal}")
        print("-----------------------------------")

# ==========================================
# 2. SIGNALQUALITÄT & REGIME DETECTION
# ==========================================

class SignalEngine:
    def __init__(self):
        self.imbalance_history = []
        
    def process_tick(self, tick: OrderbookTick, state: DiagnosticState) -> DiagnosticState:
        state.liquidity_spread = tick.best_ask - tick.best_bid
        total_vol = tick.bid_vol + tick.ask_vol
        
        state.imbalance = (tick.bid_vol - tick.ask_vol) / total_vol if total_vol > 0 else 0.0
        
        self.imbalance_history.append(state.imbalance)
        if len(self.imbalance_history) > 10:
            self.imbalance_history.pop(0)
            
        if len(self.imbalance_history) >= 2:
            state.imbalance_momentum = self.imbalance_history[-1] - self.imbalance_history[0]
            
        if total_vol > 0:
            state.microprice = (tick.best_bid * tick.ask_vol + tick.best_ask * tick.bid_vol) / total_vol
        else:
            state.microprice = (tick.best_bid + tick.best_ask) / 2
            
        if state.imbalance_momentum > 0.1 and state.imbalance > 0.2:
            state.regime = "BULLISH_MICRO_TREND"
        elif state.imbalance_momentum < -0.1 and state.imbalance < -0.2:
            state.regime = "BEARISH_MICRO_TREND"
        else:
            state.regime = "MEAN_REVERTING"
            
        base_score = 50
        imbalance_impact = state.imbalance * 20
        momentum_impact = state.imbalance_momentum * 30
        
        raw_score = base_score + imbalance_impact + momentum_impact
        state.signal_score = int(max(0, min(100, raw_score)))
        
        if state.signal_score > 80:
            state.entry_signal = "BUY"
        elif state.signal_score < 20:
            state.entry_signal = "SELL"
        else:
            state.entry_signal = None
            
        return state

# ==========================================
# 3. BACKTEST-REALISMUS & EXECUTION
# ==========================================

class ExecutionEngine:
    def __init__(self, latency_ms=50, fee_maker=0.0001, fee_taker=0.0004):
        self.latency_ms = latency_ms / 1000.0
        self.fee_maker = fee_maker
        self.fee_taker = fee_taker
        self.pending_orders = []
        self.position = 0.0
        self.entry_price = 0.0
        
    def manage_risk(self, tick: OrderbookTick, state: DiagnosticState):
        if self.position == 0 or len(self.pending_orders) > 0:
            return 
            
        if self.entry_price <= 0:
            return
            
        if self.position > 0:
            unrealized_pnl = (tick.best_bid - self.entry_price) / self.entry_price
        else:
            unrealized_pnl = (self.entry_price - tick.best_ask) / self.entry_price
        
        if unrealized_pnl <= -0.01:
            state.exit_signal = "STOP_LOSS"
            self.route_order(tick, -self.position, "TAKER", state, bypass_latency=True) 
        elif unrealized_pnl >= 0.02:
            state.exit_signal = "TAKE_PROFIT"
            self.route_order(tick, -self.position, "MAKER", state, bypass_latency=False)

    def route_order(self, tick: OrderbookTick, size: float, order_type: str, state: DiagnosticState, bypass_latency: bool = False):
        order_time = tick.timestamp if bypass_latency else tick.timestamp + self.latency_ms
        self.pending_orders.append({"time": order_time, "size": size, "type": order_type})
        state.fill_status = f"ROUTED (Type: {order_type}, Latency: {0 if bypass_latency else self.latency_ms}s)"

    def process_fills(self, tick: OrderbookTick, state: DiagnosticState):
        executable = [o for o in self.pending_orders if tick.timestamp >= o["time"]]
        self.pending_orders = [o for o in self.pending_orders if tick.timestamp < o["time"]]
        
        for order in executable:
            self._execute_market(tick, order["size"], order["type"], state)

    def _execute_market(self, tick: OrderbookTick, size: float, order_type: str, state: DiagnosticState):
        available_vol = tick.ask_vol if size > 0 else tick.bid_vol
        
        if available_vol > 0:
            fill_size = min(abs(size), available_vol) * np.sign(size)
        else:
            fill_size = 0.0
            
        if abs(fill_size) < abs(size):
            remaining_size = size - fill_size
            if remaining_size != 0:
                self.pending_orders.append({"time": tick.timestamp, "size": remaining_size, "type": order_type})
            state.fill_status = f"PARTIAL_FILL ({abs(fill_size)}/{abs(size)})"
        else:
            state.fill_status = "FULL_FILL"
            
        if fill_size == 0:
            return  
            
        if order_type == "TAKER":
            slippage_penalty = 0.5 * (abs(fill_size) / available_vol) * (tick.best_ask - tick.best_bid)
            exec_price = (tick.best_ask + slippage_penalty) if fill_size > 0 else (tick.best_bid - slippage_penalty)
            fee_rate = self.fee_taker
        else:
            exec_price = tick.best_bid if fill_size > 0 else tick.best_ask
            fee_rate = self.fee_maker

        fee = abs(fill_size) * exec_price * fee_rate
        is_closing_trade = (self.position > 0 and fill_size < 0) or (self.position < 0 and fill_size > 0)
        
        if is_closing_trade:
            pnl = (exec_price - self.entry_price) * abs(fill_size) * (1 if self.position > 0 else -1)
            state.realized_pnl += (pnl - fee)
            self.position += fill_size
            if abs(self.position) < 1e-8:
                self.position = 0.0
                self.entry_price = 0.0
        else:
            old_value = self.position * self.entry_price
            new_value = fill_size * exec_price
            self.position += fill_size
            self.entry_price = abs((old_value + new_value) / self.position)
            
            state.realized_pnl -= fee 
            
        state.position_size = self.position

# ==========================================
# 4. BOT ORCHESTRATOR & TRACKING
# ==========================================

class ProtoBotV4:
    def __init__(self):
        self.signal_engine = SignalEngine()
        self.execution_engine = ExecutionEngine(latency_ms=30) 
        self.diagnostics_log = []
        
    def on_tick(self, tick: OrderbookTick):
        state = DiagnosticState(timestamp=tick.timestamp)
        
        state = self.signal_engine.process_tick(tick, state)
        self.execution_engine.process_fills(tick, state)
        self.execution_engine.manage_risk(tick, state)
        
        in_flight_orders = len(self.execution_engine.pending_orders)
        
        if state.entry_signal and self.execution_engine.position == 0 and in_flight_orders == 0:
            size = 1.0 if state.entry_signal == "BUY" else -1.0
            self.execution_engine.route_order(tick, size, "TAKER", state)
            
        state.position_size = self.execution_engine.position
        
        self.diagnostics_log.append(state)

    def generate_statistics(self):
        print("\n=== EQUITY / DRAWDOWN / STATISTICS ===")
        pnls = [s.realized_pnl for s in self.diagnostics_log]
        equity_curve = np.cumsum(pnls)
        
        if len(equity_curve) > 0:
            peak = np.maximum.accumulate(equity_curve)
            drawdown = (peak - equity_curve)
            max_dd = np.max(drawdown)
            
            total_fills = len([s for s in self.diagnostics_log if "FILL" in s.fill_status])
            
            print(f"Total Fill Events Evaluated: {total_fills}")
            print(f"Final Equity PnL:            ${equity_curve[-1]:.4f}")
            print(f"Max Drawdown:                ${max_dd:.4f}")
        else:
            print("No trades executed.")

if __name__ == "__main__":
    bot = ProtoBotV4()
    
    mock_data = [
        OrderbookTick(1.00, 100.0, 100.5, 10, 10),
        OrderbookTick(1.01, 100.1, 100.5, 50, 5),   
        OrderbookTick(1.02, 100.2, 100.6, 100, 2),  
        OrderbookTick(1.04, 100.3, 100.7, 80, 0.5), 
        OrderbookTick(1.05, 100.4, 100.7, 80, 10),  
        OrderbookTick(1.06, 102.5, 103.0, 20, 20),  
        OrderbookTick(1.07, 102.5, 103.0, 20, 20),  
        OrderbookTick(1.09, 102.5, 103.0, 20, 20),  
    ]
    
    for tick in mock_data:
        bot.on_tick(tick)
        bot.diagnostics_log[-1].print_diagnostics()
        
    bot.generate_statistics()
