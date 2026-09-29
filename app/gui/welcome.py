"""Приветствие при первом запуске: у нового пользователя нет ни одного канала.

Без него программа открывалась бы с пустым списком каналов и человек не понял
бы, что делать дальше: поле ссылки есть, а постить некуда.
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
)

from app.gui import theme


class WelcomeDialog(QDialog):
    """Окно первого запуска. exec() возвращает Accepted, если человек нажал
    «Создать канал» — вызывающий код открывает вкладку настроек."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Добро пожаловать")
        self.setMinimumWidth(460)
        root = QVBoxLayout(self)
        root.setSpacing(14)

        title = QLabel("<b>Uniqly</b>")
        title.setStyleSheet("font-size:22px;")
        title.setAlignment(Qt.AlignCenter)
        root.addWidget(title)

        sub = QLabel("Уникализация роликов для Reels, Shorts и TikTok")
        sub.setAlignment(Qt.AlignCenter)
        sub.setStyleSheet(f"color:{theme.TEXT_MUTED};")
        root.addWidget(sub)

        steps = QLabel(
            "<p>Как это работает:</p>"
            "<ol>"
            "<li><b>Заведите канал</b> — куда вы постите ролики. "
            "Выберите способ обработки и откуда брать видео.</li>"
            "<li><b>Вставьте ссылку</b> на ролик с YouTube или Instagram "
            "на вкладке «Работа».</li>"
            "<li><b>Настройте обрезку</b> и ползунки уникализации, "
            "нажмите «Применить».</li>"
            "<li>Готовый ролик появится во вкладке <b>«Мои ролики»</b>.</li>"
            "</ol>"
        )
        steps.setWordWrap(True)
        steps.setTextFormat(Qt.RichText)
        root.addWidget(steps)

        note = QLabel(
            "Для роликов из Instagram нужно быть залогиненным в Instagram "
            "в браузере — программа берёт оттуда куки (браузер выбирается "
            "во вкладке «Настройки»)."
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{theme.TEXT_MUTED};font-size:11px;")
        root.addWidget(note)

        btns = QHBoxLayout()
        btns.addStretch()
        later = QPushButton("Позже")
        later.clicked.connect(self.reject)
        create = QPushButton("Создать канал")
        create.setObjectName("primary")
        create.setDefault(True)
        create.clicked.connect(self.accept)
        btns.addWidget(later)
        btns.addWidget(create)
        root.addLayout(btns)
