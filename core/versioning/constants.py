"""Analysis pipeline versioning constants.

APP_VERSION is the application release version (from VERSION file / env).
The other constants version individual analytical modules so report payloads
carry a full provenance chain.
"""
from core.versioning.version import APP_VERSION  # re-exported for convenience

ANALYSIS_VERSION = "0.2.0"
CAUSAL_MODEL_VERSION = "0.1.0"
RULE_PACK_VERSION = "m350-rules-0.1.0"
SIGNAL_DICTIONARY_VERSION = "m350-signals-0.3.0"
LLM_PROMPT_VERSION = "session-report-0.1.0"

__all__ = [
    "APP_VERSION",
    "ANALYSIS_VERSION",
    "CAUSAL_MODEL_VERSION",
    "RULE_PACK_VERSION",
    "SIGNAL_DICTIONARY_VERSION",
    "LLM_PROMPT_VERSION",
]
