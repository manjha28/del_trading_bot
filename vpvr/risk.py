"""Centralized, conservative paper-risk engine. No live order path exists here."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date
import math

@dataclass(frozen=True)
class RiskDecision:
    accepted: bool
    reason: str
    quantity: float = 0.0
    notional: float = 0.0
    risk_amount: float = 0.0
    margin_required: float = 0.0
    leverage: float = 0.0

class RiskEngine:
    def __init__(self, config):
        self.config=config
        self.daily_start_equity=None
        self.daily_realized_pnl=0.0
        self.consecutive_losses=0
        self.day=None
        self.current_equity=None

    def reset_day(self,equity:float,day:date|None=None):
        # The daily loss reference resets each session; a consecutive losing streak
        # remains active across midnight until a winning/flat close interrupts it.
        self.daily_start_equity=float(equity)
        self.daily_realized_pnl=0.0
        self.day=day or date.today()
        self.current_equity=float(equity)

    def update_equity(self,equity:float,day:date|None=None):
        today=day or date.today()
        if self.day!=today or self.daily_start_equity is None:
            self.reset_day(equity,today)
        self.current_equity=float(equity)

    def record_close(self,pnl:float):
        pnl=float(pnl)
        self.daily_realized_pnl+=pnl
        self.consecutive_losses=self.consecutive_losses+1 if pnl<0 else 0

    def size(self,*,equity:float,entry:float,stop:float,direction:str,leverage:float=10,
             contract_multiplier:float|None=1.0,lot_size:float|None=1.0,min_qty:float|None=1.0,
             tick_size:float|None=None,fee_rate:float=0.0,slippage_bps:float=0.0)->RiskDecision:
        if self.daily_start_equity is None:
            self.reset_day(equity)
        self.current_equity=float(equity)
        if equity<=0 or self.daily_start_equity<=0:
            return RiskDecision(False,"account equity unavailable")
        if equity<=self.daily_start_equity*(1-self.config.daily_loss_limit_pct):
            return RiskDecision(False,"daily equity drawdown lockout (includes unrealized PnL)")
        if self.consecutive_losses>=self.config.max_consecutive_losses:
            return RiskDecision(False,"consecutive loss lockout")
        if contract_multiplier is None or lot_size is None or min_qty is None:
            return RiskDecision(False,"unknown contract specification")
        if entry<=0 or contract_multiplier<=0 or lot_size<=0 or min_qty<=0:
            return RiskDecision(False,"invalid price or contract specification")
        if leverage<1 or leverage>self.config.max_leverage:
            return RiskDecision(False,"leverage outside allowed range")
        if direction not in ("LONG","SHORT"):
            return RiskDecision(False,"unsupported direction")
        distance=(entry-stop) if direction=="LONG" else (stop-entry)
        if distance<=0:
            return RiskDecision(False,"stop is on the wrong side of entry")
        if tick_size is not None:
            if tick_size<=0:
                return RiskDecision(False,"invalid tick size")
            stop=round(stop/tick_size)*tick_size
            distance=(entry-stop) if direction=="LONG" else (stop-entry)
            if distance<=0:
                return RiskDecision(False,"tick rounding invalidated stop")
        costs=entry*(fee_rate*2+slippage_bps/10000*2)
        risk_per_contract=(distance+costs)*contract_multiplier
        if risk_per_contract<=0:
            return RiskDecision(False,"invalid risk per contract")
        risk_budget=equity*self.config.risk_per_trade_pct
        qty=math.floor((risk_budget/risk_per_contract)/lot_size)*lot_size
        # Never round up to the exchange minimum: that could exceed risk.
        if qty+1e-12<min_qty:
            return RiskDecision(False,"risk-sized quantity below exchange minimum")
        notional=qty*entry*contract_multiplier
        margin=notional/leverage
        if margin>equity*0.9:
            return RiskDecision(False,"insufficient margin after 10% reserve")
        if notional>equity*leverage:
            return RiskDecision(False,"notional exceeds configured leverage cap")
        return RiskDecision(True,"risk checks passed",qty,notional,qty*risk_per_contract,margin,leverage)
