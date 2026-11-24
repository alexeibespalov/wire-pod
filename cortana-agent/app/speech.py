"""Turn the model's streamed text into clean spoken sentences for Piper.

Everything she writes is read aloud, so markdown, emoji, *stage directions*, {{cues}}
(a habit from Vector's prompts) and URLs are removed. The stream is cut into sentences
as soon as each one is complete, so the first sentence is spoken while the rest is
still being written.
"""
import re

_CUE = re.compile(r"\{\{[^{}]*\}\}")
_DANGLING_CUE = re.compile(r"\{\{[^}]*$")
_BOLD = re.compile(r"\*\*([^*\n]{1,120})\*\*")
_STAGE = re.compile(r"(?<!\*)\*(?!\*)[^*\n]{1,80}(?<!\*)\*(?!\*)")   # *smiles*
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF‍️]")
_URL = re.compile(r"https?://\S+|www\.\S+")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+", re.M)
_NOTE = re.compile(r"\(heard [^()]*\)")       # the time note the server puts on messages
_SPACES = re.compile(r"[ \t]{2,}")

# a sentence ends at . ! ? (plus closing quotes/brackets) followed by whitespace
_END = re.compile(r"[.!?…]+[\"')\]]*\s+")
_ABBREV = re.compile(r"\b(?:Mr|Mrs|Ms|Dr|St|Mt|vs|etc|e\.g|i\.e|approx|No)\.$", re.I)
MIN_CHARS = 12          # don't send "Okay." on its own; join it to the next sentence
SILENT = re.compile(r"^\W*SILENT\W*$", re.I)


def clean(text: str) -> str:
    text = _CUE.sub("", text)
    text = _NOTE.sub("", text)
    text = _URL.sub("", text)
    text = _HEADING.sub("", text)
    text = _BULLET.sub("", text)
    text = _BOLD.sub(r"\1", text)
    text = _STAGE.sub("", text).replace("*", "")
    text = _EMOJI.sub("", text)
    text = text.replace("\n", " ")
    return _SPACES.sub(" ", text)


class Sentences:
    """feed(chunk) -> complete, cleaned sentences; flush() -> whatever is left."""

    def __init__(self) -> None:
        self.buf = ""

    def _safe_cut(self) -> int:
        """Don't cut inside an unfinished {{cue}} or *stage direction*."""
        cut = len(self.buf)
        o = self.buf.rfind("{{")
        if o != -1 and self.buf.find("}}", o) == -1:
            cut = o
        if self.buf[:cut].count("*") % 2 == 1:
            cut = self.buf[:cut].rfind("*")
        return cut

    def feed(self, chunk: str) -> list[str]:
        self.buf += chunk
        out: list[str] = []
        limit = self._safe_cut()
        start = 0
        for m in _END.finditer(self.buf, 0, limit):
            end = m.end()
            piece = self.buf[start:end]
            if _ABBREV.search(piece.rstrip()):
                continue
            said = clean(piece).strip()
            if len(said) < MIN_CHARS and not out and start == 0:
                continue                 # too short: keep it with the next sentence
            if said:
                out.append(said)
            start = end
        self.buf = self.buf[start:]
        return out

    def flush(self) -> str:
        out, self.buf = self.buf, ""
        return clean(_DANGLING_CUE.sub("", out)).strip()
