"""Sleep job `reconcile_beliefs`: while he sleeps, his notes and the day's talk become
long-term beliefs (agent_core.reconcile, shared with Cortana). Runs instead of the old
`harvest_memories` when the belief store is on."""
from . import beliefs, memory, sleep_routine  # noqa: F401  (memory registers his speaker labels)
from agent_core import reconcile
from agent_core.reconcile import JOB, line_time  # noqa: F401


def has_work() -> bool:
    return reconcile.has_work(sleep_routine._state.get("jobs", {}).get(JOB, {}).get("state", {}) or {})


async def reconcile_beliefs(ctx) -> dict:
    """Before the day's conversations are folded away (compact_conversations, order 80)."""
    return await reconcile.run(ctx)


if beliefs.ENABLED:        # otherwise compaction.harvest_memories keeps this slot (notes-only memory)
    sleep_routine.job(JOB, order=70, budget_s=300, has_work=has_work)(reconcile_beliefs)
