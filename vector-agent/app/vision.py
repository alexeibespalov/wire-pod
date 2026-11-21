"""Camera + multimodal model: Vector looks, the vision model describes what matters."""
import base64
import time
from typing import Annotated

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


@tool
async def look(question: Annotated[str, Field(description=
        "What you want to know about the view, in the user's terms, e.g. 'what is the user holding up?'")]) -> str:
    """Take a picture with Vector's camera and get a description focused on `question`.
    Load the 'camera' skill first. Point your head the right way BEFORE calling this
    (move_head up for faces and held-up objects, level for things on the desk)."""
    t0 = time.time()
    jpg = await robot.camera_frame()
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
