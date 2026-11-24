"""Shared with Cortana: this is agent_core.sleepbatch (see ../agent-core)."""
import sys

from . import config  # noqa: F401  (registers Vector's settings with the core first)
from agent_core import sleepbatch as _core

sys.modules[__name__] = _core
