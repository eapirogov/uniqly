"""OCR надписей с кадров видео через tesseract (текст, вшитый в ролик)."""
import re
import subprocess
import tempfile
from pathlib import Path
from app import binaries


def _duration(video_path: Path) -> float:
    r = binaries.run(
        [binaries.ffprobe(), "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(video_path)],
        capture_output=True, text=True,
    )
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def read_onscreen_text(video_path: Path, n_frames: int = 6) -> str:
    """Читает надпись с кадров и возвращает самую ЧИСТУЮ версию (не склейку).

    Надпись анимируется/частично видна на разных кадрах → берём кадр, где
    tesseract прочитал её с минимумом мусора.
    """
    video_path = Path(video_path)
    dur = _duration(video_path)
    if dur <= 0:
        return ""
    candidates: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        for i in range(n_frames):
            frac = (i + 1) / (n_frames + 1)
            frame = Path(td) / f"f{i}.png"
            # берём только верхнюю половину кадра (надпись там) + апскейл ×2
            # и бинаризацию — tesseract точнее на крупном контрастном тексте
            binaries.run(
                [binaries.ffmpeg(), "-y", "-ss", f"{dur * frac:.2f}", "-i", str(video_path),
                 "-frames:v", "1",
                 "-vf", "crop=iw:ih/2:0:0,scale=iw*2:ih*2,format=gray",
                 str(frame)],
                capture_output=True,
            )
            if not frame.exists():
                continue
            # склеиваем строки одного кадра в одну надпись
            lines = [_clean(ln) for ln in _ocr_frame(frame).splitlines()]
            text = " ".join(ln for ln in lines if ln)
            if text:
                candidates.append(text)
    if not candidates:
        return ""
    # лучшая надпись = максимум букв при минимуме мусорных символов
    return max(candidates, key=_score)


def _score(text: str) -> float:
    """Оценка чистоты надписи: доля букв минус штраф за мусорные символы."""
    letters = len(re.sub(r"[^А-Яа-яЁёA-Za-z ]", "", text))
    junk = len(re.sub(r"[А-Яа-яЁёA-Za-z0-9 ]", "", text))  # |, „, ], скобки и пр.
    return letters - junk * 3


def _ocr_frame(frame: Path) -> str:
    result = binaries.run(
        ["tesseract", str(frame), "stdout", "-l", "rus+eng"],
        capture_output=True, text=True,
    )
    return result.stdout if result.returncode == 0 else ""


def _clean(line: str) -> str:
    """Отсеивает мусорные OCR-строки (обрывки, символьный шум)."""
    line = re.sub(r"\s+", " ", line).strip()
    letters = len(re.sub(r"[^А-Яа-яЁёA-Za-z]", "", line))
    junk = len(re.sub(r"[А-Яа-яЁёA-Za-z0-9 ]", "", line))
    if letters < 4:
        return ""
    if junk > letters * 0.4:  # больше 40% мусора → строка битая
        return ""
    return line
