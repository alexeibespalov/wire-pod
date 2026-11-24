import json

from agent_core import notes
from app import memory


def test_notes_add_forget_plan(tmp_path, monkeypatch):
    monkeypatch.setattr(notes, "_PATH", tmp_path / "m.json")
    assert memory.add("Leo's swimming is on Tuesdays") == "remembered"
    assert memory.add("leo's swimming is on tuesdays!") == "already remembered"
    changed = memory.apply_plan({"add": ["Bin day is Wednesday"],
                                 "update": [{"n": 1, "fact": "Leo's swimming is on Thursdays"}]})
    assert changed == ["updated: Leo's swimming is on Tuesdays -> Leo's swimming is on Thursdays",
                       "saved: Bin day is Wednesday"]
    rows = json.loads((tmp_path / "m.json").read_text())
    assert all(r["by"] == "cortana" for r in rows)
    assert memory.forget("bin day") == "forgot 1 fact(s)"
    assert memory.all_facts() == ["Leo's swimming is on Thursdays"]


def test_speaker_line():
    assert memory.speaker_line("(heard Sun 4:15 PM; first words in 3 hours)\nmy birthday is in May") == \
        "SOMEONE (Sun 4:15 PM): my birthday is in May"
    assert notes.speaker_line is memory.speaker_line        # the reconcile job reads transcripts her way
