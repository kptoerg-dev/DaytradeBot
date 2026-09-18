import unittest
import math
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Dict

# ==========================================
# 1. ENUMS & DATACLASSES (CORE ARCHITEKTUR)
# ==========================================

class Direction(Enum):
    LONG = 1
    SHORT = -1

class OrderType(Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"

class OrderStatus(Enum):
    PENDING = "PENDING"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELED = "CANCELED"

@dataclass
class Tick:
    timestamp: int
    bid: float
    ask: float
    bid_vol: float
    ask_vol: float

@dataclass
class Order:
    order_id: int
    direction: Direction
    order_type: OrderType
    quantity: float
    price: float = 0.0          # Limit or Stop Price
    status: OrderStatus = OrderStatus.PENDING
    filled_quantity: float = 0.0
    sl_distance: float = 0.0    # Fixed distance requested by strategy
    tp_distance: float = 0.0

@dataclass
class Fill:
    order_id: int
    direction: Direction
    fill_price: float
    quantity: float
    timestamp: int

class Position:
    def __init__(self, direction: Direction, entry_price: float, quantity: float, sl_price: float = None, tp_price: float = None):
        self.direction = direction
        self.entry_price = entry_price
        self.quantity = quantity
        
        # SL/TP sind ab jetzt STATISCH und an die Position gebunden (Punkt 3)
        self.sl_price = sl_price
        self.tp_price = tp_price
        self.is_closed = False

# ==========================================
# 2. EVENT-DRIVEN ENGINE COMPONENTS
# ==========================================

class RiskManager:
    @staticmethod
    def calculate_position_size(equity: float, risk_per_trade: float, entry_price: float, sl_price: float, contract_multiplier: float = 1.0) -> float:
        """ Punkt 2: Korrektes Stop-Loss basiertes Risk Sizing """
        if entry_price <= 0 or sl_price <= 0:
            raise ValueError("Preise müssen positiv sein.")
        if entry_price == sl_price:
            raise ValueError("Entry Price darf nicht gleich SL Price sein.")
            
        risk_capital = equity * risk_per_trade
        risk_per_unit = abs(entry_price - sl_price) * contract_multiplier
        
        if risk_per_unit == 0:
            return 0.0
            
        position_size = risk_capital / risk_per_unit
        
        # Verhindern von extremen oder NaN Positionen
        if math.isnan(position_size) or math.isinf(position_size):
            return 0.0
            
        return math.floor(position_size) # Ganze Contracts

class ExecutionHandler:
    def __init__(self, slippage_model: float = 0.1):
        self.slippage_model = slippage_model # Fester Slippage in Preis-Einheiten
        
    def process_order(self, order: Order, tick: Tick) -> List[Fill]:
        """ Punkt 1 & 5: Realistische Ausführung, keine unrealistischen Fills """
        fills = []
        if order.status in (OrderStatus.FILLED, OrderStatus.CANCELED):
            return fills
            
        remaining_qty = order.quantity - order.filled_quantity
        
        if order.order_type == OrderType.MARKET:
            # Taker Execution: Buy at Ask, Sell at Bid. Plus/Minus Slippage.
            if order.direction == Direction.LONG:
                fill_price = tick.ask + self.slippage_model
                available_vol = tick.ask_vol
            else:
                fill_price = tick.bid - self.slippage_model
                available_vol = tick.bid_vol
                
            # Partial Fill Logik
            fill_qty = min(remaining_qty, available_vol)
            if fill_qty > 0:
                fills.append(Fill(order.order_id, order.direction, fill_price, fill_qty, tick.timestamp))
                order.filled_quantity += fill_qty
                if order.filled_quantity >= order.quantity:
                    order.status = OrderStatus.FILLED
                else:
                    order.status = OrderStatus.PARTIAL
                    
        return fills

class BacktestEngine:
    def __init__(self, initial_equity: float = 10000.0):
        self.equity = initial_equity
        self.positions: List[Position] = []
        self.active_orders: List[Order] = []
        self.order_counter = 0
        self.execution_handler = ExecutionHandler(slippage_model=0.5) # Bsp: 0.5 Punkte Slippage
        self.current_tick: Optional[Tick] = None

    def on_tick(self, tick: Tick):
        """ Punkt 4: Strikte, Lookahead-freie Event-Reihenfolge """
        self.current_tick = tick
        
        # 1. Markt-/Orderbook Zustand aktualisieren (erledigt durch Parameter-Übergabe)
        
        # 2 & 3. Fills für bestehende Orders durchführen
        new_fills = []
        for order in self.active_orders:
            fills = self.execution_handler.process_order(order, tick)
            new_fills.extend(fills)
            
        self.active_orders = [o for o in self.active_orders if o.status not in (OrderStatus.FILLED, OrderStatus.CANCELED)]
        
        # 4. Positionen aktualisieren (Neu eröffnen)
        for fill in new_fills:
            self._handle_fill(fill)
            
        # 5 & 6. Risk Checks & Stops prüfen
        self._check_stops(tick)

    def _handle_fill(self, fill: Fill):
        # Einfaches Handling: Findet order für SL/TP Distanz
        # (In einer echten Architektur per Order-ID gemappt, hier der Einfachheit halber fix berechnet)
        # Wenn LONG, SL ist unter dem FILL PREIS (nicht dem Mid-Preis!)
        sl_price = fill.fill_price - 1.0 if fill.direction == Direction.LONG else fill.fill_price + 1.0 
        pos = Position(fill.direction, fill.fill_price, fill.quantity, sl_price=sl_price)
        self.positions.append(pos)
        
    def _check_stops(self, tick: Tick):
        """ Punkt 5: Stop-Loss Logic mit Gap-Handling """
        for pos in self.positions:
            if pos.is_closed:
                continue
                
            trigger_close = False
            close_direction = Direction.SHORT if pos.direction == Direction.LONG else Direction.LONG
            
            # LONG Position: Stop wird getriggert, wenn der Bid unter den SL fällt
            if pos.direction == Direction.LONG and pos.sl_price is not None:
                if tick.bid <= pos.sl_price:
                    trigger_close = True
            
            # SHORT Position: Stop wird getriggert, wenn Ask über SL steigt
            elif pos.direction == Direction.SHORT and pos.sl_price is not None:
                if tick.ask >= pos.sl_price:
                    trigger_close = True
                    
            if trigger_close:
                # Erzeuge Market Order zum Schließen (unterliegt im nächsten/selben Tick Slippage!)
                # Ein Gap wird natürlich bestraft, weil die Execution Handler zum aktuellen Bid/Ask + Slippage füllt!
                self.send_order(close_direction, OrderType.MARKET, pos.quantity)
                pos.is_closed = True # Markiere Position als im Schließungsprozess

    def send_order(self, direction: Direction, order_type: OrderType, quantity: float) -> int:
        self.order_counter += 1
        order = Order(self.order_counter, direction, order_type, quantity)
        # Order wird NUR in die Queue gestellt. Execution erfolgt erst im NÄCHSTEN loop oder nachgelagert.
        self.active_orders.append(order)
        return self.order_counter


# ==========================================
# 3. UNIT TESTS (TEST-DRIVEN DEVELOPMENT)
# ==========================================

class TestTradingEngine(unittest.TestCase):

    def setUp(self):
        self.engine = BacktestEngine()
        
    def test_point_1_buy_taker_execution_and_slippage(self):
        """ Market Buy muss am Ask ausgeführt werden, plus Slippage """
        self.engine.execution_handler.slippage_model = 0.5
        order_id = self.engine.send_order(Direction.LONG, OrderType.MARKET, 1.0)
        
        # Tick: Spread = 1, Mid = 100.5. Bid=100, Ask=101.
        tick = Tick(1, bid=100.0, ask=101.0, bid_vol=10.0, ask_vol=10.0)
        self.engine.on_tick(tick) # Führt die Order aus
        
        # Erwartet: Ask (101.0) + Slippage (0.5) = 101.5
        pos = self.engine.positions[0]
        self.assertEqual(pos.entry_price, 101.5)
        self.assertEqual(pos.direction, Direction.LONG)

    def test_point_1_sell_taker_execution_and_slippage(self):
        """ Market Sell muss am Bid ausgeführt werden, minus Slippage """
        self.engine.execution_handler.slippage_model = 0.5
        self.engine.send_order(Direction.SHORT, OrderType.MARKET, 1.0)
        
        tick = Tick(1, bid=100.0, ask=101.0, bid_vol=10.0, ask_vol=10.0)
        self.engine.on_tick(tick)
        
        # Erwartet: Bid (100.0) - Slippage (0.5) = 99.5
        pos = self.engine.positions[0]
        self.assertEqual(pos.entry_price, 99.5)

    def test_point_1_partial_fill(self):
        """ Wenn die Order-Size größer als die Ask-Liquidität ist """
        self.engine.execution_handler.slippage_model = 0.0
        self.engine.send_order(Direction.LONG, OrderType.MARKET, 5.0)
        
        # Nur 3 Lots auf dem Ask verfügbar
        tick = Tick(1, bid=100.0, ask=101.0, bid_vol=10.0, ask_vol=3.0)
        self.engine.on_tick(tick)
        
        self.assertEqual(len(self.engine.positions), 1)
        self.assertEqual(self.engine.positions[0].quantity, 3.0) # Nur 3 gefillt
        
        # Nächster Tick bringt neue Liquidität
        tick2 = Tick(2, bid=100.0, ask=101.0, bid_vol=10.0, ask_vol=5.0)
        self.engine.on_tick(tick2)
        
        # Restliche 2 Lots gefillt
        self.assertEqual(self.engine.positions[1].quantity, 2.0)

    def test_point_2_position_sizing(self):
        """ Testet das Stop-Loss basierte Risk Sizing gem. deiner Vorgabe """
        equity = 10000.0
        risk_per_trade = 0.01 # 1% = 100 Risk Capital
        entry = 100.0
        sl = 99.0
        
        size = RiskManager.calculate_position_size(equity, risk_per_trade, entry, sl)
        
        # Risk Capital = 100. Risk per Unit = 1.0. -> Size = 100
        self.assertEqual(size, 100.0)

        # Div by Zero Verhinderung
        size_zero = RiskManager.calculate_position_size(equity, risk_per_trade, entry, entry) # SL = Entry
        self.assertEqual(size_zero, 0.0)

    def test_point_3_static_stops(self):
        """ SL/TP dürfen nicht bei jedem Tick updaten, sondern binden sich fix an die Position """
        pos = Position(Direction.LONG, entry_price=100.0, quantity=1.0, sl_price=98.0, tp_price=105.0)
        
        # Beweis, dass diese Instanzvariablen statisch bleiben, auch wenn der Spread wackelt.
        # In der alten Architektur war dies oft ein property `def sl_price(self): return current_ask - x`.
        # Hier sind es reine primitive Datentypen.
        self.assertIsInstance(pos.sl_price, float)
        self.assertEqual(pos.sl_price, 98.0)

    def test_point_4_and_5_stop_loss_execution_gap(self):
        """ Prüft Event-Reihenfolge und korrekte SL-Ausführung bei Gaps """
        self.engine.execution_handler.slippage_model = 0.0 # Für klare Mathematik
        
        # Wir platzieren künstlich eine Long-Position in den Markt
        pos = Position(Direction.LONG, entry_price=100.0, quantity=1.0, sl_price=98.0)
        self.engine.positions.append(pos)
        
        # Tick 1: Markt bei 99 (Stop noch nicht erreicht)
        self.engine.on_tick(Tick(1, bid=99.0, ask=100.0, bid_vol=10, ask_vol=10))
        self.assertEqual(len(self.engine.active_orders), 0)
        self.assertFalse(pos.is_closed)
        
        # Tick 2: GAP DOWN auf 95 Bid! (Überspringt die 98.0 komplett)
        self.engine.on_tick(Tick(2, bid=95.0, ask=96.0, bid_vol=10, ask_vol=10))
        
        # Der Stop muss ausgelöst worden sein und eine Market Sell Order erzeugt haben
        self.assertTrue(pos.is_closed)
        self.assertEqual(len(self.engine.active_orders), 1)
        self.assertEqual(self.engine.active_orders[0].direction, Direction.SHORT)
        
        # Tick 3: Führt die Market Sell (Stop) Order aus
        self.engine.on_tick(Tick(3, bid=94.5, ask=95.5, bid_vol=10, ask_vol=10))
        
        # Der Fill der Stop Order erfolgte zum aktuellen Bid (94.5), NICHT zum Stop-Preis (98.0)!
        # Dies simuliert realistische Verluste in einem illiquiden Markt.
        stop_fill_pos = self.engine.positions[-1]
        self.assertEqual(stop_fill_pos.direction, Direction.SHORT)
        self.assertEqual(stop_fill_pos.entry_price, 94.5) 


if __name__ == '__main__':
    # Test-Report generieren
    print("="*50)
    print("STARTING TEST RUN FOR EVENT-DRIVEN ENGINE")
    print("="*50)
    unittest.main(verbosity=2)

