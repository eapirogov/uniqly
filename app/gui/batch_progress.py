"""Окно прогресса пачечной уникализации.

Показывается по центру после «Применить ко всем»: пользователь настроил
параметры на эталонном ролике — дальше всё идёт само, а здесь видно, сколько
роликов готово и что происходит сейчас.

Полоса — «сегментная», как в старом загрузчике Counter-Strike (об этом просил
пользователь): не сплошная заливка, а ряд одинаковых прямоугольников, которые
загораются слева направо. Рисуется вручную в paintEvent, потому что QSS такого
не умеет — QProgressBar::chunk даёт сплошную полосу.
"""
from PySide6.QtCore import Qt, QRect, QTimer
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

from app.gui import theme

# Размеры сегмента полосы. Ширина с зазором подобраны так, чтобы на типичной
# ширине окна (~460 px) сегментов было около полусотни — как в оригинале.
_SEG_W = 7
_SEG_GAP = 2
_BAR_H = 22


class SegmentedBar(QWidget):
    """Полоса прогресса из отдельных сегментов (стиль загрузчика CS)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._value = 0.0            # 0.0 … 1.0
        self.setFixedHeight(_BAR_H)

    def set_value(self, value: float):
        self._value = max(0.0, min(1.0, value))
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setPen(Qt.NoPen)

        # подложка — утопленный жёлоб
        painter.setBrush(QColor(theme.SURFACE))
        painter.drawRoundedRect(self.rect(), 3, 3)

        step = _SEG_W + _SEG_GAP
        total = max(1, (self.width() - _SEG_GAP) // step)
        lit = int(round(total * self._value))
        # сначала все сегменты тусклыми — видно, сколько всего осталось,
        # как в оригинальном загрузчике; поверх — загоревшиеся
        dim = QColor(theme.BG)
        painter.setBrush(dim)
        for i in range(total):
            painter.drawRect(QRect(_SEG_GAP + i * step, 4,
                                    _SEG_W, self.height() - 8))
        painter.setBrush(QColor(theme.SUCCESS))
        for i in range(lit):
            painter.drawRect(QRect(_SEG_GAP + i * step, 4,
                                    _SEG_W, self.height() - 8))
        painter.end()


class BatchProgressDialog(QDialog):
    """Ход пачечной уникализации: общая полоса + строка текущего ролика.

    Когда всё готово без ошибок — окно закрывается само через пару секунд:
    держать его открытым незачем, ролики уже в галерее. Если были ошибки,
    остаётся на экране, чтобы пользователь их увидел.
    """

    def __init__(self, total: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Уникализация")
        self.setModal(False)     # не блокируем окно: панели видны за диалогом
        self.total = total
        self.done_count = 0
        self.failed_count = 0
        self.failures: list[tuple[str, str]] = []   # (ссылка, текст ошибки)
        self._stages: dict[str, str] = {}           # ссылка → текущий этап
        self._finished: set[str] = set()            # уже отчитавшиеся ссылки

        self.setStyleSheet(f"QDialog {{ background: {theme.BG}; }}")
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(10)

        self.title = QLabel(f"Уникализация роликов: 0 из {total}")
        self.title.setStyleSheet(
            f"color:{theme.TEXT};font-size:14px;font-weight:600;")
        root.addWidget(self.title)

        self.bar = SegmentedBar()
        root.addWidget(self.bar)

        self.current = QLabel("Подготовка…")
        self.current.setStyleSheet(f"color:{theme.TEXT_MUTED};font-size:12px;")
        self.current.setWordWrap(True)
        root.addWidget(self.current)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.close_btn = QPushButton("Свернуть")
        self.close_btn.setToolTip(
            "Закрыть окно — уникализация продолжится в фоне,\n"
            "ход видно на карточках роликов")
        self.close_btn.clicked.connect(self.accept)
        btn_row.addWidget(self.close_btn)
        root.addLayout(btn_row)

        # ширина фиксирована, а высота — по содержимому: список ошибок в
        # конце может занять несколько строк
        self.setFixedWidth(460)
        self.current.setMinimumWidth(460 - 40)   # минус поля макета

    # --- вызывается из main_window по мере готовности роликов ---

    def set_current(self, text: str):
        """Строка «что сейчас происходит»."""
        self.current.setText(text)

    def set_stage(self, url: str, text: str):
        """Ход одного ролика. Ролики качаются одновременно, поэтому строка
        показывает не «ролик 3 из 10», а сколько всего в работе — иначе
        сообщения от разных ссылок затирали бы друг друга по очереди.
        """
        self._stages[url] = text
        # уже готовые не показываем — они ушли в счётчик
        active = [t for u, t in self._stages.items() if u not in self._finished]
        if not active:
            return
        left = self.total - self.done_count
        # если все заняты одним и тем же этапом — пишем его словами,
        # иначе просто сколько роликов в работе
        same = set(active)
        if len(same) == 1:
            self.current.setText(f"{active[0]}  (осталось роликов: {left})")
        else:
            self.current.setText(f"В работе роликов: {len(active)} "
                                 f"(осталось: {left})")

    def mark_done(self, ok: bool = True, url: str = "", error: str = ""):
        """Один ролик закончил обработку (ok=False — с ошибкой).

        url/error — что именно упало и почему: без них в окне была только
        цифра «с ошибкой — 2», и понять, какие ссылки не прошли, было нельзя.
        """
        self.done_count += 1
        self._finished.add(url)
        if not ok:
            self.failed_count += 1
            self.failures.append((url, error))
        self.bar.set_value(self.done_count / max(1, self.total))
        self.title.setText(
            f"Уникализация роликов: {self.done_count} из {self.total}")
        if self.done_count >= self.total:
            self._finish()

    def _finish(self):
        if self.failed_count:
            self.title.setText(
                f"Готово: {self.total - self.failed_count} из {self.total}, "
                f"с ошибкой — {self.failed_count}")
            self.title.setStyleSheet(
                f"color:{theme.DANGER};font-size:14px;font-weight:600;")
            # перечисляем, что именно не прошло: одной цифры мало, чтобы
            # понять, какую ссылку чинить
            lines = []
            for url, err in self.failures[:5]:
                short = url if len(url) <= 46 else url[:43] + "…"
                lines.append(f"• {short}\n    {err}" if err else f"• {short}")
            if len(self.failures) > 5:
                lines.append(f"…и ещё {len(self.failures) - 5}")
            lines.append("\nЭти ролики остались на вкладке «Работа» — "
                         "там есть кнопка «Повторить».")
            self.current.setText("\n".join(lines))
            self.close_btn.setText("Закрыть")
            # окно выросло по содержимому: список ошибок длиннее одной строки,
            # при фиксированной высоте он обрезался сверху и снизу
            # heightForWidth: QLabel с переносом считает высоту по КОНКРЕТНОЙ
            # ширине; sizeHint() брал её из ещё не устоявшегося макета и давал
            # заниженное число — текст обрезался сверху и снизу
            need = self.current.heightForWidth(self.current.width())
            self.current.setMinimumHeight(max(need, 0))
            self.adjustSize()
            return
        self.title.setText(f"Готово: все {self.total} роликов")
        self.current.setText("Ролики появились во вкладке «Мои ролики».")
        self.close_btn.setText("Закрыть")
        # всё прошло успешно — показываем итог пару секунд и убираем окно
        QTimer.singleShot(2000, self.accept)
