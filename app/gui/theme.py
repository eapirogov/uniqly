"""Единая тёмная тема приложения: палитра + глобальный QSS.

Раньше цвета были раскиданы по setStyleSheet() в каждом виджете (#4a90d9,
#5cb85c, #d9534f, #e08a1e, #888, #222 — вручную, без единого источника).
Здесь та же палитра сведена в constants + один QSS, применяемый глобально
через app.setStyleSheet() в app/main.py. Старые точечные setStyleSheet()
в существующих вкладках не трогаем — переносим на тему постепенно, по одной
вкладке, отдельными правками (см. CLAUDE.md/roadmap редизайна).
"""

# ── палитра ──────────────────────────────────────────────────────────────
# Фоны (тёмная тема, насыщенная — в духе DaVinci/Adobe: слои поверхностей
# с лёгким контрастом, а не плоский чёрный).
BG = "#1b1d22"           # фон окна
SURFACE = "#24262e"      # карточки, панели
SURFACE_RAISED = "#2c2f38"  # приподнятые элементы (поля ввода, кнопки)
BORDER = "#383b45"       # тонкие разделители/обводки

TEXT = "#e8e9ec"         # основной текст
TEXT_MUTED = "#8b8f9c"   # вторичный текст (было #888)

# Акцентный и семантические — те же оттенки, что уже прижились в коде,
# просто формализованы в одном месте.
ACCENT = "#4a90d9"       # инфо/в процессе (было #4a90d9)
ACCENT_HOVER = "#5b9fe3"
SUCCESS = "#5cb85c"      # готово/успех (было #5cb85c)
DANGER = "#d9534f"       # ошибка (было #d9534f)
WARNING = "#e08a1e"      # хук/акцент-предупреждение (было #e08a1e)

RADIUS = 8               # базовый радиус скругления карточек/кнопок
RADIUS_SM = 5


def build_qss() -> str:
    """Глобальный QSS, применяется один раз при старте приложения."""
    return f"""
    QMainWindow, QDialog {{
        background: {BG};
    }}
    QWidget {{
        color: {TEXT};
        font-size: 13px;
    }}
    QLabel {{
        background: transparent;
    }}

    QTabWidget::pane {{
        border: 1px solid {BORDER};
        border-radius: {RADIUS}px;
        background: {BG};
        top: -1px;
    }}
    QTabBar::tab {{
        background: transparent;
        color: {TEXT_MUTED};
        padding: 7px 16px;
        margin-right: 2px;
        border-top-left-radius: {RADIUS_SM}px;
        border-top-right-radius: {RADIUS_SM}px;
    }}
    QTabBar::tab:selected {{
        background: {SURFACE};
        color: {TEXT};
        font-weight: 600;
    }}
    QTabBar::tab:hover:!selected {{
        color: {TEXT};
    }}

    QFrame {{
        background: {SURFACE};
        border-radius: {RADIUS}px;
    }}

    QPushButton {{
        background: {SURFACE_RAISED};
        color: {TEXT};
        border: 1px solid {BORDER};
        border-radius: {RADIUS_SM}px;
        padding: 6px 12px;
    }}
    QPushButton:hover {{
        background: {BORDER};
    }}
    QPushButton:pressed {{
        background: {BG};
    }}
    QPushButton:disabled {{
        color: {TEXT_MUTED};
        background: {SURFACE};
    }}

    QPushButton#primary {{
        background: {ACCENT};
        border: 1px solid {ACCENT};
        color: white;
        font-weight: 600;
    }}
    QPushButton#primary:hover {{
        background: {ACCENT_HOVER};
    }}

    QLineEdit, QPlainTextEdit, QComboBox {{
        background: {SURFACE_RAISED};
        color: {TEXT};
        border: 1px solid {BORDER};
        border-radius: {RADIUS_SM}px;
        padding: 5px 8px;
        selection-background-color: {ACCENT};
    }}
    QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus {{
        border: 1px solid {ACCENT};
    }}

    QScrollArea {{
        border: none;
        background: transparent;
    }}
    QScrollBar:vertical {{
        background: transparent;
        width: 10px;
    }}
    QScrollBar::handle:vertical {{
        background: {BORDER};
        border-radius: 5px;
        min-height: 24px;
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0px;
    }}

    QMessageBox {{
        background: {SURFACE};
    }}
    """


def apply(app) -> None:
    """Применяет тему ко всему приложению. Вызывать один раз при старте."""
    app.setStyleSheet(build_qss())
