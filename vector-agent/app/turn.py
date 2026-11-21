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

wirepod_has_control = False


def reset() -> None:
    global wirepod_has_control
    wirepod_has_control = False


def mark_wirepod_control() -> None:
    global wirepod_has_control
    wirepod_has_control = True
