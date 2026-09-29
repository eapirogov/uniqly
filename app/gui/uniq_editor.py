"""Общий виджет «кроп + слайдеры уникализации» — переиспользуется в
app/gui/process_panel.py (одиночный ролик) и app/gui/batch_dialog.py
(пачечная загрузка), чтобы не дублировать самую рискованную часть UI дважды.

Кроп — embedded _CropView (app/gui/crop_editor.py) без модального QDialog.
Слайдеры — все 9 параметров processors/uniq.make_params(): contrast,
brightness, saturation, gamma, rotate, noise, speed, pitch, trim_start.

Превью после изменения СЛАЙДЕРА — debounce: любое движение перезапускает
QTimer на 600мс, по истечении без новых изменений запускается Worker с
pipeline.preview_frame_with_params. Если предыдущий debounce-рендер ещё не
закончился — новый тик выставляет self._preview_pending=True вместо
повторного запуска (не более одного ffmpeg-процесса превью одновременно на
виджет), по завершении текущего — сразу стартует следующий с АКТУАЛЬНЫМИ
на тот момент значениями слайдеров.

Кроп (рамка на кадре) — НЕ debounce и НЕ авто: двигается свободно без
рендера превью на каждое движение, применяется только по отдельной кнопке
«Обрезать» (решение пользователя — обрезка меняет letterbox/pad, дороже
дёргать на каждый чих рамки, плюс легче понять, когда именно применилось).

Клик по превью-кадру (как в CapCut) — рендерит честный ffmpeg-клип (первые 5с,
видео+звук, с текущими кроп+слайдерами) и открывает его в мини-плеере
(app/gui/preview_player.RenderedPlayerDialog), чтобы увидеть эффекты в
движении, а не по кадру. Живая (без рендера) QGraphicsVideoItem-имитация не
прижилась — на реальном macOS видео-слой рендерится отдельным GPU-composited
layer и игнорирует Qt scene-клиппинг (обрезка визуально не работала).
"""
from pathlib import Path

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QComboBox, QGridLayout, QHBoxLayout, QInputDialog, QLabel, QMessageBox,
    QPushButton, QSlider, QVBoxLayout, QWidget,
)

from app import pipeline
from app.gui import theme
from app.gui.crop_editor import _CropView, fit_frame, rect_to_crop
from app.gui.preview_player import RenderedPlayerDialog
from app.worker import Worker
from processors import uniq

DEBOUNCE_MS = 600

# (ключ, подпись, min, max, дефолт-диапазон как в uniq.make_params(), масштаб)
# слайдер работает в целых единицах (QSlider поддерживает только int) —
# value_real = slider_value / SCALE
_SLIDER_SPECS = [
    ("contrast", "Контраст", 0.90, 1.10, 1000),
    ("brightness", "Яркость", -0.10, 0.10, 1000),
    ("saturation", "Насыщенность", 0.80, 1.20, 1000),
    ("gamma", "Гамма", 0.80, 1.20, 1000),
    ("rotate", "Поворот (°)", -3.0, 3.0, 100),
    ("noise", "Зерно", 0, 30, 1),
    ("speed", "Скорость", 0.90, 1.05, 1000),
    ("pitch", "Тон аудио", 0.95, 1.05, 10000),
    ("trim_start", "Обрезка старта (с)", 0.0, 2.0, 100),
]


class UniqEditorWidget(QWidget):
    """Кроп (embedded) + 9 слайдеров + debounce-превью. on_change() зовётся
    при любом изменении (для внешнего кода, если нужно, напр. пометить
    несохранённые правки) — необязательный колбэк."""

    def __init__(self, on_change=None, compact: bool = False,
                 channel_id: str | None = None):
        """compact — превью поменьше: в пачечной загрузке редактор живёт в
        диалоге, и полноразмерное превью занимало почти всё окно, ползунки
        оказывались далеко внизу.

        channel_id — чтобы превью рисовало фон полей так же, как готовый
        ролик (у канала он может быть размытым, а не чёрным)."""
        super().__init__()
        self.on_change = on_change
        self.channel_id = channel_id
        self._workers = []  # держим ссылки — см. паттерн против SIGSEGV
        # (тот же приём в app/gui/gallery.py)
        self._preview_pending = False
        self._preview_running = False
        self._src: Path | None = None
        self._frame_w = 0
        self._frame_h = 0
        self._frame_pix = None
        self._crop: tuple[int, int, int, int] | None = None
        self._crop_confident = True
        self._scale = 1.0
        self._sliders: dict[str, QSlider] = {}
        self._value_labels: dict[str, QLabel] = {}

        root = QVBoxLayout(self)

        # --- превью по центру + компактная кнопка обрезки ---
        # Превью крупное и по центру: на него смотрят постоянно, оценивая
        # эффект ползунков. Кадр с рамкой обрезки открывается отдельным окном
        # по кнопке — он нужен один раз на ролик.
        head = QLabel("Превью — клик, чтобы проиграть ▶")
        head.setAlignment(Qt.AlignCenter)
        head.setStyleSheet(f"color:{theme.TEXT_MUTED};font-size:11px;")
        root.addWidget(head)

        self.preview_label = QLabel("нет превью")
        self.preview_label.setAlignment(Qt.AlignCenter)
        # 9:16, как готовый ролик; в компактном режиме — вдвое меньше
        self.preview_label.setFixedSize(*((170, 302) if compact else (300, 533)))
        self.preview_label.setCursor(Qt.PointingHandCursor)
        self.preview_label.setStyleSheet(
            f"background:{theme.SURFACE_RAISED};color:{theme.TEXT_MUTED};"
            f"border-radius:{theme.RADIUS}px;"
        )
        self.preview_label.mousePressEvent = self._on_preview_clicked
        prev_row = QHBoxLayout()
        prev_row.addStretch(1)
        prev_row.addWidget(self.preview_label)
        prev_row.addStretch(1)
        root.addLayout(prev_row)

        self.preview_status = QLabel("")
        self.preview_status.setAlignment(Qt.AlignCenter)
        self.preview_status.setStyleSheet(f"color:{theme.TEXT_MUTED};font-size:11px;")
        root.addWidget(self.preview_status)

        crop_row = QHBoxLayout()
        crop_row.addStretch(1)
        self._crop_btn = QPushButton("✂️ Обрезать…")
        self._crop_btn.setToolTip("Открыть окно обрезки: подвинуть рамку на кадре")
        self._crop_btn.clicked.connect(self._open_crop_dialog)
        self._crop_btn.setEnabled(False)
        self._crop_btn.setFixedWidth(150)
        crop_row.addWidget(self._crop_btn)
        self._crop_info = QLabel("Кадр ещё не загружен")
        self._crop_info.setStyleSheet(f"color:{theme.TEXT_MUTED};font-size:11px;")
        crop_row.addWidget(self._crop_info)
        crop_row.addStretch(1)
        root.addLayout(crop_row)

        # --- слайдеры ---
        grid = QGridLayout()
        for row, (key, label, lo, hi, scale) in enumerate(_SLIDER_SPECS):
            grid.addWidget(QLabel(label), row, 0)
            slider = QSlider(Qt.Horizontal)
            slider.setMinimum(round(lo * scale))
            slider.setMaximum(round(hi * scale))
            # двухпальцевый скролл на трекпаде Mac шлёт wheelEvent — без
            # этого он попадал на слайдер под курсором мыши при скролле
            # страницы вниз и незаметно сдвигал значение (пользователь
            # промотал панель, а не крутил ползунок). Слайдер должен
            # реагировать ТОЛЬКО на клик/перетаскивание, не на скролл —
            # пробрасываем wheel-событие наверх, чтобы скроллилась страница.
            slider.wheelEvent = lambda event: event.ignore()
            slider.valueChanged.connect(
                lambda _v, k=key: self._on_slider_changed(k)
            )
            grid.addWidget(slider, row, 1)
            value_label = QLabel("")
            value_label.setMinimumWidth(60)
            grid.addWidget(value_label, row, 2)
            self._sliders[key] = slider
            self._value_labels[key] = value_label
        root.addLayout(grid)

        # --- свои пресеты: выставил ползунки один раз → применяешь дальше ---
        preset_row = QHBoxLayout()
        preset_row.addWidget(QLabel("Мой пресет:"))
        self.preset_combo = QComboBox()
        self.preset_combo.setMinimumWidth(160)
        self.preset_combo.activated.connect(self._apply_preset)
        preset_row.addWidget(self.preset_combo, 1)
        save_btn = QPushButton("Сохранить")
        save_btn.setToolTip("Запомнить текущее положение ползунков под именем")
        save_btn.clicked.connect(self._save_preset)
        preset_row.addWidget(save_btn)
        del_btn = QPushButton("Удалить")
        del_btn.setToolTip("Убрать выбранный пресет из списка")
        del_btn.clicked.connect(self._delete_preset)
        preset_row.addWidget(del_btn)
        root.addLayout(preset_row)
        self._reload_presets()

        btn_row = QHBoxLayout()
        reset_btn = QPushButton("Сбросить")
        reset_btn.setToolTip("Вернуть ползунки в нейтраль — ролик без изменений")
        reset_btn.clicked.connect(self.reset_params)
        btn_row.addWidget(reset_btn)
        randomize_btn = QPushButton("🎲 Случайные настройки")
        randomize_btn.setToolTip(
            "Заново раскидать ползунки — каждый ролик получает свой набор,\n"
            "чтобы площадка не посчитала его копией")
        randomize_btn.clicked.connect(self.randomize_params)
        btn_row.addWidget(randomize_btn)
        btn_row.addStretch(1)
        root.addLayout(btn_row)

        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.timeout.connect(self._trigger_preview)

    # --- публичный API ---

    def _background(self) -> tuple[str, int]:
        """Чем канал заполняет поля по краям: ("black"|"blur", сила размытия).

        Без канала (или если его удалили) — чёрные поля: так вело себя превью
        всегда, это безопасное значение по умолчанию.
        """
        if not self.channel_id:
            return "black", 0
        try:
            from app import channels
            ch = channels.by_id(self.channel_id)
        except KeyError:
            return "black", 0
        return getattr(ch, "background", "black"), getattr(ch, "blur_strength", 0)

    def set_frame(self, src: Path, pix: QPixmap, frame_w: int, frame_h: int,
                  initial_crop: tuple[int, int, int, int] | None,
                  confident: bool = True):
        """Инициализирует embedded-кроп по кадру исходника. Вызывается один
        раз после скачивания (или при load_from_history)."""
        self._src = Path(src)
        self._frame_w = frame_w
        self._frame_h = frame_h
        self._frame_pix = pix
        # рамка живёт как числа (w,h,x,y), а не как виджет: окно обрезки
        # строится по ней при открытии и возвращает новую
        self._crop = initial_crop or (frame_w, frame_h, 0, 0)
        self._crop_confident = confident
        self._crop_btn.setEnabled(True)
        self._update_crop_info()
        self._schedule_preview()

    def _update_crop_info(self):
        """Подпись под кнопкой: что сейчас за рамка."""
        w, h, x, y = self._crop
        full = (w, h) == (self._frame_w, self._frame_h)
        if full:
            text = "Обрезка не задана — берётся весь кадр"
        else:
            text = f"Обрезка: {w}×{h}, отступ {x}:{y}"
        if not self._crop_confident:
            text += " · проверьте рамку"
        self._crop_info.setText(text)

    def reset_crop(self, crop_rect: tuple[int, int, int, int] | None,
                    confident: bool = True):
        """Переставляет рамку обрезки (напр. при смене канала). Кадр уже
        загружен — окно обрезки построится по новым числам при открытии."""
        if not self._frame_w:
            return
        self._crop = crop_rect or (self._frame_w, self._frame_h, 0, 0)
        self._crop_confident = confident
        self._update_crop_info()
        if self.on_change:
            self.on_change()

    def set_params(self, params: dict):
        """Программно выставляет значения слайдеров (не триггерит debounce
        цепочкой — обновляем один раз и планируем ОДИН превью в конце)."""
        for key, _label, _lo, _hi, scale in _SLIDER_SPECS:
            slider = self._sliders[key]
            slider.blockSignals(True)
            slider.setValue(round(params[key] * scale))
            slider.blockSignals(False)
            self._update_value_label(key)
        self._schedule_preview()

    def randomize_params(self):
        """Кнопка «Случайные настройки» — новый случайный набор (как раньше
        генерировался автоматически внутри processors/*.uniqueize())."""
        self.set_params(uniq.make_params())

    def reset_params(self):
        """Кнопка «Сбросить» — ползунки в нейтраль, ролик без изменений."""
        self.set_params(uniq.neutral_params())

    def apply_default_preset(self):
        """Пресет по умолчанию из настроек (вкладка «Настройки» → «Пресет
        уникализации для всех роликов»). Не задан или удалён — нейтраль."""
        from app import storage
        name = storage.kv_get("default_uniq_preset", "")
        params = None
        if name:
            params = next((r["params"] for r in storage.uniq_presets()
                           if r["name"] == name), None)
        self.set_params(params or uniq.neutral_params())
        # показываем в списке, какой набор применён
        idx = self.preset_combo.findText(name) if name and params else 0
        self.preset_combo.setCurrentIndex(idx if idx >= 0 else 0)

    # --- свои пресеты ---

    def _reload_presets(self, select: str = ""):
        """Перечитывает список пресетов из базы. select — какой показать
        выбранным после перезагрузки (только что сохранённый)."""
        from app import storage
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        self.preset_combo.addItem("— не выбран —", None)
        for row in storage.uniq_presets():
            self.preset_combo.addItem(row["name"], row["params"])
        if select:
            idx = self.preset_combo.findText(select)
            if idx >= 0:
                self.preset_combo.setCurrentIndex(idx)
        self.preset_combo.blockSignals(False)

    def _apply_preset(self, index: int):
        params = self.preset_combo.itemData(index)
        if params:
            self.set_params(params)

    def _save_preset(self):
        from app import storage
        name, ok = QInputDialog.getText(
            self, "Сохранить пресет",
            "Название набора (существующий с тем же именем перезапишется):")
        name = (name or "").strip()
        if not ok or not name:
            return
        storage.uniq_preset_save(name, self.current_params())
        self._reload_presets(select=name)

    def _delete_preset(self):
        from app import storage
        name = self.preset_combo.currentText()
        if self.preset_combo.currentData() is None:
            QMessageBox.information(self, "Пресет не выбран",
                                    "Сначала выберите пресет в списке.")
            return
        if QMessageBox.question(
                self, "Удалить пресет?",
                f"Удалить «{name}»? Ползунки останутся как есть."
        ) != QMessageBox.Yes:
            return
        storage.uniq_preset_delete(name)
        self._reload_presets()

    def current_params(self) -> dict:
        """Собирает текущие значения слайдеров в dict формата
        uniq.make_params() — контракт для pipeline.render_with_params()."""
        result = {}
        for key, _label, _lo, _hi, scale in _SLIDER_SPECS:
            value = self._sliders[key].value() / scale
            result[key] = int(value) if key == "noise" else round(value, 4)
        return result

    def current_crop(self) -> tuple[int, int, int, int] | None:
        if not self._frame_w:
            return None
        return self._crop

    # --- внутреннее ---

    def _update_value_label(self, key: str):
        _key, _label, _lo, _hi, scale = next(s for s in _SLIDER_SPECS if s[0] == key)
        value = self._sliders[key].value() / scale
        text = f"{int(value)}" if key == "noise" else f"{value:.3f}"
        self._value_labels[key].setText(text)

    def _on_slider_changed(self, key: str):
        self._update_value_label(key)
        self._schedule_preview()
        if self.on_change:
            self.on_change()

    def _open_crop_dialog(self):
        """Открывает окно обрезки. Подтвердил — новая рамка и сразу превью."""
        if not self._src or self._frame_pix is None:
            return
        from app.gui.crop_editor import CropEditorDialog

        dlg = CropEditorDialog(
            self._src, self._frame_w, self._frame_h,
            initial_crop=self._crop, confident=self._crop_confident,
            parent=self, pixmap=self._frame_pix,
        )
        if dlg.exec() != CropEditorDialog.Accepted:
            return
        if dlg.result_crop:
            self._crop = dlg.result_crop
            self._crop_confident = True
            self._update_crop_info()
            if self.on_change:
                self.on_change()
        # превью сразу, без debounce — это явное однократное действие
        self._preview_timer.stop()
        self._trigger_preview()

    def _schedule_preview(self):
        """Перезапускает debounce-таймер (см. модульный docstring) — только
        для слайдеров, кроп сюда не ведёт (см. _on_crop_changed)."""
        if not self._src:
            return
        self._preview_timer.start(DEBOUNCE_MS)

    def _pix_with_play_hint(self, pix: QPixmap) -> QPixmap:
        """Кадр, вписанный в preview_label, с полупрозрачным кружком ▶
        поверх центра — визуальная подсказка, что по превью можно кликнуть
        и проиграть клип с эффектами (как постер видео в CapCut/YouTube)."""
        from PySide6.QtGui import QColor, QPainter, QFont

        scaled = pix.scaled(self.preview_label.size(), Qt.KeepAspectRatio,
                             Qt.SmoothTransformation)
        result = QPixmap(scaled)
        painter = QPainter(result)
        painter.setRenderHint(QPainter.Antialiasing)
        r = 26
        cx, cy = result.width() // 2, result.height() // 2
        painter.setBrush(QColor(0, 0, 0, 140))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(cx - r, cy - r, r * 2, r * 2)
        painter.setBrush(QColor(255, 255, 255, 230))
        triangle = [
            (cx - r // 3, cy - r // 2), (cx - r // 3, cy + r // 2), (cx + r // 2, cy),
        ]
        from PySide6.QtCore import QPoint
        from PySide6.QtGui import QPolygon
        painter.drawPolygon(QPolygon([QPoint(x, y) for x, y in triangle]))
        painter.end()
        return result

    def _on_preview_clicked(self, _event):
        """Клик по превью — рендерит первые 5с ролика (видео+звук) с текущими
        кроп+слайдерами через честный ffmpeg-фильтр (см.
        pipeline.preview_clip_with_params) и открывает мини-плеер
        (app/gui/preview_player.RenderedPlayerDialog).

        Пробовали live-имитацию через QGraphicsVideoItem без рендера в файл —
        на реальном экране macOS видео-слой композитится отдельным GPU-native
        layer поверх Qt-сцены и игнорирует scene-клиппинг, из-за чего "обрезка"
        визуально не работала при живом тестировании (хотя offscreen-рендер
        сцены был корректным) — ffmpeg-рендер даёт то же самое изображение,
        что и финальный результат, без сюрпризов платформы.
        """
        if not self._src or self._preview_running:
            return
        self.preview_status.setText("Рендерю превью-клип…")
        crop_rect = self.current_crop()
        params = self.current_params()
        src = self._src

        bg, blur = self._background()
        worker = Worker(
            lambda _progress: pipeline.preview_clip_with_params(
                src, crop_rect, params, background=bg, blur=blur)
        )
        self._workers.append(worker)

        def _cleanup(*_):
            if worker in self._workers:
                self._workers.remove(worker)

        def _on_done(path):
            self.preview_status.setText("")
            _cleanup()
            dlg = RenderedPlayerDialog(path, parent=self)
            dlg.exec()

        def _on_failed(err):
            self.preview_status.setText(f"клип: {err}")
            _cleanup()

        worker.signals.done.connect(_on_done)
        worker.signals.failed.connect(_on_failed)
        from PySide6.QtCore import QThreadPool
        QThreadPool.globalInstance().start(worker)

    def _trigger_preview(self):
        if self._preview_running:
            # рендер уже идёт — не плодим второй, отметим что нужно повторить
            # по завершении текущего с актуальными значениями
            self._preview_pending = True
            return
        self._render_preview()

    def _render_preview(self):
        self._preview_running = True
        self._preview_pending = False
        self.preview_status.setText("Обновляю превью…")
        crop_rect = self.current_crop()
        params = self.current_params()
        src = self._src
        # Метка запуска: превью всех рендеров пишутся в ОДИН файл
        # ({stem}_editpreview.jpg), поэтому пришедший позже результат мог
        # оказаться от предыдущей рамки — картинка не совпадала с текущей
        # обрезкой («съезжали границы»). Показываем только результат самого
        # свежего запуска, опоздавшие отбрасываем.
        self._preview_seq = getattr(self, "_preview_seq", 0) + 1
        seq = self._preview_seq

        bg, blur = self._background()
        worker = Worker(
            lambda _progress: pipeline.preview_frame_with_params(
                src, crop_rect, params, background=bg, blur=blur)
        )
        self._workers.append(worker)

        def _cleanup(*_):
            if worker in self._workers:
                self._workers.remove(worker)
            self._preview_running = False
            if self._preview_pending:
                # значения могли измениться, пока рендерился прошлый кадр —
                # сразу стартуем следующий с актуальными на этот момент
                self._render_preview()

        def _on_done(path):
            if seq != self._preview_seq:
                _cleanup()       # опоздавший кадр от прежней рамки — игнор
                return
            pix = QPixmap(str(path))
            if not pix.isNull():
                self._preview_pix = pix  # источник для оверлея ▶ при ресайзе
                self.preview_label.setPixmap(self._pix_with_play_hint(pix))
            self.preview_status.setText("")
            _cleanup()

        def _on_failed(err):
            self.preview_status.setText(f"превью: {err}")
            _cleanup()

        worker.signals.done.connect(_on_done)
        worker.signals.failed.connect(_on_failed)
        from PySide6.QtCore import QThreadPool
        QThreadPool.globalInstance().start(worker)
