"""Где взять ffmpeg и ffprobe.

Из исходников — те, что стоят в системе (напр. Homebrew). В собранной
программе системного ffmpeg у пользователя может не быть вовсе, поэтому
статические сборки кладутся внутрь программы, и путь берётся оттуда.

Весь код зовёт ffmpeg просто по имени, поэтому здесь возвращается путь, а
вызывающие места подставляют его первым элементом команды.
"""
import os
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

from app.config import resource_path


def _bundled(name: str) -> Path | None:
    """Вшитый в сборку бинарник под текущую систему, если он там есть."""
    if os.name == "nt":
        folder, suffix = "bin/windows-x64", ".exe"
    elif sys.platform == "darwin":
        folder = "bin/macos-arm64" if _is_arm() else "bin/macos-x64"
        suffix = ""
    else:
        folder, suffix = "bin/linux-x64", ""
    path = resource_path(f"{folder}/{name}{suffix}")
    return path if path.exists() else None


def _is_arm() -> bool:
    import platform
    return platform.machine() in ("arm64", "aarch64")


@lru_cache(maxsize=None)
def _resolve(name: str) -> str:
    """Путь к бинарнику: вшитый → системный → просто имя (пусть падает с
    внятной ошибкой при запуске, а не при импорте)."""
    bundled = _bundled(name)
    if bundled:
        # права на исполнение теряются при распаковке некоторыми архиваторами
        try:
            os.chmod(bundled, 0o755)
        except OSError:
            pass
        return str(bundled)
    system = shutil.which(name)
    return system or name


def ffmpeg() -> str:
    """Путь к ffmpeg."""
    return _resolve("ffmpeg")


def ffprobe() -> str:
    """Путь к ffprobe."""
    return _resolve("ffprobe")


def available() -> bool:
    """Есть ли рабочий ffmpeg — для понятного сообщения при старте."""
    try:
        r = run([ffmpeg(), "-version"], capture_output=True, timeout=10)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False

# --- запуск без мигающих окон консоли (Windows) ---
#
# На Windows каждый запуск .exe из программы создаёт окно консоли. У ffmpeg
# своего окна нет, но система всё равно рисует его на долю секунды — при
# скачивании и на каждом движении ползунка окна выскакивали десятками.
# На macOS/Linux флага нет и он игнорируется: поведение прежнее.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run(cmd, **kwargs):
    """subprocess.run для ffmpeg/ffprobe — без окна консоли на Windows.

    Все вызовы ffmpeg/ffprobe в проекте идут через неё, чтобы флаг не нужно
    было дописывать в каждое место (их 19) и не забыть в новых.
    """
    if _NO_WINDOW:
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | _NO_WINDOW
    return subprocess.run(cmd, **kwargs)
