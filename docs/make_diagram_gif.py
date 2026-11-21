"""Draws docs/how-it-works.gif, the animated diagram in the README: a Claude Code chat
works, cSwitcher checks the live account's usage after each step, switches the login
from 'work' to 'personal' at the 90% threshold while the chat goes on, and switches back
when 'work' resets, since it is first in the order. Names and numbers are examples.
Run: uv run --no-project --with pillow docs/make_diagram_gif.py"""

from dataclasses import dataclass, replace
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 1040, 440
BACKGROUND, BOX, EDGE = "#0b0f14", "#141a22", "#2a333f"
TEXT, DIM, CYAN, YELLOW, PURPLE, GREEN = "#d6dde6", "#7d8794", "#56b6c2", "#e5c07b", "#c678dd", "#98c379"
CHAT, CORE, API = (40, 150, 300, 300), (400, 175, 600, 275), (410, 40, 590, 100)
CARD_X, CARD_W, CARD_H, CARD_TOPS = 700, 300, 100, {"work": 36, "personal": 160, "other": 284}
TOOLS = ["> Bash pytest -q", "> Read src/shop/cart.py", "> Edit src/shop/cart.py", "> Bash pytest -q",
         "> Edit tests/test_cart.py", "> Read src/shop/prices.py"]


@dataclass(frozen=True)
class State:
    clock: str
    caption: str
    work: float
    personal: float
    live_y: float
    step: int = 0
    flash: bool = False


def main() -> None:
    fonts = _fonts()
    frames, durations = [], []

    def show(state: State, ms: int) -> None:
        frames.append(_draw(fonts, state))
        durations.append(ms)

    work_y, personal_y = _card_middle("work"), _card_middle("personal")
    state = State("Mon 10:38", "Claude works; after each step cSwitcher checks the live account's usage", 78, 12, work_y)
    for i in range(25):
        state = replace(state, work=78 + i * 0.5, step=i // 4)
        show(state, 1400 if i == 0 else 110)
    state = replace(state, caption="'work' reached 90% of its 5-hour window, so cSwitcher switches the login", flash=True)
    show(state, 900)
    for i in range(1, 9):
        show(replace(state, live_y=work_y + (personal_y - work_y) * _ease(i / 8)), 70)
    state = replace(state, live_y=personal_y, flash=False, clock="Mon 10:45",
                    caption="The same chat keeps going, now on 'personal' - no restart, nothing lost")
    for i in range(20):
        state = replace(state, personal=12 + i * 0.8, step=6 + i // 4)
        show(state, 1500 if i == 0 else 110)
    state = replace(state, clock="Mon 14:53", caption="14:50: 'work' resets, and it is first in your order...")
    for work in (70, 45, 20, 0):
        show(replace(state, work=work), 90)
    state = replace(state, work=0)
    show(state, 1300)
    state = replace(state, caption="...so cSwitcher switches back to 'work'", flash=True)
    for i in range(1, 9):
        show(replace(state, live_y=personal_y + (work_y - personal_y) * _ease(i / 8)), 70)
    show(replace(state, live_y=work_y, flash=False), 3200)
    out = Path(__file__).with_name("how-it-works.gif")
    frames[0].save(out, save_all=True, append_images=frames[1:], duration=durations, loop=0, optimize=True)
    print(f"{out} ({out.stat().st_size // 1024} KB, {len(frames)} frames, {sum(durations) / 1000:.1f} s)")


def _fonts() -> dict[str, ImageFont.FreeTypeFont]:
    for regular, bold in (("consola.ttf", "consolab.ttf"), ("DejaVuSansMono.ttf", "DejaVuSansMono-Bold.ttf")):
        try:
            return {"text": ImageFont.truetype(regular, 14), "small": ImageFont.truetype(regular, 12),
                    "bold": ImageFont.truetype(bold, 15), "caption": ImageFont.truetype(regular, 16)}
        except OSError:
            continue
    raise SystemExit("No monospace font found (Consolas or DejaVu Sans Mono).")


def _ease(t: float) -> float:
    return t * t * (3 - 2 * t)


def _card_middle(name: str) -> float:
    return CARD_TOPS[name] + CARD_H / 2


def _draw(fonts: dict, state: State) -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
    draw = ImageDraw.Draw(image)
    draw.text((WIDTH - 24, 18), state.clock, font=fonts["small"], fill=DIM, anchor="ra")

    draw.rounded_rectangle(CHAT, radius=10, fill=BOX, outline=EDGE, width=2)
    draw.text((CHAT[0] + 16, CHAT[1] + 14), "Claude Code chat", font=fonts["bold"], fill=TEXT)
    for row, tool in enumerate(TOOLS[i % len(TOOLS)] for i in range(state.step, state.step + 4)):
        draw.text((CHAT[0] + 16, CHAT[1] + 50 + row * 22), tool, font=fonts["text"], fill=PURPLE if row == 3 else DIM)

    core_edge = CYAN if state.flash else EDGE
    draw.rounded_rectangle(CORE, radius=10, fill="#10262a" if state.flash else BOX, outline=core_edge, width=2)
    draw.text(((CORE[0] + CORE[2]) / 2, CORE[1] + 32), "cSwitcher", font=fonts["bold"], fill=CYAN, anchor="mm")
    draw.text(((CORE[0] + CORE[2]) / 2, CORE[1] + 62), "switch!" if state.flash else "usage ok",
              font=fonts["text"], fill=YELLOW if state.flash else GREEN, anchor="mm")
    _arrow(draw, (CHAT[2], 225), (CORE[0], 225), EDGE)
    draw.text(((CHAT[2] + CORE[0]) / 2, 210), "hook", font=fonts["small"], fill=DIM, anchor="mm")

    draw.rounded_rectangle(API, radius=8, fill=BOX, outline=EDGE, width=1)
    draw.text(((API[0] + API[2]) / 2, (API[1] + API[3]) / 2), "Anthropic usage", font=fonts["text"], fill=TEXT, anchor="mm")
    for y in range(API[3] + 4, CORE[1] - 2, 8):
        draw.line((500, y, 500, y + 4), fill=DIM, width=2)
    draw.text((510, 137), "read-only", font=fonts["small"], fill=DIM, anchor="lm")

    draw.line((CORE[2], 225, CARD_X, state.live_y), fill=YELLOW, width=4)
    draw.text(((CORE[2] + CARD_X) / 2, 252), "login", font=fonts["small"], fill=YELLOW, anchor="mm")
    _card(draw, fonts, "work", state.work, 41, 95, abs(state.live_y - _card_middle("work")) < 1)
    _card(draw, fonts, "personal", state.personal, 63, 95, abs(state.live_y - _card_middle("personal")) < 1)
    _card(draw, fonts, "other", 0, 70, 80, False, "keeps 20%")

    draw.rectangle((0, HEIGHT - 44, WIDTH, HEIGHT), fill="#0f141b")
    draw.text((WIDTH / 2, HEIGHT - 22), state.caption, font=fonts["caption"], fill=TEXT, anchor="mm")
    return image


def _card(draw: ImageDraw.ImageDraw, fonts: dict, name: str, five: float, week: float, week_limit: float,
          live: bool, note: str = "") -> None:
    top = CARD_TOPS[name]
    draw.rounded_rectangle((CARD_X, top, CARD_X + CARD_W, top + CARD_H), radius=10, fill=BOX,
                           outline=YELLOW if live else EDGE, width=3 if live else 2)
    draw.text((CARD_X + 16, top + 12), ("* " if live else "") + name, font=fonts["bold"], fill=YELLOW if live else TEXT)
    if note:
        draw.text((CARD_X + CARD_W - 14, top + 14), note, font=fonts["small"], fill=DIM, anchor="ra")
    for row, (label, used, limit) in enumerate((("5h", five, 90), ("7d", week, week_limit))):
        y = top + 46 + row * 28
        draw.text((CARD_X + 16, y + 6), label, font=fonts["small"], fill=DIM, anchor="lm")
        left, right = CARD_X + 44, CARD_X + CARD_W - 64
        draw.rounded_rectangle((left, y, right, y + 12), radius=4, fill="#222a35")
        color = YELLOW if used >= limit else (CYAN if live else "#4b5563")
        if used > 0:
            draw.rounded_rectangle((left, y, left + (right - left) * min(used, 100) / 100, y + 12), radius=4, fill=color)
        tick = left + (right - left) * limit / 100
        draw.line((tick, y - 3, tick, y + 15), fill=TEXT, width=2)
        draw.text((CARD_X + CARD_W - 14, y + 6), f"{used:.0f}%", font=fonts["small"], fill=TEXT, anchor="rm")


def _arrow(draw: ImageDraw.ImageDraw, start: tuple, end: tuple, color: str) -> None:
    draw.line((*start, *end), fill=color, width=3)
    draw.polygon([end, (end[0] - 10, end[1] - 6), (end[0] - 10, end[1] + 6)], fill=color)


if __name__ == "__main__":
    main()
