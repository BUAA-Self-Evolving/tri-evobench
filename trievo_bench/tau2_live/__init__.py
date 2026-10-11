"""Live tau2-bench sessions for L2 Turns (host side).

``session/`` runs inside the τ² task image; this module only drives Docker.
"""

from .launcher import Tau2LiveSession, Tau2LiveSessionError, is_live_profile

__all__ = ["Tau2LiveSession", "Tau2LiveSessionError", "is_live_profile"]
