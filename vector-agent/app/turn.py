"""Per-turn coordination between the agent's robot actions and wire-pod's speech.

wire-pod grabs Vector's behaviour control (OVERRIDE priority) as soon as the first
words of a reply arrive, and holds it while it speaks. A robot action that asks for
control during that time *steals* it and wire-pod stops talking mid-reply. So:

* before any words have been sent: robot actions run immediately (action first,
  then speech)
* once words have been sent: robot actions are queued and run after Vector has
  finished speaking

One robot, one conversation turn at a time (server holds a lock), so module state is fine.
"""
import asyncio
import logging

log = logging.getLogger("vector-agent")

# Tools that need Vector's behaviour control (motors, animations, face screen).
CONTROL_TOOLS = {"drive", "turn", "move_head", "move_lift", "do_action", "play_animation",
                 "show_picture", "show_text", "show_last_view"}

speaking = False
_deferred: list[tuple] = []
_background: set = set()  # strong refs: un-referenced asyncio tasks can be garbage collected


def reset() -> None:
    global speaking
    speaking = False
    _deferred.clear()


def mark_speaking() -> None:
    global speaking
    speaking = True


def defer(tool, args: dict) -> None:
    _deferred.append((tool, args))


def run_deferred_after(seconds: float) -> None:
    """Run queued robot actions once wire-pod has (probably) finished speaking."""
    if not _deferred:
        return
    jobs = list(_deferred)
    _deferred.clear()

    async def runner():
        await asyncio.sleep(seconds)
        for tool, args in jobs:
            try:
                log.info("deferred action %s(%s): %s", tool.name, args, await tool.ainvoke(args))
            except Exception as e:
                log.warning("deferred action %s failed: %s", tool.name, e)

    log.info("queued %d robot action(s) to run in %.1fs (after speech)", len(jobs), seconds)
    task = asyncio.get_running_loop().create_task(runner())
    _background.add(task)
    task.add_done_callback(_background.discard)
