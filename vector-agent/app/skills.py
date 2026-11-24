"""Shared with Cortana: this is agent_core.skills (see ../agent-core)."""
import sys

from . import config  # noqa: F401  (registers Vector's settings with the core first)
from agent_core import skills as _core


def _author() -> str:
    from . import faces                     # who taught him a skill: the face in front of him
    return faces.front_name() or "someone"


_core.author = _author

sys.modules[__name__] = _core
