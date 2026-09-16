"""Phase 1 workers for the short-term surge research platform.

Scope of this package in Phase 1 is deliberately narrow: security master and
universe construction only. No price screening, news collection or analysis.
"""

__version__ = "0.2.0"

# Bumped whenever the job's output can change: parsers, classification,
# identity rules or the snapshot layout.
JOB_VERSION = "universe_sync-1.1b.0"
