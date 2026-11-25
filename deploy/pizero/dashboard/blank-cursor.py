"""Write an Xcursor theme called "blank" whose every pointer is one transparent pixel.

cage has no option to hide the pointer, and the touch panel makes it draw one in the middle
of the screen. cortana-dash.service points XCURSOR_PATH / XCURSOR_THEME here.

    python3 blank-cursor.py /opt/cortana-dash/cursors
"""
import struct
import sys
from pathlib import Path

NAMES = ["default", "left_ptr", "text", "xterm", "pointer", "hand1", "hand2", "top_left_arrow",
         "arrow", "ibeam", "crosshair", "wait", "watch", "progress", "not-allowed", "grabbing", "grab"]


def xcursor() -> bytes:
    image_type, size = 0xFFFD0002, 1
    header = struct.pack("<4sIII", b"Xcur", 16, 0x10000, 1)
    toc = struct.pack("<III", image_type, size, 16 + 12)
    image = struct.pack("<IIIIIIIII", 36, image_type, size, 1, 1, 1, 0, 0, 0) + struct.pack("<I", 0)
    return header + toc + image


def main(root: str) -> None:
    theme = Path(root) / "blank"
    cursors = theme / "cursors"
    cursors.mkdir(parents=True, exist_ok=True)
    (theme / "index.theme").write_text("[Icon Theme]\nName=blank\n")
    data = xcursor()
    for name in NAMES:
        (cursors / name).write_bytes(data)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "cursors")
