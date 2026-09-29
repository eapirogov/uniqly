"""Окно ручной разметки кропа: кадр видео + перетаскиваемая рамка (как в CapCut).

Показывается перед уникализацией (см. app/gui/process_panel.py). Стартовая рамка —
из автодетекта чёрных полос (processors/cropdetect.py) или готового дефолта
(прежняя константа INSTA_CROP), юзер может подвинуть любую грань мышкой и
подтвердить. Результат — (w, h, x, y) в пикселях исходного видео, идёт
напрямую в processors/*.uniqueize(crop_rect=...).

ВАЖНО про архитектуру перетаскивания: изначально хендлы были отдельными
QGraphicsItem с собственными mousePressEvent/mouseMoveEvent. На практике это
дважды роняло процесс в SIGSEGV (Sbk_GetPyOverride на мёртвом объекте) —
похоже на use-after-free на границе Python/Shiboken, когда diалог закрывается,
а в очереди Qt ещё остаётся событие, адресованное конкретному item'у. Поэтому
здесь хендлы — ЧИСТО визуальные QGraphicsRectItem без собственной обработки
событий; всё перетаскивание обрабатывается централизованно во view
(mousePress/Move/ReleaseEvent), которая переживает весь диалог целиком и не
пересоздаётся — событию некому "зависнуть" на мёртвом объекте.
"""
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication, QDialog, QGraphicsPixmapItem, QGraphicsRectItem,
    QFrame, QGraphicsScene, QGraphicsView, QHBoxLayout, QLabel, QLayout,
    QPushButton,
    QVBoxLayout,
)

from app.gui import theme

HANDLE = 10  # размер визуального квадратика хендла, в экранных пикселях
HIT = 14     # зона хвата мышью вокруг грани/угла (шире, чем сам квадратик)

EDGES = ["nw", "n", "ne", "e", "se", "s", "sw", "w"]
CURSORS = {
    "n": Qt.SizeVerCursor, "s": Qt.SizeVerCursor,
    "e": Qt.SizeHorCursor, "w": Qt.SizeHorCursor,
    "ne": Qt.SizeBDiagCursor, "sw": Qt.SizeBDiagCursor,
    "nw": Qt.SizeFDiagCursor, "se": Qt.SizeFDiagCursor,
}


def fit_frame(pix: QPixmap, frame_w: int, frame_h: int,
              initial_crop: tuple[int, int, int, int] | None,
              max_w: int, max_h: int) -> tuple[QPixmap, float, QRectF, QRectF]:
    """Вписывает кадр в доступное место экрана, считает координаты рамки.

    Вынесено из CropEditorDialog.__init__ как чистая функция (без Qt-виджетов
    сверх QPixmap/QRectF) — переиспользуется и диалогом, и embedded-виджетом
    в app/gui/uniq_editor.py, чтобы не дублировать формулы масштабирования.

    Возвращает (scaled_pixmap, scale, bounds_rect, initial_rect_scene):
    - scaled_pixmap — кадр, вписанный в max_w×max_h с сохранением пропорций;
    - scale — коэффициент кадр(px исходника) → сцена(px виджета);
    - bounds_rect — границы сцены (0,0,scaled_w,scaled_h);
    - initial_rect_scene — стартовая рамка кропа в координатах сцены.
    """
    if pix.isNull():
        raise RuntimeError(
            "Не удалось показать кадр ролика — файл повреждён или "
            "скачался не полностью. Скачайте ссылку заново.")
    # вписываем кадр И по ширине, И по высоте доступного места — вертикальное
    # видео (9:16) масштабируется по высоте, горизонтальное по ширине;
    # KeepAspectRatio само выбирает более тесную границу.
    scale_w = max_w / max(pix.width(), 1)
    scale_h = max_h / max(pix.height(), 1)
    scale = max(0.05, min(1.0, scale_w, scale_h))
    scaled = pix.scaled(int(pix.width() * scale), int(pix.height() * scale),
                         Qt.KeepAspectRatio, Qt.SmoothTransformation)
    scale = scaled.width() / frame_w  # точный коэффициент кадр→сцена
    bounds = QRectF(0, 0, scaled.width(), scaled.height())

    if initial_crop:
        w, h, x, y = initial_crop
    else:
        w, h, x, y = frame_w, frame_h, 0, 0
    initial_rect = QRectF(x * scale, y * scale, w * scale, h * scale)
    return scaled, scale, bounds, initial_rect


def rect_to_crop(r: QRectF, scale: float,
                 frame_w: int = 0, frame_h: int = 0) -> tuple[int, int, int, int]:
    """Переводит рамку в координатах сцены обратно в (w,h,x,y) исходника,
    с округлением к чётности (libx264 требует чётную ширину/высоту).

    frame_w/frame_h — размер исходника; если заданы, рамка зажимается в его
    границы. Без этого «Весь кадр» на сцене 500.0 px давал высоту 1922 при
    кадре 1920 (округление scale), а ffmpeg на выходящей за кадр рамке падает.
    """
    w = round(r.width() / scale)
    h = round(r.height() / scale)
    x = round(r.x() / scale)
    y = round(r.y() / scale)
    if frame_w and frame_h:
        x = max(0, min(x, frame_w))
        y = max(0, min(y, frame_h))
        w = min(w, frame_w - x)
        h = min(h, frame_h - y)
    w -= w % 2
    h -= h % 2
    return (w, h, x, y)


class _CropView(QGraphicsView):
    """Вид с рамкой кропа. Вся логика перетаскивания — здесь (см. модульный
    docstring про причину: не в отдельных QGraphicsItem)."""

    def __init__(self, bounds: QRectF, initial: QRectF, on_change):
        super().__init__()
        self.setRenderHint(QPainter.Antialiasing)
        self.bounds = bounds
        self.on_change = on_change
        self._drag_edge: str | None = None

        self.scene_ = QGraphicsScene(self)
        self.setScene(self.scene_)

        # Затемняем ТО, ЧТО ОТРЕЖЕТСЯ, а внутри рамки оставляем чистый кадр.
        # Раньше синей полупрозрачной заливкой был залит наоборот сохраняемый
        # кусок: кожа зеленела, оценить кадр было нельзя. Как в фоторедакторах.
        self.shade_items: list[QGraphicsRectItem] = []
        for _ in range(4):          # полосы сверху/снизу/слева/справа
            band = QGraphicsRectItem()
            band.setBrush(QBrush(QColor(0, 0, 0, 130)))
            band.setPen(QPen(Qt.NoPen))
            band.setZValue(4)
            self.scene_.addItem(band)
            self.shade_items.append(band)

        self.rect_item = QGraphicsRectItem(initial)
        self.rect_item.setPen(QPen(QColor(theme.ACCENT), 2))
        self.rect_item.setBrush(QBrush(Qt.NoBrush))   # внутри — чистый кадр
        self.rect_item.setZValue(5)
        self.scene_.addItem(self.rect_item)

        self.handles: dict[str, QGraphicsRectItem] = {}
        for edge in EDGES:
            h = QGraphicsRectItem(-HANDLE / 2, -HANDLE / 2, HANDLE, HANDLE)
            h.setBrush(QBrush(QColor(theme.ACCENT)))
            h.setPen(QPen(Qt.white, 1))
            h.setZValue(10)
            self.scene_.addItem(h)
            self.handles[edge] = h
        self._sync_handles()
        self._sync_shade()

    def _sync_shade(self):
        """Пересчитывает четыре затемняющие полосы вокруг рамки."""
        if not getattr(self, "shade_items", None):
            return
        scene = self.scene_.sceneRect()
        r = self.rect_item.rect()
        top, bottom, left, right = self.shade_items
        top.setRect(QRectF(scene.left(), scene.top(),
                           scene.width(), max(0.0, r.top() - scene.top())))
        bottom.setRect(QRectF(scene.left(), r.bottom(),
                              scene.width(), max(0.0, scene.bottom() - r.bottom())))
        # боковые — только по высоте рамки: углы уже закрыты верхней и нижней
        # полосой, иначе они затемнялись бы дважды и были бы темнее остального
        left.setRect(QRectF(scene.left(), r.top(),
                            max(0.0, r.left() - scene.left()), r.height()))
        right.setRect(QRectF(r.right(), r.top(),
                             max(0.0, scene.right() - r.right()), r.height()))

    def add_background(self, pix):
        self.scene_.addItem(QGraphicsPixmapItem(pix))
        # фон должен рисоваться под рамкой — вставляем перед остальным z-order
        # (проще: даём отрицательный zValue)
        for item in self.scene_.items():
            if isinstance(item, QGraphicsPixmapItem):
                item.setZValue(-1)

    def rect(self) -> QRectF:
        return self.rect_item.rect()

    def set_rect(self, r: QRectF):
        self.rect_item.setRect(r)
        self._sync_handles()

    def _sync_handles(self):
        # затемнение пересчитываем здесь же: так ни одна точка, двигающая
        # рамку (set_rect, перетаскивание, «Весь кадр»), не забудет про него
        self._sync_shade()
        r = self.rect_item.rect()
        c = r.center()
        positions = {
            "nw": r.topLeft(), "n": QPointF(c.x(), r.top()),
            "ne": r.topRight(), "e": QPointF(r.right(), c.y()),
            "se": r.bottomRight(), "s": QPointF(c.x(), r.bottom()),
            "sw": r.bottomLeft(), "w": QPointF(r.left(), c.y()),
        }
        for edge, pos in positions.items():
            self.handles[edge].setPos(pos)

    def _edge_at(self, scene_pos: QPointF) -> str | None:
        """Какая грань/угол под курсором (с запасом HIT), если есть."""
        for edge, item in self.handles.items():
            center = item.pos()
            if (abs(scene_pos.x() - center.x()) <= HIT
                    and abs(scene_pos.y() - center.y()) <= HIT):
                return edge
        return None

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            pos = self.mapToScene(event.position().toPoint())
            edge = self._edge_at(pos)
            if edge:
                self._drag_edge = edge
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_edge:
            pos = self.mapToScene(event.position().toPoint())
            self._resize(self._drag_edge, pos)
            event.accept()
            return
        # курсор-подсказка при наведении на хендл, даже без зажатой кнопки
        pos = self.mapToScene(event.position().toPoint())
        edge = self._edge_at(pos)
        self.setCursor(CURSORS[edge] if edge else Qt.ArrowCursor)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._drag_edge:
            self._drag_edge = None
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _resize(self, edge: str, scene_pos: QPointF):
        r = QRectF(self.rect_item.rect())
        x = max(self.bounds.left(), min(scene_pos.x(), self.bounds.right()))
        y = max(self.bounds.top(), min(scene_pos.y(), self.bounds.bottom()))

        min_size = 40
        if "n" in edge:
            r.setTop(min(y, r.bottom() - min_size))
        if "s" in edge:
            r.setBottom(max(y, r.top() + min_size))
        if "w" in edge:
            r.setLeft(min(x, r.right() - min_size))
        if "e" in edge:
            r.setRight(max(x, r.left() + min_size))

        self.rect_item.setRect(r.normalized())
        self._sync_handles()
        if self.on_change:
            self.on_change()


class CropEditorDialog(QDialog):
    """Модальное окно: показывает кадр видео, даёт разметить прямоугольник кропа.

    exec() блокирует (обычная модалка), поэтому вызывается из главного потока —
    после фонового download(), до фонового uniqueize() (см. app/pipeline.py).
    """

    def __init__(self, frame_path: Path, frame_w: int, frame_h: int,
                 initial_crop: tuple[int, int, int, int] | None = None,
                 confident: bool = False, parent=None, pixmap: QPixmap | None = None):
        """pixmap — готовый кадр, если он уже есть у вызывающего кода
        (app/gui/uniq_editor.py держит его после скачивания); иначе кадр
        читается из frame_path."""
        super().__init__(parent)
        self.setWindowTitle("Обрезка")
        self.frame_w = frame_w
        self.frame_h = frame_h
        self.result_crop: tuple[int, int, int, int] | None = None

        # доступное место на экране — вертикальные ролики (9:16) на маленьком
        # экране не влезали по высоте без этого расчёта. self.screen()/
        # parent.screen() до show() ненадёжны на некоторых сборках Qt —
        # берём QApplication.primaryScreen(), он доступен всегда.
        screen = QApplication.primaryScreen()
        avail = screen.availableGeometry() if screen else None
        if avail:
            max_w = int(avail.width() * 0.8)
            max_h = int(avail.height() * 0.85) - 180
        else:
            max_w, max_h = 900, 700
        max_w = max(max_w, 300)
        max_h = max(max_h, 300)

        root = QVBoxLayout(self)

        hint_text = (
            "Границы найдены автоматически — можно сразу применять."
            if confident else
            "Границы определились неуверенно — проверьте рамку глазами."
        )
        hint = QLabel(hint_text)
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{theme.TEXT_MUTED};font-size:12px;padding:2px 4px;")
        root.addWidget(hint)

        pix = pixmap if pixmap is not None and not pixmap.isNull() else QPixmap(str(frame_path))
        if pix.isNull():
            # путь к файлу пользователю ничего не говорит — он в лог, не в текст
            raise RuntimeError(
                "Не удалось показать кадр ролика — файл повреждён или "
                "скачался не полностью. Скачайте ссылку заново.")
        scaled, self.scale, bounds, initial_rect = fit_frame(
            pix, frame_w, frame_h, initial_crop, max_w, max_h
        )

        self.view = _CropView(bounds, initial_rect, on_change=self._update_size_label)
        self.view.add_background(scaled)
        self.view.scene_.setSceneRect(bounds)
        # затемнение считается от sceneRect, а он задаётся только сейчас —
        # до этого момента полосы легли бы по пустой сцене и не покрыли бока
        self.view._sync_shade()
        # Виджет ровно по картинке. Прежние +4 закладывались под рамку, но
        # QGraphicsView добавляет ещё и свой frame-отступ — viewport выходил на
        # 2 px больше сцены с каждой стороны, и по краям кадра шла тёмная кайма
        # фона (theme.BG). Убираем рамку и отступ, размер ставим по viewport.
        self.view.setFrameShape(QFrame.NoFrame)
        self.view.setContentsMargins(0, 0, 0, 0)
        self.view.setViewportMargins(0, 0, 0, 0)
        self.view.setFixedSize(scaled.width(), scaled.height())
        self.view.setStyleSheet(f"background:{theme.BG};border:none;")
        # Кадр по центру и без растяжки. У view жёсткий размер (setFixedSize
        # выше — сцена не тянется), поэтому если отдать его layout'у со
        # stretch=1, лишнее место окна остаётся пустым фоном: на тёмной теме
        # это читается как чёрные полосы справа и снизу от кадра.
        view_row = QHBoxLayout()
        view_row.setContentsMargins(0, 0, 0, 0)
        view_row.addStretch(1)
        view_row.addWidget(self.view)
        view_row.addStretch(1)
        root.addLayout(view_row)

        self.size_label = QLabel()
        self.size_label.setStyleSheet(f"color:{theme.TEXT_MUTED};font-size:11px;")
        # Ширина подписи фиксирована по кадру: окно подогнано по содержимому
        # (SetFixedSize ниже), а текст меняется на каждое движение мыши
        # («1080×1920 отступ 0:0» ↔ «884×1312 отступ 0:481»). Без фиксации
        # окно дёргалось на десятки пикселей при перетаскивании рамки.
        self.size_label.setFixedWidth(scaled.width())
        self.size_label.setAlignment(Qt.AlignCenter)
        self.size_label.setWordWrap(True)   # длинная подпись переносится,
        self.size_label.setFixedHeight(30)  # но высота тоже фиксирована
        root.addWidget(self.size_label)
        self._update_size_label()

        btn_row = QHBoxLayout()
        reset_btn = QPushButton("Весь кадр")
        reset_btn.setToolTip("Растянуть рамку на всё видео — обрезки не будет")
        reset_btn.clicked.connect(self._reset_full)
        btn_row.addWidget(reset_btn)
        btn_row.addStretch(1)
        cancel_btn = QPushButton("Отмена")
        cancel_btn.setToolTip("Закрыть окно, оставив прежнюю рамку")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        ok_btn = QPushButton("Применить")
        ok_btn.setObjectName("primary")
        ok_btn.clicked.connect(self._accept)
        btn_row.addWidget(ok_btn)
        root.addLayout(btn_row)

        # окно ровно по содержимому: иначе вокруг кадра остаётся пустой фон
        # (кадр вписан в max_w/max_h, а окно открывалось заметно крупнее)
        self.setSizeGripEnabled(False)
        root.setSizeConstraint(QLayout.SetFixedSize)
        # Фон окна задаём явно. Глобальная тема красит «QMainWindow, QDialog»,
        # но при SetFixedSize macOS дорисовывает вокруг ужатого содержимого
        # свой системный фон окна — на светлой системной теме под кнопками
        # получалась светлая панель, хотя всё приложение тёмное.
        self.setAutoFillBackground(True)
        self.setStyleSheet(f"QDialog {{ background: {theme.BG}; }}")

    def _update_size_label(self):
        w, h, x, y = rect_to_crop(self.view.rect(), self.scale, self.frame_w, self.frame_h)
        self.size_label.setText(
            f"Обрезка {w}×{h}, отступ {x}:{y}\n"
            f"исходник {self.frame_w}×{self.frame_h}")

    def _reset_full(self):
        self.view.set_rect(self.view.scene_.sceneRect())
        self._update_size_label()

    def _skip(self):
        self.result_crop = None
        self.accept()

    def _accept(self):
        self.result_crop = rect_to_crop(self.view.rect(), self.scale, self.frame_w, self.frame_h)
        self.accept()


def ask_crop(frame_path: Path, frame_w: int, frame_h: int,
             initial_crop: tuple[int, int, int, int] | None = None,
             confident: bool = False, parent=None) -> tuple[int, int, int, int] | None:
    """Открывает модалку разметки, возвращает (w,h,x,y) или None («без кропа»)."""
    dlg = CropEditorDialog(frame_path, frame_w, frame_h, initial_crop, confident, parent)
    dlg.exec()
    return dlg.result_crop
