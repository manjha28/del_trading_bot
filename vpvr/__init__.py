"""VPVR scalping engine. Paper-first, no live order execution in this package."""
from .config import VPVRConfig
from .profile import VolumeProfile, build_volume_profile
from .regime import MarketRegime, classify_regime
from .strategies import StrategySignal, generate_signals
from .risk import RiskEngine, RiskDecision

__all__ = ["VPVRConfig", "VolumeProfile", "build_volume_profile", "MarketRegime", "classify_regime", "StrategySignal", "generate_signals", "RiskEngine", "RiskDecision"]
