"""StockApp V12 swing-trading package (code lives under v12/src/)."""

__version__ = "12.0.0"

from stockapp_v12.contracts import (  # stable Protocol seams
    BrokerAdapter,
    ChronosBackend,
    Clock,
    EntryEvalStore,
    JournalStore,
    MarketDataClient,
    Notifier,
    PositionStore,
    WatchlistStore,
)
from stockapp_v12.core import (
    V12RiskLimits,
    calculate_risk_based_size_pct,
    load_v12_risk_limits,
    v12_enabled,
)
from stockapp_v12.core.execution import BracketOrderService, BracketResult, PaperBroker
from stockapp_v12.pipeline import (
    AnalystOutput,
    EntryEvaluationGuard,
    TradeJournal,
    parse_analyst_output,
)
from stockapp_v12.services import (
    DailyReview,
    DEFAULT_FALLBACK_SPREAD_PCT,
    ENSEMBLE_ENV_VAR,
    EntryOrchestrator,
    EntryContext,
    EntryResult,
    EnsembleChronosBackend,
    FALLBACK_SPREAD_ENV_VAR,
    ForecastService,
    PositionManager,
    PositionRecord,
    PositionState,
    ReviewDecision,
    SignalRuleEngine,
    YoloFilterDecision,
    YoloNoTradeFilter,
    make_ensemble_backend_from_env,
)

__all__ = [
    "__version__",
    "BrokerAdapter",
    "ChronosBackend",
    "Clock",
    "EntryEvalStore",
    "JournalStore",
    "MarketDataClient",
    "Notifier",
    "PositionStore",
    "WatchlistStore",
    "V12RiskLimits",
    "load_v12_risk_limits",
    "v12_enabled",
    "calculate_risk_based_size_pct",
    "AnalystOutput",
    "parse_analyst_output",
    "EntryEvaluationGuard",
    "TradeJournal",
    "PositionManager",
    "PositionRecord",
    "PositionState",
    "EntryOrchestrator",
    "EntryContext",
    "EntryResult",
    "ForecastService",
    "SignalRuleEngine",
    "DailyReview",
    "ReviewDecision",
    "BracketOrderService",
    "BracketResult",
    "PaperBroker",
    # Wave 9: YOLO no-trade filter
    "YoloNoTradeFilter",
    "YoloFilterDecision",
    # Round 3 closure (2026-07-01): A+B ensemble backend
    "EnsembleChronosBackend",
    "make_ensemble_backend_from_env",
    "ENSEMBLE_ENV_VAR",
    "FALLBACK_SPREAD_ENV_VAR",
    "DEFAULT_FALLBACK_SPREAD_PCT",
]
