"""Phase 2.5 semantic planning contracts.

This package is intentionally isolated from the production orchestrator.  It
is consumed only by tests, audit tooling, and the offline shadow runner.
"""

from .models import PlanEnvelope

__all__ = ["PlanEnvelope"]
