"""Advanced process diagnostics kept separate from operator-facing rules.

The algorithms in this package are deliberately marked as ``shadow`` until
their thresholds are validated against labelled production runs.  They may
explain suspicious behaviour, but must not stop a build or create an alarm.
"""

from analytics.process_monitoring.advanced import build_advanced_monitoring

__all__ = ["build_advanced_monitoring"]
