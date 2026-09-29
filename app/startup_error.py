"""Понятное окно вместо молчаливой смерти при старте.

Раньше любая ошибка на старте (нет прав на рабочую папку, диск отключён,
битая база) вылетала ДО создания окна: программа завершалась с кодом 1, а
текст уходил в консоль. У собранного .app консоли нет — пользователь видел
только, как иконка дёрнулась в доке и погасла, и мог сказать лишь «не запускается».

Здесь ошибка переводится на человеческий язык и показывается окном, а полный
текст пишется в лог рядом с данными — его можно переслать разработчику.
"""
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path


def log_path() -> Path:
    """Куда писать лог сбоя.

    Пробуем рабочую папку (рядом с данными — пользователю проще найти), но
    именно она может быть недоступна — тогда падаем на домашнюю папку, а в
    крайнем случае на временную. Функция не должна бросать исключений: её
    зовут в момент, когда уже что-то сломалось.
    """
    candidates = []
    forced = os.getenv("UNIQLY_WORKSPACE")
    if forced:
        candidates.append(Path(forced).expanduser())
    try:
        from app import config
        candidates.append(config.workspace_dir())
    except Exception:  # noqa: BLE001 — config мог и не загрузиться, это норма
        pass
    candidates.append(Path.home() / "Uniqly")
    candidates.append(Path(os.getenv("TMPDIR") or "/tmp"))

    for base in candidates:
        try:
            base.mkdir(parents=True, exist_ok=True)
            path = base / "uniqly-error.log"
            with open(path, "a", encoding="utf-8"):
                pass          # проверяем, что писать реально можно
            return path
        except OSError:
            continue
    return Path("/tmp/uniqly-error.log")


def write_log(exc: BaseException) -> Path | None:
    """Записывает полный traceback в лог. Возвращает путь или None."""
    try:
        path = log_path()
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n{'=' * 60}\n{datetime.now():%Y-%m-%d %H:%M:%S}\n")
            f.write("".join(traceback.format_exception(
                type(exc), exc, exc.__traceback__)))
        return path
    except OSError:
        return None


def explain(exc: BaseException) -> tuple[str, str]:
    """Переводит исключение в (заголовок, что делать) человеческим языком.

    Разбор по типу и тексту ошибки, а не по коду: одна и та же PermissionError
    приходит и от антивируса, и от облака, выгрузившего папку.
    """
    text = str(exc)
    # Путь подставляется в предложения разной формы («создать папки в X»,
    # «открыть X»), поэтому запасной вариант — существительное в именительном
    # падеже, а не «в папке с данными»: иначе выходило «открыть папке с данными».
    workspace = os.getenv("UNIQLY_WORKSPACE")
    if not workspace:
        try:
            from app import config
            workspace = str(config.workspace_dir())
        except Exception:  # noqa: BLE001
            workspace = "папка с данными"

    if isinstance(exc, PermissionError):
        return (
            "Нет доступа к папке с данными",
            f"Программа не может создать свои папки здесь:\n{workspace}\n\n"
            "Обычно это значит одно из:\n"
            "• папка лежит на внешнем диске, а он отключён;\n"
            "• папку заблокировал антивирус или настройки системы;\n"
            "• у этой учётной записи нет прав на запись.\n\n"
            "Подключите диск или разрешите доступ к папке и запустите снова."
        )
    if isinstance(exc, FileNotFoundError):
        return (
            "Папка с данными не найдена",
            f"Не удалось открыть эту папку:\n{workspace}\n\n"
            "Скорее всего, папку переместили, переименовали или она была на "
            "диске, который сейчас отключён.\n\n"
            "Подключите диск или верните папку на место."
        )
    if isinstance(exc, OSError) and ("No space" in text or "Errno 28" in text):
        return (
            "На диске нет свободного места",
            "Программе некуда сохранять ролики и базу.\n\n"
            "Освободите место на диске и запустите снова."
        )
    if "database" in text.lower() or "sqlite" in type(exc).__name__.lower():
        return (
            "Не удалось открыть базу данных",
            "Файл app_state.db повреждён или недоступен.\n\n"
            "Копии базы лежат в папке backups рядом с данными — можно "
            "заменить повреждённый файл последней копией.\n\n"
            f"Подробности: {text[:200]}"
        )
    return (
        "Программа не смогла запуститься",
        f"Произошла ошибка: {type(exc).__name__}\n{text[:300]}"
    )


def show(exc: BaseException) -> None:
    """Показывает окно с объяснением. Работает даже если Qt ещё не поднят.

    Если и окно показать не удалось (нет дисплея, Qt не грузится) — печатаем в
    консоль: хуже молчания не будет.
    """
    log = write_log(exc)
    title, body = explain(exc)
    if log:
        body += f"\n\nПодробный отчёт об ошибке: {log}"

    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
        app = QApplication.instance() or QApplication(sys.argv)
        box = QMessageBox()
        box.setIcon(QMessageBox.Critical)
        box.setWindowTitle("Uniqly")
        box.setText(title)
        box.setInformativeText(body)
        box.exec()
    except Exception:  # noqa: BLE001 — последний рубеж, падать здесь нельзя
        print(f"{title}\n\n{body}", file=sys.stderr)
