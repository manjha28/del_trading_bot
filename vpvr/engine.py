"""Pure VPVR analysis orchestrator. No I/O, exchange credentials or order side effects."""
from __future__ import annotations
from dataclasses import dataclass
import pandas as pd
from .config import VPVRConfig
from .profile import VolumeProfile, build_volume_profile, rolling_profiles
from .regime import RegimeResult, classify_regime
from .strategies import StrategySignal, generate_signals
from .indicators import session_vwap, ema, volume_expansion, cumulative_volume_delta, trade_delta_bars, delta_divergence


@dataclass(frozen=True)
class VPVRAnalysis:
    symbol: str
    profile: VolumeProfile
    regime: RegimeResult
    signals: tuple[StrategySignal, ...]
    diagnostics: dict


class VPVREngine:
    def __init__(self, config: VPVRConfig | None = None):
        self.config = (config or VPVRConfig.from_env()).validate()

    @staticmethod
    def _validate_frame(frame, timeframe):
        required={"time","open","high","low","close","volume"}
        if frame is None or frame.empty or not required.issubset(frame.columns):
            raise ValueError(f"{timeframe} candles are missing or incomplete")
        times=pd.to_datetime(frame.time,utc=True)
        if not times.is_monotonic_increasing or times.duplicated().any():
            raise ValueError(f"{timeframe} candle times are unordered or duplicated")
        return frame

    def analyse(self, symbol, candles_1m, candles_3m, candles_15m, trades=None) -> VPVRAnalysis:
        c1=self._validate_frame(candles_1m,"1m")
        c3=self._validate_frame(candles_3m,"3m")
        c15=self._validate_frame(candles_15m,"15m")
        profile_frame=c15.tail(self.config.profile_lookback_15m)
        start=pd.Timestamp(profile_frame.time.iloc[0])
        end=pd.Timestamp(profile_frame.time.iloc[-1])+pd.Timedelta(minutes=15)
        tape_covers=(trades is not None and not trades.empty and
                     pd.to_datetime(trades.time,utc=True).min()<=start and
                     pd.to_datetime(trades.time,utc=True).max()>=end-pd.Timedelta(minutes=1))
        if tape_covers:
            selected=trades.loc[(pd.to_datetime(trades.time,utc=True)>=start)&
                                (pd.to_datetime(trades.time,utc=True)<end)]
            profile=build_volume_profile(trades=selected,rows=self.config.profile_rows,
                value_area_pct=self.config.value_area_pct,
                smoothing_window=self.config.node_smoothing_window,
                node_threshold_pct=self.config.node_threshold_pct,start=start,end=end)
        else:
            profile=build_volume_profile(profile_frame,rows=self.config.profile_rows,
                value_area_pct=self.config.value_area_pct,
                smoothing_window=self.config.node_smoothing_window,
                node_threshold_pct=self.config.node_threshold_pct,start=start,end=end)
        history=rolling_profiles(c15.tail(96),rows=self.config.profile_rows,
            value_area_pct=self.config.value_area_pct,window=32,step=16,
            smoothing_window=self.config.node_smoothing_window,
            node_threshold_pct=self.config.node_threshold_pct)
        regime=classify_regime(c15,c3,profile,history)
        signals=generate_signals(symbol,c1,c3,c15,profile,regime,
            self.config.min_reward_risk,self.config.min_volume_multiple)
        cvd=cumulative_volume_delta(trades)
        delta_bars=trade_delta_bars(trades,"1min")
        divergence=delta_divergence(c1.tail(10),delta_bars.tail(10),lookback=10)
        diagnostics={
            "profile_source":profile.source,
            "trade_tape_covers_profile_window":bool(tape_covers),
            "session_vwap":float(session_vwap(c1).iloc[-1]),
            "ema20_1m":float(ema(c1.close,self.config.ema_length).iloc[-1]),
            "ema20_3m":float(ema(c3.close,self.config.ema_length).iloc[-1]),
            "ema20_15m":float(ema(c15.close,self.config.ema_length).iloc[-1]),
            "volume_expansion_1m":volume_expansion(c1),
            "volume_expansion_3m":volume_expansion(c3),
            "cvd":cvd,
            "delta_divergence":divergence,
            "rolling_profiles":len(history),
        }
        return VPVRAnalysis(symbol,profile,regime,tuple(signals),diagnostics)
