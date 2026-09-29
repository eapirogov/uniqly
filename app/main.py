"""Точка входа десктоп-приложения."""
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication

# ВАЖНО: здесь импортируется только Qt и модуль объяснения ошибок.
# `app.config` (а через него storage/channels/main_window) создаёт рабочие
# папки прямо при импорте — если туда нельзя писать, исключение вылетало бы
# на этой строке, ДО QApplication, и показать его было бы негде. Поэтому все
# импорты приложения ушли внутрь _start(), под перехват (см. startup_error).
from app import startup_error


def _activate_app():
    """Активирует приложение через AppKit. Вызывается уже внутри event loop —
    только тогда NSApp() существует и активация срабатывает. На macOS с обычным
    (не .app) запуском без этого окно есть, но не выходит вперёд и не в Dock."""
    try:
        from AppKit import NSApp, NSApplicationActivationPolicyRegular
        NSApp().setActivationPolicy_(NSApplicationActivationPolicyRegular)
        NSApp().activateIgnoringOtherApps_(True)
    except Exception:  # noqa: BLE001 — AppKit может быть недоступен, не критично
        pass


def _bring_to_front(window):
    """Поднимает окно на передний план."""
    window.setWindowState(
        (window.windowState() & ~Qt.WindowMinimized) | Qt.WindowActive
    )
    window.show()
    window.raise_()
    window.activateWindow()


def _start(app):
    """Всё, что может упасть по вине окружения: рабочая папка, база, каналы.

    Вынесено в отдельную функцию, чтобы main() мог перехватить сбой и показать
    окно с объяснением вместо тихого выхода с кодом 1.
    """
    from app import channels, storage
    from app.gui import theme
    from app.gui.main_window import MainWindow

    storage.backup_db()  # бэкап БД при старте (workspace/backups/, последние 20)

    theme.apply(app)
    window = MainWindow()
    _bring_to_front(window)
    # первый запуск: каналов нет — объясняем, что делать, и ведём в настройки.
    # После окна, но до event loop — иначе диалог откроется за главным окном.
    if not channels.CHANNELS:
        from app.gui.welcome import WelcomeDialog
        if WelcomeDialog(window).exec() == WelcomeDialog.Accepted:
            window.open_settings(new_channel=True)
    return window


def main():
    # QApplication создаём ПЕРВЫМ делом: без него нельзя показать даже окно с
    # ошибкой. Сам по себе он ничего не пишет на диск и упасть не может.
    app = QApplication(sys.argv)
    try:
        _start(app)
    except Exception as exc:  # noqa: BLE001 — показываем любую причину сбоя
        startup_error.show(exc)
        sys.exit(1)
    # активация через AppKit — уже после старта цикла (иначе NSApp() ещё None)
    QTimer.singleShot(0, _activate_app)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
