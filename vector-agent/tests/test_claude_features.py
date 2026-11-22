"""Claude-era features: append-only history, sleep batches, body-program plumbing."""
import asyncio
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app import agent, config, programs, sleepbatch


def _hist():
    return [HumanMessage("hi", id="h1"), SystemMessage("[RIGHT NOW] t1", id="s1"),
            AIMessage(content=[{"type": "thinking", "thinking": "", "signature": "x"},
                               {"type": "text", "text": "Hello!"}], id="a1"),
            HumanMessage("(eyes: Leo) look", id="h2"), SystemMessage("[RIGHT NOW] t2", id="s2")]


def test_claude_history_is_never_edited():
    sys_msg = SystemMessage("persona")
    hist = _hist()
    out = agent.for_model(sys_msg, hist)                  # Claude: no context argument
    # same messages, same order, same text: only cache markers may be added
    assert [type(m) for m in out[1:]] == [type(m) for m in hist]
    texts = [m.content if isinstance(m.content, str) else m.content[0].get("text") for m in out[1:]]
    assert texts[0] == "hi" and texts[3] == "(eyes: Leo) look" and texts[4] == "[RIGHT NOW] t2"
    assert out[-1].content[0]["cache_control"]            # breakpoint on this turn's context


def test_qwen_drops_saved_system_messages_and_prepends_context():
    out = agent.for_model(SystemMessage("persona"), _hist(), context="[RIGHT NOW] live")
    assert not any(isinstance(m, SystemMessage) for m in out[1:])
    newest = [m for m in out if isinstance(m, HumanMessage)][-1]
    body = newest.content if isinstance(newest.content, str) else newest.content[0]["text"]
    assert body.startswith("[RIGHT NOW] live\n(eyes: Leo) look")


def test_batch_params_convert_images_and_system():
    msgs = [SystemMessage("label it"), HumanMessage(content=[
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,QUJD"}},
        {"type": "text", "text": "Label this photo as JSON."}])]
    p = sleepbatch._params(msgs, 400)
    assert p["system"] == "label it" and p["model"] == config.SLEEP_MODEL and p["max_tokens"] >= 4096
    img, txt = p["messages"][0]["content"]
    assert img == {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "QUJD"}}
    assert txt["text"] == "Label this photo as JSON."
    k = sleepbatch._key(p)
    assert k == sleepbatch._key(dict(p)) and len(k) <= 64 and k.isalnum()


def test_batch_model_queues_then_answers(tmp_path, monkeypatch):
    monkeypatch.setattr(sleepbatch, "_FILE", tmp_path / "b.json")
    monkeypatch.setattr(sleepbatch, "_arm_flush", lambda: None)
    sleepbatch._queue.clear()
    m = sleepbatch._BatchModel(500)
    msgs = [HumanMessage("summarise this")]

    async def go():
        with pytest.raises(sleepbatch.Pending):
            await m.ainvoke(msgs)
        key = next(iter(sleepbatch._queue))
        with pytest.raises(sleepbatch.Pending):               # still waiting: not queued twice
            await m.ainvoke(msgs)
        assert len(sleepbatch._queue) == 1
        sleepbatch._queue.clear()
        d = sleepbatch._load()
        d["results"][key] = {"text": '{"ok": true}', "t": 0}
        sleepbatch._save(d)
        res = await m.ainvoke(msgs)
        assert res.content == '{"ok": true}'
        assert key not in sleepbatch._load()["results"]       # used once
    asyncio.run(go())


def test_program_steps_and_results():
    assert programs._step("drive", {"mm": 40, "stop_below_mm": 30}) == {
        "step": "drive", "mm": 40, "speed": 60, "stop_when": {"prox_below_mm": 30}}
    assert programs._step("turn", {"deg": -15}) == {"step": "turn", "deg": -15}
    r = json.loads(programs._jsonable({"disp_mm": 38.6, "heading_deg": 0.4, "prox_before": 316,
                                       "prox_after": 120, "duration_s": 1.52, "stop_reason": "",
                                       "notes": []}))
    assert r == {"moved_mm": 39, "turned_deg": 0, "prox_start_mm": 316, "prox_end_mm": 120, "duration_s": 1.5}
    assert all(t["allowed_callers"] == [programs.CODE_TOOL] for t in programs._BODY_TOOLS)
