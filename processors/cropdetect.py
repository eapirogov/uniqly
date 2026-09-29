"""Автодетект чёрных полос (letterbox/pillarbox) через ffmpeg cropdetect.

Проблема: разные исходники (YouTube/Instagram) имеют разную толщину чёрных
полей — нельзя зашить одни числа на все ролики (см. processors/uniq.py,
где сейчас 1080:1280:0:422 — фиксированный кроп под старый формат исходников).

Подход — консервативный, не медиана:
- берём несколько временных семплов по видео (не с самого начала/конца —
  там часто интро/аутро без полос или чёрный кадр-заглушка);
- на каждом гоняем cropdetect, получаем прямоугольник «полезного» кадра;
- тёмные сцены заставляют cropdetect ЗАНИЖАТЬ полезную область (принимает
  тёмный контент за продолжение чёрного поля) — то есть ошибка ВСЕГДА в одну
  сторону (обрезать больше, чем нужно), никогда в другую (границы полос
  физически не «дышат», они одинаковы на всех кадрах);
- поэтому правильная агрегация — не медиана, а МАКСИМУМ полезной площади
  среди семплов (= минимальная обрезка): семпл, давший наибольшую area,
  и есть кадр без искажающей тёмной сцены.

Результат — стартовая подсказка для ручного редактора (crop_editor.py),
не финальное решение — юзер всегда может подвинуть рамку сам.
"""
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from app import binaries

_CROP_RE = re.compile(r"crop=(\d+):(\d+):(\d+):(\d+)")


@dataclass
class CropBox:
    w: int
    h: int
    x: int
    y: int
    confident: bool  # семплы согласились между собой (низкий разброс площади)

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.w, self.h, self.x, self.y)


def _probe_duration(path: Path) -> float:
    out = binaries.run(
        [binaries.ffprobe(), "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0


def probe_size(path: str | Path) -> tuple[int, int]:
    """Реальное разрешение видео (ширина, высота) в пикселях, без учёта SAR.

    Битый/неполный файл — это ошибка, а не размер (0, 0): раньше нули молча
    расходились по конвейеру, рамка обрезки становилась (0,0,0,0), и падало
    уже в другом месте, где причину было не видно. Лучше сказать сразу.
    """
    out = binaries.run(
        [binaries.ffprobe(), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    try:
        w, h = out.stdout.strip().split(",")
        w, h = int(w), int(h)
    except (ValueError, AttributeError):
        w = h = 0
    if w <= 0 or h <= 0:
        if not Path(path).exists():
            raise RuntimeError(
                "Исходный файл не найден: его удалили или переместили.")
        raise RuntimeError(
            "Не удалось прочитать видео — файл повреждён или скачался не "
            "полностью. Удалите ролик и скачайте ссылку заново.")
    return w, h


def _sample_crop(path: Path, at_sec: float, limit: str = "24:2:0") -> tuple[int, int, int, int] | None:
    """Гоняет cropdetect на ~1с видео начиная с at_sec. Возвращает последний
    найденный crop=W:H:X:Y (самая свежая/устойчивая оценка на этом отрезке)."""
    cmd = [
        binaries.ffmpeg(), "-ss", str(max(at_sec, 0)), "-i", str(path),
        "-t", "1", "-vf", f"cropdetect={limit}", "-f", "null", "-",
    ]
    result = binaries.run(cmd, capture_output=True, text=True, timeout=30)
    matches = _CROP_RE.findall(result.stderr)
    if not matches:
        return None
    w, h, x, y = matches[-1]
    return int(w), int(h), int(x), int(y)


def detect(path: str | Path, samples: int = 6) -> CropBox | None:
    """Находит границы полезного кадра без чёрных полос. None — если видео
    слишком короткое или cropdetect не дал ни одного результата (нет ffmpeg,
    битый файл и т.п.) — вызывающий код в этом случае берёт полный кадр."""
    path = Path(path)
    duration = _probe_duration(path)
    if duration <= 0:
        return None

    # семплы между 10% и 90% длительности — не задеваем интро/аутро
    lo, hi = duration * 0.1, duration * 0.9
    if hi <= lo:
        points = [duration / 2]
    else:
        step = (hi - lo) / max(samples - 1, 1)
        points = [lo + step * i for i in range(samples)]

    results = []
    for t in points:
        box = _sample_crop(path, t)
        if box:
            results.append(box)

    if not results:
        return None

    # консервативная агрегация: максимальная площадь = минимальная обрезка
    best = max(results, key=lambda b: b[0] * b[1])
    areas = [w * h for w, h, _, _ in results]
    spread = (max(areas) - min(areas)) / max(areas)  # доля разброса
    confident = spread < 0.05  # семплы согласны в пределах 5% площади

    w, h, x, y = best
    return CropBox(w=w, h=h, x=x, y=y, confident=confident)
