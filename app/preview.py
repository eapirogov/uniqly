"""Кадр-превью из видео через ffmpeg."""
import subprocess
from pathlib import Path

from app.config import PREVIEWS
from app import binaries, ffmpeg_error


def make_preview(video_path: Path, at_sec: float = 1.0) -> Path:
    """Снимает один кадр на отметке at_sec, сохраняет JPEG в previews/.

    Если на этой отметке кадра нет — берём самый первый. Так бывает у роликов
    короче секунды: ffmpeg честно доходит до конца файла, не находит кадра и
    падает. Готовый ролик при этом в порядке, а пользователю показывалась
    ошибка про «рамку обрезки» — по общему правилу разбора ffmpeg-логов.
    """
    video_path = Path(video_path)
    out = PREVIEWS / f"{video_path.stem}.jpg"

    def _grab(seek: float):
        cmd = [
            binaries.ffmpeg(), "-y", "-ss", str(seek), "-i", str(video_path),
            "-frames:v", "1", "-q:v", "3", str(out),
        ]
        return binaries.run(cmd, capture_output=True, text=True), cmd

    result, cmd = _grab(at_sec)
    if (result.returncode != 0 or not out.exists()) and at_sec > 0:
        result, cmd = _grab(0)          # ролик короче отметки — первый кадр
    if result.returncode != 0 or not out.exists():
        raise ffmpeg_error.fail(result.stderr, cmd)
    return out
