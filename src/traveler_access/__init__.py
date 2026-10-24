"""Governed, AI-safe access to traveler data.

Read `specs/` first: every module here implements a numbered spec, and the specs
explain *why* the obvious implementation would be wrong.
"""

from .events import PIPELINE_VERSION

__all__ = ["PIPELINE_VERSION"]
