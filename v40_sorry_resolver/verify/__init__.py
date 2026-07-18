"""Verification layer public API (SPEC 3.6-3.8).

``build_verifier(cfg)`` is the factory (re-exported from ``base``). The default
path is ``subprocess`` (real ``lake env lean``); ``dojo`` is an optional
flag-gated path; ``mock`` is for tests only and must be explicitly requested.
"""
from .base import VerificationResult, Verifier, build_verifier
from .dojo import DojoUnavailableError, LeanDojoVerifier
from .mock import MockVerifier
from .subprocess_lean import SubprocessLeanVerifier, VerificationError

__all__ = [
    "VerificationResult",
    "Verifier",
    "build_verifier",
    "SubprocessLeanVerifier",
    "VerificationError",
    "LeanDojoVerifier",
    "DojoUnavailableError",
    "MockVerifier",
]
