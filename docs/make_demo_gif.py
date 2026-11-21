"""Draws docs/demo.gif, the animated terminal at the top of the README: `cswitch log`
(scheduled primes and an automatic switch), `cswitch status` and an unattended `cswitch run`
that waits for a reset and finishes. The lines follow cSwitcher's real output formats;
names and numbers are examples. Run: uv run --with pillow docs/make_demo_gif.py"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT, PAD, TITLE_BAR = 1040, 400, 22, 34
FONT_SIZE, LINE_HEIGHT = 15, 22
BACKGROUND, WINDOW, BAR = "#0b0f14", "#11161d", "#1b222c"
COLORS = {"text": "#d6dde6", "dim": "#7d8794", "prompt": "#6cc070", "cswitch": "#56b6c2", "live": "#e5c07b",
          "tool": "#c678dd", "good": "#98c379"}
TYPE_MS, LINE_MS = 38, 90

SESSION = "8c1f2e7a-4b9d-4f16-a0c3-5d2e9b7f1a64"
SCENES = [
    ("Mon 10:45", "cswitch log -n 5", [
        [("dim", "2026-11-23T04:40:02  "), ("text", "prime: 'work' primed: resets 09:40")],
        [("dim", "2026-11-23T06:22:05  "), ("text", "prime: 'personal' primed: resets 11:22")],
        [("dim", "2026-11-23T08:04:03  "), ("text", "prime: 'other' primed: resets 13:04")],
        [("dim", "2026-11-23T09:50:04  "), ("text", "prime: 'work' primed: resets 14:50")],
        [("dim", "2026-11-23T10:41:58  "), ("cswitch", "PostToolUse: switched 'work' -> 'personal': "
                                                       "'work' reached a threshold")],
    ]),
    ("Mon 10:46", "cswitch status", [
        [("text", "  work             5h 93% (resets Mon 14:50)   7d 41% (resets Tue 16:00)   seen Mon 10:41")],
        [("live", "* personal         5h 12% (resets Mon 11:22)   7d 63% (resets Tue 22:00)   seen Mon 10:46")],
        [("text", "  other            5h 0% (resets Mon 13:04)    7d 70% (resets Sat 16:59)   seen Mon 08:04"),
         ("dim", "  (keeps 20% weekly)")],
        [("text", "Stays: 'personal' is below the thresholds (thresholds: 5h 90%, 7d 95%).")],
        [("dim", "Scheduled priming: spread, between 04:40 and 20:00 on mon, tue, wed, thu, fri.")],
    ]),
    ("Mon 22:58", 'cswitch run "fix all failing tests"', [
        [("dim", rf"Session {SESSION} in C:\projects\shop. Stop with Ctrl-C;")],
        [("dim", f"continue later from this folder with: cswitch run --resume {SESSION}")],
        [("text", "I'll run the test suite first to see what fails.")],
        [("tool", "  > Bash pytest -q")],
        [("tool", r"  > Read C:\projects\shop\src\shop\cart.py")],
        [("tool", r"  > Edit C:\projects\shop\src\shop\cart.py")],
        ("clock", "Tue 00:41"),
        [("cswitch", "[cswitch] every account is used up; waiting until Tue 01:03")],
        ("clock", "Tue 01:03"),
        [("tool", "  > Bash pytest -q")],
        [("text", "All 214 tests pass now.")],
        [("good", "[cswitch] finished: All 214 tests pass now.")],
    ]),
]


def main() -> None:
    fonts = _fonts()
    frames: list[Image.Image] = []
    durations: list[int] = []

    def frame(clock: str, lines: list, typing: str | None, ms: int) -> None:
        frames.append(_draw(fonts, clock, lines, typing))
        durations.append(ms)

    for clock, command, output in SCENES:
        shown: list = []
        for end in range(len(command) + 1):
            frame(clock, shown, command[:end], TYPE_MS if end < len(command) else 450)
        shown.append([("prompt", "PS> "), ("text", command)])
        for item in output:
            if isinstance(item, tuple):
                frame(clock, shown, None, 900)
                clock = item[1]
                frame(clock, shown, None, 700)
                continue
            shown.append(item)
            frame(clock, shown, None, LINE_MS)
        frame(clock, shown, "", 2600 if command != SCENES[-1][1] else 4000)
    out = Path(__file__).with_name("demo.gif")
    frames[0].save(out, save_all=True, append_images=frames[1:], duration=durations, loop=0, optimize=True)
    print(f"{out} ({out.stat().st_size // 1024} KB, {len(frames)} frames)")


def _fonts() -> tuple[ImageFont.FreeTypeFont, ImageFont.FreeTypeFont]:
    for regular, bold in (("consola.ttf", "consolab.ttf"), ("DejaVuSansMono.ttf", "DejaVuSansMono-Bold.ttf")):
        try:
            return ImageFont.truetype(regular, FONT_SIZE), ImageFont.truetype(bold, 13)
        except OSError:
            continue
    raise SystemExit("No monospace font found (Consolas or DejaVu Sans Mono).")


def _draw(fonts: tuple, clock: str, lines: list, typing: str | None) -> Image.Image:
    font, title_font = fonts
    image = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((10, 10, WIDTH - 10, HEIGHT - 10), radius=10, fill=WINDOW)
    draw.rounded_rectangle((10, 10, WIDTH - 10, 10 + TITLE_BAR), radius=10, fill=BAR)
    draw.rectangle((10, 10 + TITLE_BAR - 10, WIDTH - 10, 10 + TITLE_BAR), fill=BAR)
    for i, color in enumerate(("#ff5f57", "#febc2e", "#28c840")):
        draw.ellipse((26 + i * 20, 22, 38 + i * 20, 34), fill=color)
    draw.text((WIDTH / 2, 27), "cSwitcher  -  PowerShell", font=title_font, fill=COLORS["dim"], anchor="mm")
    draw.text((WIDTH - 28, 27), clock, font=title_font, fill=COLORS["dim"], anchor="rm")
    rows = list(lines) + ([[("prompt", "PS> "), ("text", typing + "_")]] if typing is not None else [])
    visible = (HEIGHT - TITLE_BAR - 2 * PAD - 10) // LINE_HEIGHT
    y = 10 + TITLE_BAR + PAD
    for row in rows[-visible:]:
        x = PAD + 10
        for style, text in row:
            draw.text((x, y), text, font=font, fill=COLORS[style])
            x += draw.textlength(text, font=font)
        y += LINE_HEIGHT
    return image


if __name__ == "__main__":
    main()
