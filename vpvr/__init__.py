"""VPVR research and paper trading subsystem (no live order execution)."""
from .config import VPVRConfig
from .profile import VolumeProfile, build_volume_profile, build_fixed_range_profile, developing_profile, rolling_profiles, session_profiles
from .regime import MarketRegime, RegimeResult, classify_regime
from .strategies import StrategySignal, generate_signals
from .risk import RiskEngine, RiskDecision
from .engine import VPVREngine, VPVRAnalysis
from .paper import PaperBroker

__all__ = [
    "VPVRConfig", "VolumeProfile", "build_volume_profile", "build_fixed_range_profile",
    "developing_profile", "rolling_profiles", "session_profiles", "MarketRegime",
    "RegimeResult", "classify_regime", "StrategySignal", "generate_signals",
    "RiskEngine", "RiskDecision", "VPVREngine", "VPVRAnalysis", "PaperBroker"
]
