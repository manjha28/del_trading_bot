"""Deterministic VPVR unit tests. No credentials, network or live orders are used."""
from __future__ import annotations
import tempfile
import unittest
import json
from datetime import date
from pathlib import Path
from unittest.mock import patch
import numpy as np
import pandas as pd

from vpvr.config import VPVRConfig
from vpvr.profile import build_volume_profile, _value_area
from vpvr.indicators import cumulative_volume_delta, trade_delta_bars, delta_divergence
from vpvr.risk import RiskEngine
from vpvr.strategies import StrategySignal, _make
from vpvr.paper import PaperBroker
from vpvr.data import DeltaMarketData
from vpvr.cli import _current_paper_marks
from vpvr.stream import DeltaPublicStream
from vpvr.backtest import BacktestConfig, run_backtest, _completed_1m_history, _quantity
from vpvr.engine import VPVREngine


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

    def test_profile_snapshot_exposes_volume_at_price_arrays(self):
        candles=pd.DataFrame({"high":[101.],"low":[100.],"volume":[10.]})
        profile=build_volume_profile(candles,rows=50)
        snapshot=profile.as_dict()
        self.assertEqual(len(snapshot["price_levels"]),50)
        self.assertEqual(len(snapshot["volume_at_price"]),50)
        self.assertAlmostEqual(sum(snapshot["volume_at_price"]),10.0)


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
        candles=pd.DataFrame({"time":pd.date_range("2026-01-01",periods=5,freq="1min",tz="UTC"),
            "close":[100,98,99,97,98]})
        deltas=pd.DataFrame({"cvd":[10,5,7,8,9]},
            index=pd.date_range("2026-01-01",periods=5,freq="1min",tz="UTC"))
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


    def test_btc_contract_multiplier_sizes_integer_order_contracts(self):
        engine = RiskEngine(self.cfg)
        engine.reset_day(1000, date(2026, 1, 1))
        decision = engine.size(
            equity=1000, entry=80000, stop=79000, direction="LONG",
            leverage=10, contract_multiplier=0.001, lot_size=1, min_qty=1,
            tick_size=0.5, fee_rate=0.0005, slippage_bps=2, max_qty=125000,
        )
        self.assertTrue(decision.accepted, decision.reason)
        self.assertEqual(decision.quantity, 4)
        self.assertIsInstance(decision.quantity, int)
        self.assertEqual(decision.notional, 320.0)

    def test_exchange_position_size_limit_caps_quantity(self):
        engine=RiskEngine(self.cfg);engine.reset_day(1000,date(2026,1,1))
        decision=engine.size(equity=1000,entry=100,stop=99,direction="LONG",
            leverage=10,contract_multiplier=1,lot_size=1,min_qty=1,tick_size=.01,
            fee_rate=.0005,slippage_bps=2,max_qty=2)
        self.assertTrue(decision.accepted,decision.reason)
        self.assertEqual(decision.quantity,2)

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

    def test_tick_rounding_never_moves_stop_toward_entry(self):
        engine=RiskEngine(self.cfg);engine.reset_day(1000,date(2026,1,1))
        long=engine.size(equity=1000,entry=100,stop=99.16,direction="LONG",
            leverage=10,contract_multiplier=1,lot_size=1,min_qty=1,tick_size=0.1,
            fee_rate=.0005,slippage_bps=2)
        short=engine.size(equity=1000,entry=100,stop=100.84,direction="SHORT",
            leverage=10,contract_multiplier=1,lot_size=1,min_qty=1,tick_size=0.1,
            fee_rate=.0005,slippage_bps=2)
        self.assertTrue(long.accepted,long.reason)
        self.assertTrue(short.accepted,short.reason)
        self.assertEqual(long.quantity,4)
        self.assertEqual(short.quantity,4)


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

    def test_two_targets_leave_runner_and_trail_structurally(self):
        with tempfile.TemporaryDirectory() as directory:
            broker=PaperBroker(Path(directory)/"paper.json",initial_balance=1000,
                fee_rate=0,slippage_bps=0,trailing_buffer_pct=.001)
            broker.queue_signal(self.signal(),1)
            broker.process_candle("BTCUSD",self.candle("2026-01-01T00:00:00Z",100,102,100,101))
            broker.process_candle("BTCUSD",self.candle("2026-01-01T00:01:00Z",100,102,100,101))
            broker.process_candle("BTCUSD",self.candle("2026-01-01T00:02:00Z",101,106,102,105))
            broker.process_candle("BTCUSD",self.candle("2026-01-01T00:03:00Z",106,110,106,109))
            position=broker.state["positions"]["BTCUSD"]
            self.assertAlmostEqual(position["remaining"],.25)
            self.assertEqual(position["target_index"],2)
            self.assertEqual(position["stop"],105.0)
            broker.process_candle("BTCUSD",self.candle("2026-01-01T00:04:00Z",108,109,107,108))
            tightened=broker.state["positions"]["BTCUSD"]["stop"]
            self.assertGreater(tightened,105.0)
            events=broker.process_candle("BTCUSD",self.candle("2026-01-01T00:05:00Z",106,108,105.8,106))
            self.assertTrue(any(e.get("reason")=="TRAIL_STOP" for e in events))
            self.assertNotIn("BTCUSD",broker.state["positions"])

    def test_partial_target_then_tightened_stop_is_conservative_same_bar(self):
        with tempfile.TemporaryDirectory() as directory:
            broker=PaperBroker(Path(directory)/"paper.json",initial_balance=1000,
                fee_rate=0,slippage_bps=0)
            broker.queue_signal(self.signal(),1)
            broker.process_candle("BTCUSD",self.candle("2026-01-01T00:00:00Z",100,102,100,101))
            broker.process_candle("BTCUSD",self.candle("2026-01-01T00:01:00Z",100,102,100,101))
            events=broker.process_candle("BTCUSD",self.candle("2026-01-01T00:02:00Z",101,106,100,103))
            self.assertTrue(any(e.get("reason")=="SAME_BAR_TIGHTENED_STOP" for e in events))
            self.assertNotIn("BTCUSD",broker.state["positions"])

    def test_funding_requires_an_explicit_timestamped_settlement_event(self):
        with tempfile.TemporaryDirectory() as directory:
            broker=PaperBroker(Path(directory)/"paper.json",initial_balance=1000,
                fee_rate=0,slippage_bps=0)
            broker.queue_signal(self.signal(),1)
            broker.process_candle("BTCUSD",self.candle("2026-01-01T00:00:00Z",100,102,100,101))
            broker.process_candle("BTCUSD",self.candle("2026-01-01T00:01:00Z",100,102,100,101))
            before=broker.balance
            # A bare rate is not a funding settlement and cannot change cash.
            broker.process_candle("BTCUSD",self.candle("2026-01-01T00:02:00Z",101,102,100,101.5))
            self.assertAlmostEqual(broker.balance,before)
            # A real timestamped event within the 1m bar is charged once.
            event={"timestamp":"2026-01-01T00:03:30Z","rate":0.001,"source":"test"}
            events=broker.process_candle("BTCUSD",self.candle("2026-01-01T00:03:00Z",101,102,100,101.5),
                funding_event=event)
            self.assertTrue(any(e["event"]=="funding" for e in events))
            self.assertLess(broker.balance,before)


class DataTests(unittest.TestCase):

    def test_paper_equity_marks_use_current_ticker_and_last_confirmed_fallback(self):
        positions = {
            "BTCUSD": {"entry": 79000.0, "last_mark": 80000.0},
            "ETHUSD": {"entry": 2900.0, "last_mark": 3000.0},
            "SOLUSD": {"entry": 100.0, "last_mark": 101.0},
        }
        tickers = {
            "BTCUSD": {"mark_price": "81000"},
            "ETHUSD": {"mark_price": None, "close": "3050"},
            "SOLUSD": {},
        }
        marks = _current_paper_marks(positions, tickers)
        self.assertEqual(marks, {"BTCUSD": 81000.0, "ETHUSD": 3050.0, "SOLUSD": 101.0})

    def test_public_trade_loader_preserves_identical_distinct_execution_rows(self):
        data=DeltaMarketData(config=VPVRConfig())
        duplicate={"price":100.0,"size":2.0,"side":"buy","timestamp":1767225600000000}
        payload={"result":{"trades":[duplicate.copy(),duplicate.copy()]}}
        with patch.object(data,"_get_json",return_value=payload):
            trades=data.fetch_public_trades("BTCUSD")
        self.assertEqual(len(trades),2)
        self.assertEqual(float(trades.volume.sum()),4.0)

    def test_funding_history_loader_and_staleness_check(self):
        data=DeltaMarketData(config=VPVRConfig())
        now=1767225600
        payload={"result":[{"time":now-3600,"close":0.0002}]}
        with patch.object(data,"_get_json",return_value=payload):
            self.assertAlmostEqual(data.fetch_current_funding_rate("BTCUSD",now=now),0.0002)
        stale={"result":[{"time":now-12*3600,"close":0.0002}]}
        with patch.object(data,"_get_json",return_value=stale):
            with self.assertRaisesRegex(Exception,"stale"):
                data.fetch_current_funding_rate("BTCUSD",now=now)


    def _valid_product(self, symbol="BTCUSD", **overrides):
        defaults = {
            "symbol": symbol,
            "contract_type": "perpetual_futures",
            "state": "live",
            "trading_status": "operational",
            "notional_type": "vanilla",
            "is_quanto": False,
            "contract_value": "0.001" if symbol == "BTCUSD" else "0.01",
            "contract_unit_currency": "BTC" if symbol == "BTCUSD" else "ETH",
            "tick_size": "0.5" if symbol == "BTCUSD" else "0.05",
            "position_size_limit": "125000",
        }
        return {**defaults, **overrides}

    def test_btc_contract_specs_verify_against_documented_minimum(self):
        specs = DeltaMarketData(config=VPVRConfig()).contract_specs(self._valid_product())
        self.assertTrue(specs["spec_verified"], specs["verification_errors"])
        self.assertEqual(specs["contract_multiplier"], 0.001)
        self.assertEqual(specs["contract_unit_currency"], "BTC")
        self.assertEqual(specs["lot_size"], 1)
        self.assertEqual(specs["min_qty"], 1)
        self.assertEqual(specs["minimum_underlying_qty"], "0.001")
        self.assertEqual(specs["tick_size"], 0.5)
        self.assertEqual(specs["position_size_limit"], 125000)
        self.assertTrue(specs["lot_increment_verified"])
        self.assertTrue(specs["minimum_quantity_verified"])
        self.assertIsNone(specs["sizing_assumption"])

    def test_eth_contract_specs_verify_against_documented_minimum(self):
        specs = DeltaMarketData(config=VPVRConfig()).contract_specs(self._valid_product("ETHUSD"))
        self.assertTrue(specs["spec_verified"], specs["verification_errors"])
        self.assertEqual(specs["contract_multiplier"], 0.01)
        self.assertEqual(specs["contract_unit_currency"], "ETH")
        self.assertEqual(specs["lot_size"], 1)
        self.assertEqual(specs["min_qty"], 1)
        self.assertEqual(specs["minimum_order_contracts"], 1)
        self.assertEqual(specs["tick_size"], 0.05)

    def test_minimum_underlying_quantity_must_convert_exactly_to_contracts(self):
        specs = DeltaMarketData(config=VPVRConfig()).contract_specs(
            self._valid_product(contract_value="0.002")
        )
        self.assertFalse(specs["spec_verified"])
        self.assertIsNone(specs["min_qty"])
        self.assertIn(
            "minimum_underlying_quantity_does_not_convert_to_integer_contracts",
            specs["verification_errors"],
        )

    def test_delta_order_increment_is_integer_contract_count(self):
        specs = DeltaMarketData(config=VPVRConfig()).contract_specs(self._valid_product())
        self.assertEqual(specs["lot_size"], 1)
        self.assertTrue(specs["lot_increment_verified"])
        self.assertIn("integer number of contracts", specs["verification_source"]["increment_basis"])

    def test_live_contract_multiplier_and_tick_are_used(self):
        specs = DeltaMarketData(config=VPVRConfig()).contract_specs(self._valid_product(tick_size="0.25"))
        self.assertEqual(specs["contract_multiplier"], 0.001)
        self.assertEqual(specs["tick_size"], 0.25)

    def test_position_size_limit_is_integer_order_contract_count(self):
        data = DeltaMarketData(config=VPVRConfig())
        specs = data.contract_specs(self._valid_product(position_size_limit="1234"))
        self.assertTrue(specs["spec_verified"])
        self.assertEqual(specs["position_size_limit"], 1234)
        invalid = data.contract_specs(self._valid_product(position_size_limit="1234.5"))
        self.assertFalse(invalid["spec_verified"])
        self.assertIsNone(invalid["position_size_limit"])

    def test_unknown_symbol_remains_unverified(self):
        product = self._valid_product("SOLUSD", contract_value="1", contract_unit_currency="SOL")
        specs = DeltaMarketData(config=VPVRConfig()).contract_specs(product)
        self.assertFalse(specs["spec_verified"])
        self.assertIn("unsupported_symbol: no verified BTCUSD/ETHUSD sizing policy", specs["verification_errors"])

    def test_missing_contract_value_remains_unverified(self):
        product = self._valid_product()
        product.pop("contract_value")
        specs = DeltaMarketData(config=VPVRConfig()).contract_specs(product)
        self.assertFalse(specs["spec_verified"])
        self.assertIn("contract_value_missing_or_invalid", specs["verification_errors"])

    def test_missing_contract_unit_currency_remains_unverified(self):
        product = self._valid_product()
        product.pop("contract_unit_currency")
        specs = DeltaMarketData(config=VPVRConfig()).contract_specs(product)
        self.assertFalse(specs["spec_verified"])
        self.assertIn("contract_unit_currency_does_not_match_symbol_policy", specs["verification_errors"])

    def test_contract_value_mismatch_remains_unverified(self):
        specs = DeltaMarketData(config=VPVRConfig()).contract_specs(
            self._valid_product(contract_value="0.002")
        )
        self.assertFalse(specs["spec_verified"])
        self.assertIn("contract_value_does_not_match_published_symbol_policy", specs["verification_errors"])
        self.assertFalse(specs["minimum_quantity_verified"])

    def test_wrong_underlying_currency_remains_unverified(self):
        specs = DeltaMarketData(config=VPVRConfig()).contract_specs(
            self._valid_product(contract_unit_currency="USD")
        )
        self.assertFalse(specs["spec_verified"])
        self.assertIn("contract_unit_currency_does_not_match_symbol_policy", specs["verification_errors"])

    def test_quanto_and_inverse_products_remain_unverified(self):
        data = DeltaMarketData(config=VPVRConfig())
        quanto = data.contract_specs(self._valid_product(is_quanto=True))
        inverse = data.contract_specs(self._valid_product(notional_type="inverse"))
        self.assertFalse(quanto["spec_verified"])
        self.assertFalse(inverse["spec_verified"])
        self.assertIn("is_quanto_must_be_explicit_false", quanto["verification_errors"])
        self.assertIn("notional_type_must_be_vanilla", inverse["verification_errors"])

    def test_non_operational_or_non_live_products_remain_unverified(self):
        data = DeltaMarketData(config=VPVRConfig())
        suspended = data.contract_specs(self._valid_product(trading_status="suspended"))
        expired = data.contract_specs(self._valid_product(state="expired"))
        self.assertFalse(suspended["spec_verified"])
        self.assertFalse(expired["spec_verified"])
        self.assertIn("trading_status_must_be_operational", suspended["verification_errors"])
        self.assertIn("product_state_must_be_live", expired["verification_errors"])

    def test_incomplete_metadata_never_produces_false_positive(self):
        partial = {"symbol": "BTCUSD", "contract_value": "0.001", "tick_size": "0.5"}
        specs = DeltaMarketData(config=VPVRConfig()).contract_specs(partial)
        self.assertFalse(specs["spec_verified"])
        self.assertTrue(specs["verification_errors"])
        self.assertIsNotNone(specs["metadata_received"])

    def test_freshness_detects_gaps(self):
        data=DeltaMarketData(config=VPVRConfig())
        now=pd.Timestamp("2026-01-01T00:03:00Z").timestamp()
        good=pd.DataFrame({"time":pd.to_datetime(["2026-01-01T00:01:00Z","2026-01-01T00:02:00Z"],utc=True)})
        self.assertTrue(data.freshness(good,"1m",now=now)[0])
        gap=pd.DataFrame({"time":pd.to_datetime(["2026-01-01T00:00:00Z","2026-01-01T00:02:00Z"],utc=True)})
        self.assertFalse(data.freshness(gap,"1m",now=now)[0])


class EngineTests(unittest.TestCase):
    def test_pure_analysis_engine_returns_profile_regime_and_diagnostics(self):
        base=pd.Timestamp("2026-01-01T00:00:00Z")
        def frame(count,minutes):
            times=[base+pd.Timedelta(minutes=minutes*i) for i in range(count)]
            return pd.DataFrame({"time":times,"open":[100.]*count,"high":[100.2]*count,
                "low":[99.8]*count,"close":[100.]*count,"volume":[10.]*count})
        result=VPVREngine(VPVRConfig()).analyse("BTCUSD",frame(80,1),frame(40,3),frame(40,15))
        self.assertEqual(result.symbol,"BTCUSD")
        self.assertEqual(result.profile.source,"ohlcv_uniform_range_approximation")
        self.assertIn("ema20_1m",result.diagnostics)
        self.assertFalse(result.diagnostics["cvd"]["available"])

    def test_analysis_engine_rejects_duplicate_candle_timestamps(self):
        base=pd.Timestamp("2026-01-01T00:00:00Z")
        frame=pd.DataFrame({"time":[base,base],"open":[100.,100.],"high":[101.,101.],
            "low":[99.,99.],"close":[100.,100.],"volume":[10.,10.]})
        with self.assertRaisesRegex(ValueError,"duplicated"):
            VPVREngine._validate_frame(frame,"1m")


class StreamTests(unittest.TestCase):
    def test_compact_ticker_payload_resolves_symbol_and_l1_quotes(self):
        stream=DeltaPublicStream(["BTCUSD"])
        payload={"type":"ticker","d":[{"s":"BTCUSD","m":"100.5",
            "q":["100.6","2.0","100.4","3.0"],"to":[0,12345]}]}
        stream._on_message(None,json.dumps(payload))
        events=stream.snapshot("BTCUSD")
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]["best_ask"],100.6)
        self.assertEqual(events[0]["best_bid"],100.4)
        self.assertEqual(events[0]["mark_price"],100.5)

    def test_stream_uses_singular_candlestick_channel_and_keeps_taker_opaque(self):
        stream=DeltaPublicStream(["BTCUSD"])
        channels=[item["name"] for item in stream.subscription()["payload"]["channels"]]
        self.assertIn("candlestick_1m",channels)
        self.assertNotIn("candlesticks_1m",channels)
        trade={"type":"trades","sy":"BTCUSD","p":"100","s":"2",
               "r":"taker","t":1767225600000000,"ts":1767225600001000}
        stream._on_message(None,json.dumps(trade))
        event=stream.snapshot("BTCUSD")[0]
        self.assertEqual(event["buyer_role"],"taker")
        self.assertIsNone(event["aggressor_side"])


class BacktestTests(unittest.TestCase):
    def test_backtest_quantity_respects_lot_minimum_tick_and_exchange_cap(self):
        qty,stop=_quantity(1000,100,99.16,"LONG",1.0,BacktestConfig(),
            lot_size=.5,min_qty=.5,tick_size=.1,position_size_limit=3.0)
        self.assertEqual(qty,3.0)
        self.assertAlmostEqual(stop,99.1)

    def test_backtest_quantity_rejects_minimum_exceeding_risk_budget(self):
        qty,_=_quantity(1000,100,99.9,"LONG",1.0,BacktestConfig(),
            lot_size=1,min_qty=30,tick_size=.01)
        self.assertEqual(qty,0.0)

    def test_completed_history_excludes_current_forming_1m_bar(self):
        frame=pd.DataFrame({"time":pd.date_range("2026-01-01",periods=4,freq="1min",tz="UTC")})
        history=_completed_1m_history(frame,3)
        self.assertEqual(len(history),3)
        self.assertLess(history.time.max(),frame.time.iloc[3])

    def test_backtest_rejects_gapped_timeframes(self):
        base=pd.Timestamp("2026-01-01T00:00:00Z")
        def frame(count,minutes):
            times=[base+pd.Timedelta(minutes=minutes*i) for i in range(count)]
            return pd.DataFrame({"time":times,"open":[100.]*count,"high":[100.2]*count,
                "low":[99.8]*count,"close":[100.]*count,"volume":[10.]*count})
        broken=frame(600,1).drop(index=[200]).reset_index(drop=True)
        with self.assertRaisesRegex(ValueError,"gaps"):
            run_backtest("BTCUSD",broken,frame(200,3),frame(40,15),BacktestConfig())

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
