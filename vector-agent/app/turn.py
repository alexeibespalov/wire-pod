"""Per-turn coordination between the agent's robot actions and wire-pod.

Robot actions (animations, face screen, motors) need Vector's behaviour control.
wire-pod also takes it:
  * normal requests ("Hey Vector, ..."): at the first words of the reply
  * follow-ups / "I have a question" (knowledge-graph path): at the START of the request

If wire-pod holds control and we ask for it too, we steal it and wire-pod stops
talking. But while wire-pod holds control, commands from wire-pod's own connection
(which our Lua/SDK calls go through) are accepted WITHOUT asking (verified on the robot),
so in that state we simply don't ask.

The server also never lets text from a step that uses robot tools be spoken before
the action, so pictures/animations happen first and he talks afterwards (an image shown
over his talking face comes out black).

One robot, one conversation turn at a time (server holds a lock), so module state is fine.
"""

# Tools that need Vector's behaviour control (motors, animations, face screen).
CONTROL_TOOLS = {"drive", "turn", "move_head", "move_lift", "do_action", "play_animation",
                 "show_picture", "show_text", "show_last_view"}

import asyncio
import time

wirepod_has_control = False
followup = False          # knowledge-graph path: wire-pod loops a "searching" animation
lead_in_at: float = 0.0   # when we sent the short spoken lead-in (stops that loop)
LEAD_IN_SECONDS = 2.0     # time to let the lead-in be spoken before using the face


def reset(is_followup: bool = False) -> None:
    global wirepod_has_control, followup, lead_in_at
    wirepod_has_control = is_followup
    followup = is_followup
    lead_in_at = 0.0


def mark_wirepod_control() -> None:
    global wirepod_has_control
    wirepod_has_control = True


def mark_lead_in() -> None:
    global lead_in_at
    lead_in_at = time.time()


async def wait_for_lead_in() -> None:
    """In follow-ups, don't paint the face until the lead-in has been spoken: wire-pod's
    searching animation only stops once our first words arrive, and an image shown
    over his talking face comes out black."""
    if not followup:
        return
    # the tool can start a moment before the server has sent the lead-in
    for _ in range(20):
        if lead_in_at:
            break
        await asyncio.sleep(0.05)
    if lead_in_at:
        remaining = lead_in_at + LEAD_IN_SECONDS - time.time()
        if remaining > 0:
            await asyncio.sleep(remaining)
