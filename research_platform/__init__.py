"""Shared, research-only contracts for market data and strategy integration."""

from .market_data import (
    BookLevel,
    Candle,
    ContractSpecifications,
    CoinResearchAdapter,
    DataQuality,
    DeltaResearchAdapter,
    FundingObservation,
    Instrument,
    MarketDataProvider,
    OrderBookSnapshot,
    ProviderCapabilities,
    TickerSnapshot,
    TradePrint,
    UnsupportedCapabilityError,
    VPVRDataAdapter,
)
from .store import ResearchDataStore
from .collector import ResearchCollector
from .strategies import (
    FastSlowMAAdapter,
    MHRExhaustionAdapter,
    ScalpPullbackAdapter,
    StrategyAdapter,
    StrategyContext,
    StrategySignal,
    StrategySpecification,
    VPVRStrategyAdapter,
)

__all__ = [
    "BookLevel",
    "Candle",
    "ContractSpecifications",
    "CoinResearchAdapter",
    "DataQuality",
    "DeltaResearchAdapter",
    "FundingObservation",
    "FastSlowMAAdapter",
    "Instrument",
    "MHRExhaustionAdapter",
    "MarketDataProvider",
    "OrderBookSnapshot",
    "ProviderCapabilities",
    "ResearchCollector",
    "ResearchDataStore",
    "ScalpPullbackAdapter",
    "StrategyAdapter",
    "StrategyContext",
    "StrategySignal",
    "StrategySpecification",
    "TickerSnapshot",
    "TradePrint",
    "UnsupportedCapabilityError",
    "VPVRDataAdapter",
    "VPVRStrategyAdapter",
]
