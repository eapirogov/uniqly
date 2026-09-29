"""Перевод ошибок ffmpeg на человеческий язык.

Раньше сбой рендера попадал на карточку ролика как есть — до 500 символов
лога вида «Task finished with error code: -22 (Invalid argument)». Понять по
такому, что делать, нельзя.

Здесь по тексту stderr определяется причина и выдаётся короткая фраза с
подсказкой. Полный лог не выбрасываем — он уходит в файл (см. log_details),
чтобы при разборе можно было посмотреть настоящую причину.
"""
from pathlib import Path

# (маркеры в stderr, что показать пользователю). Порядок важен: более
# частные случаи идут раньше общих.
_RULES: list[tuple[tuple[str, ...], str]] = [
    (("Invalid data found", "moov atom not found", "Invalid NAL"),
     "Файл ролика повреждён — скачался не полностью. "
     "Удалите его и скачайте ссылку заново."),
    (("No such file or directory", "does not exist"),
     "Исходный файл не найден: его удалили или переместили."),
    (("No space left", "Errno 28"),
     "На диске нет свободного места — освободите место и попробуйте снова."),
    (("Permission denied",),
     "Нет доступа к папке для сохранения. Проверьте права на папку с данными."),
    (("Invalid argument", "not open encoder", "received no packets",
      "Conversion failed"),
     "Не удалось собрать ролик с такими настройками. "
     "Чаще всего виновата рамка обрезки — нажмите «Весь кадр» в окне обрезки "
     "и попробуйте снова."),
    (("Output file is empty", "Nothing was written"),
     "Ролик получился пустым. Проверьте обрезку начала — возможно, она длиннее "
     "самого ролика."),
    (("Unknown encoder", "Encoder not found", "Unknown decoder"),
     "В этой сборке нет нужного кодека — сообщите разработчику."),
]

_FALLBACK = ("Не удалось обработать ролик. Попробуйте другой ролик или "
             "верните настройки кнопкой «Сбросить».")


def explain(stderr: str) -> str:
    """Короткая понятная причина сбоя ffmpeg по его stderr."""
    text = stderr or ""
    for markers, message in _RULES:
        if any(m in text for m in markers):
            return message
    return _FALLBACK


def log_details(stderr: str, cmd: list[str] | None = None) -> Path | None:
    """Пишет полный вывод ffmpeg в лог рядом с данными.

    Пользователю показывается короткая фраза, а подробности нужны при разборе —
    их можно попросить прислать. Не бросает исключений: вызывается в момент,
    когда уже что-то сломалось.
    """
    from datetime import datetime
    try:
        from app.config import WORKSPACE
        path = Path(WORKSPACE) / "uniqly-error.log"
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n{'=' * 60}\n{datetime.now():%Y-%m-%d %H:%M:%S} ffmpeg\n")
            if cmd:
                f.write(" ".join(str(c) for c in cmd) + "\n\n")
            f.write((stderr or "")[-4000:] + "\n")
        return path
    except OSError:
        return None


def fail(stderr: str, cmd: list[str] | None = None) -> RuntimeError:
    """Готовое исключение с понятным текстом; лог пишется побочно.

    Использование:  raise ffmpeg_error.fail(result.stderr, cmd)
    """
    log_details(stderr, cmd)
    return RuntimeError(explain(stderr))
