"""Market-wide Delta perpetual discovery and shadow research."""

from .features import evaluate_symbol
from .store import ResearchStore

__all__ = ["ResearchStore", "evaluate_symbol"]
