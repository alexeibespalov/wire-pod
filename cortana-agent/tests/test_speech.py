from app.speech import SILENT, Sentences, clean


def feed_all(chunks):
    s = Sentences()
    out = []
    for c in chunks:
        out += s.feed(c)
    rest = s.flush()
    return out + ([rest] if rest else [])


def test_streamed_sentences_come_out_as_soon_as_complete():
    s = Sentences()
    assert s.feed("It's about four degrees outside") == []
    assert s.feed(" right now. Bring a ") == ["It's about four degrees outside right now."]
    assert s.feed("coat! ") == ["Bring a coat!"]
    assert s.flush() == ""


def test_short_first_sentence_joins_the_next():
    assert feed_all(["Okay. ", "Ten minutes, starting now. "]) == ["Okay. Ten minutes, starting now."]


def test_abbreviations_dont_split():
    assert feed_all(["Dr. Patel is at four. ", "Don't be late."]) == ["Dr. Patel is at four.", "Don't be late."]


def test_markdown_cues_emoji_urls_are_removed():
    text = "## Weather\n- **Sunny** today {{playAnimationWI||happy}} *smiles* 😀 see https://bbc.co.uk"
    assert clean(text).split() == ["Weather", "Sunny", "today", "see"]


def test_unfinished_cue_is_held_back():
    s = Sentences()
    assert s.feed("Sure thing {{playAni") == []
    assert s.feed("mationWI||happy}} done. Next one ") == ["Sure thing done."]


def test_heard_note_never_spoken():
    assert clean("(heard Sat 10:39 AM) Hello there").strip() == "Hello there"


def test_silent_word():
    assert SILENT.match(" SILENT. ")
    assert not SILENT.match("Silently we go")
