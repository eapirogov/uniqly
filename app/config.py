"""Пути и конфиг приложения.

Рабочая папка (ролики + база) лежит РЯДОМ С КОДОМ только при запуске из
исходников. В собранной программе так нельзя: внутрь .app macOS запрещает
записывать, а в Program Files Windows требует прав администратора — приложение
не смогло бы сохранить ни ролик, ни базу. Поэтому в собранном виде данные
уходят в домашнюю папку пользователя.
"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

APP_NAME = "Uniqly"

ROOT = Path(__file__).resolve().parent.parent


def _is_frozen() -> bool:
    """Запущены из собранной программы (PyInstaller), а не из исходников."""
    return getattr(sys, "frozen", False)


def _user_data_dir() -> Path:
    """Папка данных пользователя по правилам его системы.

    macOS   ~/Library/Application Support/Uniqly
    Windows %APPDATA%\\Uniqly
    Linux   ~/.local/share/uniqly (XDG)
    """
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    if os.name == "nt":
        base = os.getenv("APPDATA") or (Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_NAME
    base = os.getenv("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base) / APP_NAME.lower()


def workspace_dir() -> Path:
    """Куда складывать ролики и базу.

    Приоритет:
    1. UNIQLY_WORKSPACE — явно заданная папка (тесты, перенос на другой диск);
    2. собранная программа → папка данных пользователя;
    3. запуск из исходников → workspace/ рядом с кодом (как было раньше —
       чтобы после обновления его 438 роликов остались на месте).
    """
    forced = os.getenv("UNIQLY_WORKSPACE")
    if forced:
        return Path(forced).expanduser()
    if _is_frozen():
        return _user_data_dir()
    return ROOT / "workspace"


# .env читаем ТОЛЬКО из папки данных, если запущены из сборки: внутри .app
# своего .env нет, а ROOT там указывает в случайное место (папку, откуда
# приложение стартовало) — иначе собранная программа подхватила бы чужой
# файл с ключами, оказавшийся рядом. Из исходников — как раньше, рядом с кодом.
if _is_frozen():
    # ключи, вшитые при сборке (КиноПоиск/TMDB) — чтобы поиск фильма работал
    # у друга без всякой настройки. личных ключей там нет, см.
    # make_bundled_env.py. Файла может не быть — тогда просто пропускаем.
    try:
        import bundled_env
        bundled_env.apply()
    except ImportError:
        pass
    # .env берём ИЗ РАБОЧЕЙ ПАПКИ, а не из фиксированной папки пользователя:
    # иначе UNIQLY_WORKSPACE (запуск с другими данными) не работает — файл
    # подхватывался из старого места и версия определялась неверно.
    load_dotenv(workspace_dir() / ".env")
else:
    load_dotenv(ROOT / ".env")

WORKSPACE = workspace_dir()
DOWNLOADS = WORKSPACE / "downloads"
OUTPUT = WORKSPACE / "output"
PREVIEWS = WORKSPACE / "previews"
DB_PATH = WORKSPACE / "app_state.db"

for _d in (DOWNLOADS, OUTPUT, PREVIEWS):
    _d.mkdir(parents=True, exist_ok=True)


def channel_output_dir(channel_id: str) -> Path:
    """Папка готовых роликов конкретного канала: <workspace>/output/<channel_id>/."""
    d = OUTPUT / channel_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def resource_path(relative: str) -> Path:
    """Путь к файлу, вшитому в программу (шрифты, вотермарк-образец).

    PyInstaller распаковывает такие файлы во временную папку и кладёт её путь
    в sys._MEIPASS. При запуске из исходников — просто путь от корня проекта.
    """
    base = Path(getattr(sys, "_MEIPASS", ROOT))
    return base / relative
