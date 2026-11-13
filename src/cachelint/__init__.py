"""Offline diagnostics for LLM prompt caching."""

from cachelint._compare import (
    ComparisonResult,
    ContextDifference,
    Divergence,
    Finding,
    compare_requests,
)
from cachelint._corpus import (
    CorpusGroup,
    CorpusLimits,
    CorpusPattern,
    CorpusResult,
    analyze_corpus,
)
from cachelint._estimate import PrefixEstimate
from cachelint._evidence import (
    CorpusUsageGroup,
    CorpusUsageTrace,
    ObservedCorpusResult,
    ObservedFinding,
    analyze_observed_corpus,
)
from cachelint._ingest import (
    CanonicalRequest,
    IncomparableRequestsError,
    InvalidRequestError,
    Provider,
    Segment,
    SourceLocation,
    canonicalize_request,
)
from cachelint._observed import (
    UsageGroup,
    UsageLimits,
    UsageObservation,
    UsageResult,
    UsageTrace,
    analyze_usage,
)
from cachelint._version import __version__
from cachelint.baseline import baseline_from_report, load_baseline

__all__ = [
    "__version__",
    "CanonicalRequest",
    "ComparisonResult",
    "CorpusGroup",
    "CorpusLimits",
    "CorpusPattern",
    "CorpusResult",
    "CorpusUsageGroup",
    "CorpusUsageTrace",
    "ContextDifference",
    "Divergence",
    "Finding",
    "IncomparableRequestsError",
    "InvalidRequestError",
    "ObservedCorpusResult",
    "ObservedFinding",
    "Provider",
    "PrefixEstimate",
    "Segment",
    "SourceLocation",
    "UsageGroup",
    "UsageLimits",
    "UsageObservation",
    "UsageResult",
    "UsageTrace",
    "analyze_usage",
    "analyze_corpus",
    "analyze_observed_corpus",
    "canonicalize_request",
    "compare_requests",
    "baseline_from_report",
    "load_baseline",
]
