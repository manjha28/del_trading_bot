"""Deterministic VPVR unit tests. No credentials, network or live orders are used."""
from __future__ import annotations
import tempfile
import unittest
from datetime import date
from pathlib import Path
import numpy as np
import pandas as pd

from vpvr.config import VPVRConfig
from vpvr.profile import build_volume_profile, _value_area
from vpvr.indicators import cumulative_volume_delta, trade_delta_bars, delta_divergence
from vpvr.risk import RiskEngine
from vpvr.strategies import StrategySignal, _make
from vpvr.paper import PaperBroker
from vpvr.data import DeltaMarketData
from vpvr.backtest import BacktestConfig, run_backtest


class ProfileTests(unittest.TestCase):
    def test_value_area_expands_to_larger_adjacent_bin(self):
        prices=np.array([1.,2.,3.,4.,5.,6.])
        volumes=np.array([0.,0.,5.,10.,6.,0.])
        poc,vah,val=_value_area(volumes,prices,.70)
        self.assertEqual(poc,4.0)
        self.assertEqual(vah,5.0)
        self.assertEqual(val,4.0)

    def test_trade_profile_uses_trade_prices_and_preserves_volume(self):
        trades=pd.DataFrame({"price":[100,100,101,102,102,103],"volume":[1,9,2,1,7,1]})
        profile=build_volume_profile(trades=trades,rows=50,value_area_pct=.70)
        self.assertEqual(profile.source,"trade_prints")
        self.assertAlmostEqual(profile.total_volume,21.0)
        self.assertLessEqual(profile.val,profile.poc)
        self.assertLessEqual(profile.poc,profile.vah)

    def test_candle_profile_is_labeled_approximation(self):
        candles=pd.DataFrame({"time":pd.date_range("2026-01-01",periods=2,freq="1min",tz="UTC"),
            "high":[101.,102.],"low":[100.,101.],"volume":[5.,7.]})
        profile=build_volume_profile(candles,rows=50)
        self.assertEqual(profile.source,"ohlcv_uniform_range_approximation")
        self.assertAlmostEqual(profile.total_volume,12.0)

    def test_profile_rejects_invalid_rows_and_zero_volume(self):
        with self.assertRaises(ValueError):build_volume_profile(pd.DataFrame(),rows=49)
        empty=pd.DataFrame({"high":[1.],"low":[1.],"volume":[0.]})
        with self.assertRaises(ValueError):build_volume_profile(empty)


class IndicatorTests(unittest.TestCase):
    def test_cvd_requires_aggressor_side(self):
        unavailable=cumulative_volume_delta(pd.DataFrame({"volume":[2,1]}))
        self.assertFalse(unavailable["available"])
        trades=pd.DataFrame({"time":pd.to_datetime(["2026-01-01T00:00:01Z","2026-01-01T00:00:02Z","2026-01-01T00:01:01Z"]),
            "side":["buy","sell","buy"],"volume":[3.,1.,2.]})
        result=cumulative_volume_delta(trades)
        self.assertTrue(result["available"])
        self.assertEqual(result["value"],4.0)
        bars=trade_delta_bars(trades)
        self.assertAlmostEqual(float(bars.iloc[-1]["cvd"]),4.0)

    def test_divergence_is_unavailable_without_real_delta_bars(self):
        candles=pd.DataFrame({"close":[100,99,98,97,96]})
        result=delta_divergence(candles,pd.DataFrame(),lookback=5)
        self.assertFalse(result["available"])

    def test_divergence_flags_price_lower_low_cvd_higher_low(self):
        candles=pd.DataFrame({"close":[100,98,99,97,98]})
        deltas=pd.DataFrame({"cvd":[10,5,7,8,9]})
        result=delta_divergence(candles,deltas,lookback=5)
        self.assertTrue(result["available"])
        self.assertTrue(result["bullish"])


class RiskTests(unittest.TestCase):
    def setUp(self):self.cfg=VPVRConfig()

    def test_size_rounds_down_and_respects_risk_budget(self):
        engine=RiskEngine(self.cfg);engine.reset_day(1000,date(2026,1,1))
        decision=engine.size(equity=1000,entry=100,stop=99,direction="LONG",
            leverage=10,contract_multiplier=1,lot_size=.1,min_qty=.1,tick_size=.01,
            fee_rate=.0005,slippage_bps=2)
        self.assertTrue(decision.accepted,decision.reason)
        self.assertLessEqual(decision.risk_amount,5.0+1e-9)
        too_small=engine.size(equity=1000,entry=100,stop=99,direction="LONG",
            leverage=10,contract_multiplier=1,lot_size=1,min_qty=10,tick_size=.01)
        self.assertFalse(too_small.accepted)
        self.assertIn("below exchange minimum",too_small.reason)

    def test_consecutive_loss_lockout_persists_across_day(self):
        engine=RiskEngine(self.cfg);engine.reset_day(1000,date(2026,1,1))
        engine.record_close(-1);engine.record_close(-1);engine.record_close(-1)
        engine.update_equity(999,date(2026,1,2))
        decision=engine.size(equity=999,entry=100,stop=99,direction="LONG")
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason,"consecutive loss lockout")

    def test_daily_loss_lockout_uses_equity(self):
        engine=RiskEngine(self.cfg);engine.reset_day(1000,date(2026,1,1))
        decision=engine.size(equity=980,entry=100,stop=99,direction="LONG")
        self.assertFalse(decision.accepted)
        self.assertIn("daily equity drawdown",decision.reason)


class PaperTests(unittest.TestCase):
    def signal(self):
        return StrategySignal("BTCUSD","poc_reclaim","LONG",101.,99.,(105.,109.),2.0,
            "2026-01-01T00:00:00+00:00",("test",),("invalidate",),100.)

    def candle(self,t,open_,high,low,close):
        return {"time":pd.Timestamp(t),"open":open_,"high":high,"low":low,"close":close,"volume":10}

    def test_pending_entry_cannot_fill_on_signal_bar_and_is_deduplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            broker=PaperBroker(Path(directory)/"paper.json",initial_balance=1000)
            ok,_=broker.queue_signal(self.signal(),1)
            self.assertTrue(ok)
            same=self.candle("2026-01-01T00:00:00Z",100,102,100,101)
            self.assertEqual(broker.process_candle("BTCUSD",same),[])
            later=self.candle("2026-01-01T00:01:00Z",100,102,100,101.5)
            events=broker.process_candle("BTCUSD",later)
            self.assertTrue(any(e["event"]=="entry_filled" for e in events))
            self.assertEqual(len(broker.state["positions"]),1)
            self.assertEqual(broker.process_candle("BTCUSD",later),[])

    def test_stop_wins_when_stop_and_target_touch_same_bar(self):
        with tempfile.TemporaryDirectory() as directory:
            broker=PaperBroker(Path(directory)/"paper.json",initial_balance=1000,slippage_bps=0)
            broker.queue_signal(self.signal(),1)
            broker.process_candle("BTCUSD",self.candle("2026-01-01T00:00:00Z",100,102,100,101))
            broker.process_candle("BTCUSD",self.candle("2026-01-01T00:01:00Z",100,102,100,101))
            events=broker.process_candle("BTCUSD",self.candle("2026-01-01T00:02:00Z",101,106,98,103))
            self.assertTrue(any(e.get("reason")=="STOP" for e in events))
            self.assertNotIn("BTCUSD",broker.state["positions"])
            self.assertEqual(broker.state["trades"][-1]["exit_reason"],"STOP")

    def test_corrupt_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"paper.json";path.write_text("{broken")
            with self.assertRaises(RuntimeError):PaperBroker(path)


class DataTests(unittest.TestCase):
    def test_freshness_detects_gaps(self):
        data=DeltaMarketData(config=VPVRConfig())
        now=pd.Timestamp("2026-01-01T00:03:00Z").timestamp()
        good=pd.DataFrame({"time":pd.to_datetime(["2026-01-01T00:01:00Z","2026-01-01T00:02:00Z"],utc=True)})
        self.assertTrue(data.freshness(good,"1m",now=now)[0])
        gap=pd.DataFrame({"time":pd.to_datetime(["2026-01-01T00:00:00Z","2026-01-01T00:02:00Z"],utc=True)})
        self.assertFalse(data.freshness(gap,"1m",now=now)[0])


class BacktestTests(unittest.TestCase):
    def test_backtest_runs_without_trades_on_flat_data(self):
        base=pd.Timestamp("2026-01-01T00:00:00Z")
        def frame(count,minutes):
            times=[base+pd.Timedelta(minutes=minutes*i) for i in range(count)]
            return pd.DataFrame({"time":times,"open":[100.]*count,"high":[100.2]*count,
                "low":[99.8]*count,"close":[100.]*count,"volume":[10.]*count})
        result=run_backtest("BTCUSD",frame(600,1),frame(200,3),frame(40,15),BacktestConfig())
        self.assertEqual(result["metrics"]["trades"],0)
        self.assertEqual(result["metrics"]["ending_equity"],1000.0)
        self.assertIsNone(result["open_position_at_end"])


if __name__=="__main__":
    unittest.main()
