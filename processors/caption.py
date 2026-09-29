"""Базовый фильтр уникализации пресета tiktok и отрисовка надписи (caption).

Раньше жило в отдельном скрипте, который пресет импортировал
как модуль. Для сборки в один файл это не годится (PyInstaller плохо
переваривает импорты «из корня проекта»), поэтому код перенесён внутрь
processors/ — см. план, шаг 6, пункт 5.
"""
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def _ffmpeg() -> str:
    """ffmpeg: вшитый в сборку или системный (см. app/binaries.py).
    Скрипт запускают и напрямую, без пакета app — тогда просто "ffmpeg"."""
    try:
        from app.binaries import ffmpeg
        return ffmpeg()
    except ImportError:
        return "ffmpeg"



ROOT = Path(__file__).resolve().parent.parent  # корень проекта (processors/..)

# Окно чистого видео в исходнике 720x1280
CROP_W = 720
CROP_H = 560
CROP_X = 0
CROP_Y = 440

# Итоговый кадр для TikTok (9:16)
OUT_W = 1080
OUT_H = 1920

VIDEO_H = round(CROP_H * OUT_W / CROP_W)
VERTICAL_OFFSET = 120
VIDEO_TOP = (OUT_H - VIDEO_H) // 2 + VERTICAL_OFFSET

# Уникализация для TikTok:
# - zoom 3% (кроп + ресайз обратно) — меняет пиксельную структуру
# - eq: чуть другой контраст/насыщенность чем в Instagram версии
# - noise: лёгкое зерно — разрушает Content ID паттерн
# - setpts: ускорение на 1.5% — меняет тайминг кадров
ZOOM = 0.97  # обрезаем 3% по краям

def make_base_filter(crop_w=CROP_W, crop_h=CROP_H, crop_x=CROP_X, crop_y=CROP_Y):
    """Фильтр уникализации с заданным окном кропа исходного видео.

    Дефолт — под vgetreels 720×1280. Другие источники (инста 1080×1920)
    передают свои координаты, не меняя пресет уникализации (zoom/eq/setpts).
    """
    video_h = round(crop_h * OUT_W / crop_w)
    video_top = (OUT_H - video_h) // 2 + VERTICAL_OFFSET
    return (
        f"crop={crop_w}:{crop_h}:{crop_x}:{crop_y},"
        f"crop=iw*{ZOOM}:ih*{ZOOM}:(iw-iw*{ZOOM})/2:(ih-ih*{ZOOM})/2,"
        f"scale={OUT_W}:-2:flags=lanczos,"
        "unsharp=5:5:1.0:5:5:0.0,"
        "eq=contrast=1.08:brightness=0.01:saturation=1.15,"
        "setpts=0.985*PTS,"
        "setsar=1,"
        f"pad={OUT_W}:{OUT_H}:0:{video_top}:white"
    )


BASE_FILTER = make_base_filter()

# шрифт надписи вшивается в собранную программу — путь через
# config.resource_path (в сборке это временная папка PyInstaller)
try:
    from app.config import resource_path
    FONT = str(resource_path("fonts/SourceSans3.ttf"))
except ImportError:
    FONT = str(ROOT / "fonts" / "SourceSans3.ttf")
# системный шрифт эмодзи: на macOS свой, на Windows свой, где-то его нет
# вовсе — тогда None и эмодзи просто не рисуются (см. platform_utils)
try:
    from app.platform_utils import emoji_font_path
    EMOJI_FONT = emoji_font_path()
except ImportError:  # скрипт запускают и напрямую, без пакета app
    EMOJI_FONT = "/System/Library/Fonts/Apple Color Emoji.ttc"
FONT_VARIATION = 900
FONT_SIZE = 78
STROKE_W = 0
BOLD_EXTRA = 0
STRETCH = 1.0
LINE_SPACING = -18
LETTER_SPACING = 0
GAP_ABOVE_VIDEO = 24

PALETTE = {
    "b": ((200, 60, 0, 255), (200, 60, 0, 255)),
    "k": ((30, 40, 120, 255), (30, 40, 120, 255)),
    "r": ((205, 15, 20, 255), (205, 15, 20, 255)),
    "y": ((245, 200, 0, 255), (245, 200, 0, 255)),
    "w": ((255, 255, 255, 255), (255, 255, 255, 255)),
    "g": ((0, 150, 90, 255), (0, 150, 90, 255)),
}
DEFAULT_COLOR = "b"

EXTS = {".mp4", ".mov", ".m4v", ".mkv"}


def parse_line(raw: str) -> tuple[str, tuple, tuple]:
    raw = raw.strip()
    fill, stroke = PALETTE[DEFAULT_COLOR]
    if len(raw) >= 3 and raw[0] == "[" and raw[2] == "]" and raw[1] in PALETTE:
        fill, stroke = PALETTE[raw[1]]
        raw = raw[3:].strip()
    return raw.upper(), fill, stroke


def render_caption_png(text: str, out_png: Path) -> None:
    parsed = [parse_line(ln) for ln in text.splitlines() if ln.strip()]
    lines = [p[0] for p in parsed]
    font = ImageFont.truetype(FONT, FONT_SIZE)
    if FONT_VARIATION is not None:
        try:
            font.set_variation_by_axes([FONT_VARIATION])
        except Exception:
            pass

    base_w = int(OUT_W / STRETCH)
    img = Image.new("RGBA", (base_w, OUT_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    asc, desc = font.getmetrics()
    line_h = asc + desc + LINE_SPACING
    emoji_px = round(asc * 0.9)

    try:
        emoji_font = ImageFont.truetype(EMOJI_FONT, 160)
    except Exception:
        emoji_font = None

    def is_emoji(ch):
        o = ord(ch)
        return (0x1F000 <= o <= 0x1FAFF) or (0x2600 <= o <= 0x27BF) or o in (0x2764,)

    def char_box_w(ch):
        if is_emoji(ch):
            return emoji_px
        l, _, r, _ = font.getbbox(ch)
        return r - l

    def advance(ch):
        w = char_box_w(ch) if ch != " " else font.getlength(ch)
        return w + LETTER_SPACING

    def draw_emoji(dst, x, y, ch):
        if emoji_font is None:
            return
        tmp = Image.new("RGBA", (170, 170), (0, 0, 0, 0))
        ImageDraw.Draw(tmp).text((5, 5), ch, font=emoji_font, embedded_color=True)
        bb = tmp.getbbox()
        if bb:
            tmp = tmp.crop(bb).resize((emoji_px, emoji_px), Image.LANCZOS)
            dst.alpha_composite(tmp, (round(x), round(y + (asc - emoji_px) / 2)))

    def render_line_layer(line, fill, stroke):
        layer = Image.new("RGBA", (base_w, line_h + 80), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        x = 0.0
        for ch in line:
            if ch == " ":
                x += advance(ch)
                continue
            if is_emoji(ch):
                draw_emoji(layer, x, 0, ch)
                x += advance(ch)
                continue
            lb = font.getbbox(ch)[0]
            ld.text((x - lb, 0), ch, font=font, fill=stroke,
                    stroke_width=STROKE_W + BOLD_EXTRA, stroke_fill=stroke)
            ld.text((x - lb, 0), ch, font=font, fill=fill,
                    stroke_width=BOLD_EXTRA, stroke_fill=fill)
            x += advance(ch)
        return layer, layer.getbbox()

    block_h = line_h * len(lines)
    y = max(30, VIDEO_TOP - GAP_ABOVE_VIDEO - block_h)
    for line, fill, stroke in parsed:
        layer, bb = render_line_layer(line, fill, stroke)
        if bb:
            line_real_w = bb[2] - bb[0]
            dst_x = round((base_w - line_real_w) / 2 - bb[0])
            img.alpha_composite(layer, (dst_x, round(y)))
        y += line_h

    img = img.resize((OUT_W, OUT_H), Image.LANCZOS)
    img.save(out_png)


