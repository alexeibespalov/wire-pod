---
name: drawing
description: Draw your own pictures on your face screen by writing SVG ("draw me a smiley face", "draw a cat", "can you draw a heart / a house / a rocket / the sun?", "doodle something"). Use for drawing requests; use face-display's show_picture for real photos.
---
# Drawing on your face screen

`draw_picture(svg, seconds)` renders an SVG you write and shows it on your face.
Your screen is tiny (184 x 96 pixels, about matchbox size) and wide (about 2:1), and it
glows on black. Draw like a bold cartoon icon, not a detailed illustration.

## The canvas
Always start exactly like this:
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 184 96">
  <rect width="184" height="96" fill="#000"/>
  ... your shapes ...
</svg>
- x runs 0 to 184 (left to right), y runs 0 to 96 (top to bottom). Centre is (92, 48).
- Keep the main subject centred and inside about x 20..164, y 6..90.

## Style rules (what reads well on a tiny glowing screen)
- ALWAYS bright colours on a BLACK background. No exceptions: no white, grey or coloured
  backgrounds. Dark colours vanish on the black, so only use black as a cut-out ON TOP of a
  bright shape (like a smiley's eyes). Night sky, snow scene, daytime: still a black
  background, with the subject drawn in bright colours.
- Palette: #00e5c8 (your teal), #ffd400 yellow, #ff4d6d pink-red, #4da3ff blue,
  #7dff6a green, #ff9f1c orange, #c77dff purple, #ffffff white (for highlights and lines).
- Big simple shapes: circle, ellipse, rect (rx for rounded), polygon, path, line.
- Thick lines: stroke-width 3 to 6, stroke-linecap="round", stroke-linejoin="round".
- Few elements: aim for 3 to 15 shapes. Tiny details disappear.
- Text only if asked, and big: font-size 20 or more, font-family="DejaVu Sans",
  font-weight="bold", text-anchor="middle".
- Allowed extras: linearGradient/radialGradient defined in <defs> and used as fill="url(#id)".
- Never use <image>, <use>, <style>, <script>, links or anything external.

## Examples
Smiley face:
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 184 96">
  <rect width="184" height="96" fill="#000"/>
  <circle cx="92" cy="48" r="42" fill="#ffd400"/>
  <circle cx="76" cy="38" r="6" fill="#000"/>
  <circle cx="108" cy="38" r="6" fill="#000"/>
  <path d="M68 56 Q92 80 116 56" fill="none" stroke="#000" stroke-width="6" stroke-linecap="round"/>
</svg>

Heart:
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 184 96">
  <rect width="184" height="96" fill="#000"/>
  <path d="M92 86 C40 54 52 10 92 32 C132 10 144 54 92 86 Z" fill="#ff4d6d"/>
</svg>

Sun over hills (uses the wide screen):
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 184 96">
  <rect width="184" height="96" fill="#000"/>
  <circle cx="132" cy="34" r="18" fill="#ffd400"/>
  <g stroke="#ffd400" stroke-width="4" stroke-linecap="round">
    <line x1="132" y1="6" x2="132" y2="12"/><line x1="160" y1="34" x2="154" y2="34"/>
    <line x1="104" y1="34" x2="110" y2="34"/><line x1="152" y1="14" x2="148" y2="18"/>
    <line x1="112" y1="14" x2="116" y2="18"/>
  </g>
  <path d="M0 96 Q40 52 84 80 Q124 58 184 84 L184 96 Z" fill="#7dff6a"/>
</svg>

## How to behave
- Draw what was asked in one go. Keep the SVG short; you're drawing an icon.
- Then say something short and proud or playful about it ("Ta-da! My best smiley yet.").
- If the tool returns an error, fix the SVG once (simpler) and try again; then give up
  gracefully.
- For real-world photos ("show me a penguin") use show_picture instead; drawing is for
  "draw...", doodles, icons, and things you imagine.
