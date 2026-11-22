"""Clean what Vector says before wire-pod speaks it.

Small models sometimes write things that make no sense out loud: emoji, *stage
directions*, or made-up {{cues}}. This filter works on the token stream, holding back
an unfinished {{...}} or *...* until it can decide what to do with it.
Only wire-pod's real cues survive.
"""
import re

_ALLOWED_CUE = re.compile(r"\{\{(playAnimationWI|playAnimation|newVoiceRequest)\|\|[A-Za-z]+\}\}")
_ANY_CUE = re.compile(r"\{\{[^{}]*\}\}")
_DANGLING_CUE = re.compile(r"\{\{[^}]*$")
_BOLD = re.compile(r"\*\*([^*\n]{1,80})\*\*")    # **Heading** is markdown, keep the words
_STAGE = re.compile(r"(?<!\*)\*(?!\*)[^*\n]{1,80}(?<!\*)\*(?!\*)")   # *bounces around*
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF‍️]")
_SPACES = re.compile(r"[ \t]{2,}")
_EYES = re.compile(r"\(eyes:[^()]*\)")   # the speaker note server.py stamps on messages


def clean(text: str) -> str:
    text = _ANY_CUE.sub(lambda m: m.group(0) if _ALLOWED_CUE.fullmatch(m.group(0)) else "", text)
    text = _EYES.sub("", text)
    text = _BOLD.sub(r"\1", text)
    text = _STAGE.sub("", text).replace("*", "")
    text = _EMOJI.sub("", text)
    return _SPACES.sub(" ", text)


class Sanitizer:
    def __init__(self) -> None:
        self.buf = ""

    def feed(self, chunk: str) -> str:
        self.buf += chunk
        cut = len(self.buf)
        o = self.buf.rfind("{{")
        if o != -1 and self.buf.find("}}", o) == -1:
            cut = o
        p = self.buf.rfind("(", 0, cut)
        if p != -1 and ")" not in self.buf[p:cut]:
            tail = self.buf[p:cut]
            if tail.startswith("(eyes:") or "(eyes:".startswith(tail):
                cut = p   # might be an echoed speaker note: wait for the rest
        if self.buf[:cut].count("*") % 2 == 1:
            cut = self.buf[:cut].rfind("*")
        out, self.buf = self.buf[:cut], self.buf[cut:]
        return clean(out)

    def flush(self) -> str:
        out, self.buf = self.buf, ""
        out = re.sub(r"\(eyes:[^)]*$", "", out)
        return clean(_DANGLING_CUE.sub("", out))
