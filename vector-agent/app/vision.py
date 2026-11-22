"""Camera + multimodal model: Vector looks, the vision model describes what matters."""
import asyncio
import base64
import time
from typing import Annotated, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from pydantic import Field

from . import config, robot

_vision = ChatOpenAI(
    model=config.VISION_MODEL,
    base_url=config.LLM_BASE_URL,
    api_key=config.LLM_API_KEY,
    temperature=0.2,
    extra_body=config.LLM_EXTRA_BODY,
    timeout=45,
    max_retries=1,
    max_tokens=220,
)

_EYES = (
    "You are the eyes of Vector, a palm-sized desk robot. The image comes from his "
    "low, wide camera on a desk, so people often appear from below and close objects "
    "may be held up to him. Answer the question about the image factually and concisely "
    "(two or three sentences): name the main things, read any clear text, mention people "
    "(appearance, what they hold or do) but never guess anyone's identity, age or "
    "sensitive traits. If the image is dark, blurry or doesn't show what was asked, "
    "say exactly that."
)


_HEAD = {"up": 35.0, "level": 0.0, "down": -20.0}


async def _frame_with_head(head: str) -> bytes:
    """Aim the head and take the picture while still holding control: released in
    between, his face tracking swings the head straight back to whoever is in front."""
    from . import body
    async with body.ENGINE.batch(f"look {head}") as b:
        await b.head(_HEAD[head])
        await asyncio.sleep(0.5)               # let the image settle
        return await robot.camera_frame()


@tool
async def look(question: Annotated[str, Field(description=
        "What you want to know about the view, in the user's terms, e.g. 'what is the user holding up?'")],
        head: Annotated[Literal["as is", "up", "level", "down"], Field(description=
            "Where to point your head for this picture: up = faces / things held up, level = "
            "straight ahead, down = things on the desk or floor right in front of you")] = "as is") -> str:
    """Take a picture with Vector's camera and get a description focused on `question`.
    Load the 'camera' skill first. Use `head` to aim (it stays aimed until the picture is
    taken); don't call move_head first."""
    t0 = time.time()
    try:
        if head != "as is":
            try:
                jpg = await _frame_with_head(head)
            except Exception as e:             # body busy / on charger quirks: still look
                if "camera" in str(e).lower():
                    raise
                jpg = await robot.camera_frame()
        else:
            jpg = await robot.camera_frame()
    except Exception as e:
        return (f"error: my camera gave no picture ({type(e).__name__}). In low-power mode "
                f"(asleep / resting on the charger) the camera is off: wake up first.")
    try:  # keep the last view for show_last_view / debugging
        (config.FACE_DIR / "last_view.jpg").write_bytes(jpg)
    except OSError:
        pass
    msg = HumanMessage(content=[
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpg).decode()}},
        {"type": "text", "text": f"Question: {question}"},
    ])
    resp = await _vision.ainvoke([SystemMessage(_EYES), msg])
    return f"(camera, {time.time()-t0:.1f}s) {resp.content}"
