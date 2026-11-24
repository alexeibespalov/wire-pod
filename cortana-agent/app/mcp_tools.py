"""Shared with Vector: this is agent_core.mcp_tools (see ../agent-core)."""
import sys

from . import config  # noqa: F401  (registers Cortana's settings with the core first)
from agent_core import mcp_tools as _core

sys.modules[__name__] = _core
