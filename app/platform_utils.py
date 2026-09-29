"""Мелочи, которые на разных системах делаются по-разному.

Собраны в одном месте, чтобы не искать по всему коду, что чинить при
переносе на Windows (у большинства пользователей именно она).
"""
import os
import subprocess
import sys
from pathlib import Path


def reveal_in_file_manager(path: str | Path) -> None:
    """Показать файл в проводнике, выделив его.

    macOS   open -R
    Windows explorer /select,
    Linux   xdg-open на папку (выделить файл штатно нельзя)
    """
    path = Path(path)
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", "-R", str(path)])
        elif os.name == "nt":
            # explorer возвращает ненулевой код даже при успехе — не проверяем
            subprocess.run(["explorer", f"/select,{path}"])
        else:
            subprocess.run(["xdg-open", str(path.parent)])
    except OSError:
        pass  # не открылось — не повод ронять интерфейс


def file_manager_name() -> str:
    """Как называется проводник файлов в этой системе.

    Кнопки подписаны этим словом: на Windows «Finder» — бессмысленное слово,
    пользователь не понимает, куда его отправляют.
    """
    if sys.platform == "darwin":
        return "Finder"
    if os.name == "nt":
        return "Проводник"
    return "Папка"


def open_url(url: str) -> None:
    """Открыть ссылку в браузере по умолчанию."""
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", url])
        elif os.name == "nt":
            os.startfile(url)  # noqa: S606 — штатный способ на Windows
        else:
            subprocess.run(["xdg-open", url])
    except (OSError, AttributeError):
        pass


def emoji_font_path() -> str | None:
    """Системный шрифт с цветными эмодзи или None, если такого нет.

    Нужен пресету tiktok для эмодзи в надписи. Раньше путь был жёстко
    macOS-ный — на Windows пресет из-за этого падал.
    """
    candidates = []
    if sys.platform == "darwin":
        candidates = ["/System/Library/Fonts/Apple Color Emoji.ttc"]
    elif os.name == "nt":
        win = os.getenv("WINDIR", r"C:\Windows")
        candidates = [os.path.join(win, "Fonts", "seguiemj.ttf")]  # Segoe UI Emoji
    else:
        candidates = [
            "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
            "/usr/share/fonts/noto/NotoColorEmoji.ttf",
        ]
    for c in candidates:
        if Path(c).exists():
            return c
    return None
