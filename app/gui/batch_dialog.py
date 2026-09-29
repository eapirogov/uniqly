"""Пачечная загрузка: несколько ссылок → один набор параметров уникализации,
настроенный один раз на эталонном (первом) видео → применяется ко всем.

Единственная модалка, которую вводит эта фича — оправдана тем, что это
разовая настройка перед запуском, а не редактирование конкретного ролика
(для одиночных роликов используется встроенная ProcessPanel, не модалка).
"""
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QComboBox, QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
    QScrollArea, QVBoxLayout,
)

from app import channels, pipeline
from app.gui import theme
from app.gui.uniq_editor import UniqEditorWidget
from app.worker import Worker


class BatchDialog(QDialog):
    """Шаг 1: список ссылок. Шаг 2 (после «Далее»): эталонное видео
    скачивается, показывается UniqEditorWidget для настройки. «Применить
    ко всем» закрывает диалог, результат — в .result (urls, crop, params,
    banner) или None, если отменено."""

    def __init__(self, channel_id: str, parent=None):
        super().__init__(parent)
        self.channel_id = channel_id
        self.setWindowTitle("Пачечная загрузка")
        self.resize(560, 700)
        self.result: dict | None = None
        self._workers = []
        self._urls: list[str] = []
        self._ref_src: Path | None = None

        self.root = QVBoxLayout(self)
        self._build_step1()

    # --- шаг 1: ввод ссылок ---

    def _build_step1(self):
        self._clear_layout()
        hint = QLabel("Вставь ссылки, каждая с новой строки. Первая ссылка "
                      "станет эталонной — на ней настроишь кроп и слайдеры, "
                      "остальные получат те же параметры.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{theme.TEXT_MUTED};font-size:12px;")
        self.root.addWidget(hint)

        # Канал выбирается прямо здесь. Раньше пачка молча брала тот, что
        # стоял в списке на вкладке «Работа», и сменить его в окне было
        # нельзя — казалось, что пачка умеет только один канал.
        chan_row = QHBoxLayout()
        chan_row.addWidget(QLabel("Канал:"))
        self.channel_box = QComboBox()
        for ch in channels.CHANNELS:
            self.channel_box.addItem(ch.name, ch.id)
        idx = self.channel_box.findData(self.channel_id)
        if idx >= 0:
            self.channel_box.setCurrentIndex(idx)
        self.channel_box.currentIndexChanged.connect(self._on_channel_changed)
        chan_row.addWidget(self.channel_box, 1)
        self.root.addLayout(chan_row)

        self.urls_edit = QPlainTextEdit()
        self.urls_edit.setPlaceholderText("https://...\nhttps://...\n…")
        self.root.addWidget(self.urls_edit, 1)

        self.step1_status = QLabel("")
        self.step1_status.setStyleSheet(f"color:{theme.DANGER};")
        self.root.addWidget(self.step1_status)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        cancel_btn = QPushButton("Отмена")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        next_btn = QPushButton("Далее →")
        next_btn.setObjectName("primary")
        next_btn.clicked.connect(self._on_next)
        btn_row.addWidget(next_btn)
        self.root.addLayout(btn_row)

    def _on_channel_changed(self, _index: int):
        """Канал сменили до «Далее» — от него зависят пресет, рамка кропа
        и фон превью, поэтому запоминаем сразу."""
        self.channel_id = self.channel_box.currentData()

    def _on_next(self):
        raw = self.urls_edit.toPlainText().strip()
        urls = [u.strip() for u in raw.splitlines() if u.strip()]
        if not urls:
            self.step1_status.setText("Вставь хотя бы одну ссылку.")
            return
        self._urls = urls
        # Сохраняем список СРАЗУ, до скачивания: если пачка упадёт (пропал
        # интернет, VPN закрыл Instagram), ссылки останутся в файле —
        # восстановить их иначе неоткуда.
        saved = pipeline.save_links(urls, self.channel_id)
        if saved:
            print(f"[batch] список ссылок сохранён: {saved}")
        self._build_step2_loading()
        self._download_reference()

    # --- шаг 2: скачивание эталонного видео ---

    def _build_step2_loading(self):
        self._clear_layout()
        self.step2_status = QLabel(f"Скачиваю эталонное видео (1 из {len(self._urls)})…")
        self.root.addWidget(self.step2_status)

    def _download_reference(self):
        worker = Worker(lambda progress: pipeline.download_step(self._urls[0], progress))
        self._workers.append(worker)

        def _cleanup(*_):
            if worker in self._workers:
                self._workers.remove(worker)

        worker.signals.stage.connect(self.step2_status.setText)
        worker.signals.done.connect(self._on_reference_downloaded)
        worker.signals.done.connect(_cleanup)
        worker.signals.failed.connect(self._on_reference_failed)
        worker.signals.failed.connect(_cleanup)
        from PySide6.QtCore import QThreadPool
        QThreadPool.globalInstance().start(worker)

    def _on_reference_failed(self, err):
        """Эталон не скачался — настраивать не на чем.

        Раньше здесь просто менялся текст: оставалось пустое окно без единой
        кнопки, из которого нельзя ни повторить, ни выйти — со стороны это
        выглядело как «окно появилось и зависло». Теперь даём вернуться к
        списку ссылок (они никуда не делись) или повторить попытку.
        """
        self._clear_layout()
        title = QLabel("<b>Не удалось скачать первый ролик</b>")
        self.root.addWidget(title)

        msg = QLabel(str(err))
        msg.setWordWrap(True)
        msg.setStyleSheet(f"color:{theme.DANGER};font-size:12px;")
        self.root.addWidget(msg)

        note = QLabel(
            "Ссылки сохранены — их можно вернуть кнопкой «← К ссылкам».\n"
            "Список также лежит в папке saved_links рядом с данными.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{theme.TEXT_MUTED};font-size:11px;")
        self.root.addWidget(note)
        self.root.addStretch(1)

        btn_row = QHBoxLayout()
        cancel_btn = QPushButton("Отмена")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        btn_row.addStretch(1)
        back_btn = QPushButton("← К ссылкам")
        back_btn.clicked.connect(self._back_to_links)
        btn_row.addWidget(back_btn)
        retry_btn = QPushButton("↻ Повторить")
        retry_btn.setObjectName("primary")
        retry_btn.clicked.connect(self._retry_reference)
        btn_row.addWidget(retry_btn)
        self.root.addLayout(btn_row)

    def _back_to_links(self):
        """Возврат к списку ссылок — с уже введённым текстом."""
        urls = list(self._urls)
        self._build_step1()
        self.urls_edit.setPlainText("\n".join(urls))

    def _retry_reference(self):
        """Ещё одна попытка скачать эталон (напр. после включения VPN)."""
        self._build_step2_loading()
        self._download_reference()

    def _on_reference_downloaded(self, src):
        self._ref_src = Path(src)
        self._build_step2_editor()

    # --- шаг 2: настройка эталона ---

    def _build_step2_editor(self):
        from processors import cropdetect, uniq
        from app import preview

        self._clear_layout()
        try:
            ch_name = channels.by_id(self.channel_id).name
        except KeyError:
            ch_name = self.channel_id
        # Канал виден и на этом шаге: от него зависят пресет и фон полей,
        # а настройка идёт именно под него — без подписи легко забыть,
        # для какого канала сейчас крутишь ползунки.
        self.root.addWidget(QLabel(
            f"<b>Настрой параметры на эталонном видео</b><br>"
            f"канал <b>{ch_name}</b>, применится ко всем "
            f"{len(self._urls)} ссылкам:"))

        # Редактору нужно ~935 px по высоте, а окно 700 — ползунки уходили
        # за нижний край, и добраться до них было нельзя: в диалоге, в
        # отличие от вкладки «Работа», прокрутки не было.
        self.editor = UniqEditorWidget(compact=True, channel_id=self.channel_id)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setWidget(self.editor)
        scroll.setStyleSheet(
            f"QScrollArea, QScrollArea > QWidget > QWidget "
            f"{{ background: {theme.BG}; }}")
        self.root.addWidget(scroll, 1)

        try:
            frame = preview.make_preview(self._ref_src, at_sec=1.0)
            fw, fh = cropdetect.probe_size(self._ref_src)
            initial, confident = channels.initial_crop(
                self.channel_id, self._ref_src)
            pix = QPixmap(str(frame))
            self.editor.set_frame(self._ref_src, pix, fw, fh, initial, confident)
            # как и одиночный ролик: пресет из настроек, иначе нейтраль —
            # случайный набор менял бы картинку до того, как человек его увидел
            self.editor.apply_default_preset()
        except Exception as e:  # noqa: BLE001
            self.root.addWidget(QLabel(f"Ошибка разметки: {e}"))

        # баннер для батча — переиспользуем комбобокс через мини-панель было
        # бы избыточно для диалога; выбор баннера на batch пропускаем в первой
        # итерации (можно донастроить индивидуально на каждой ProcessPanel
        # после того, как батч создаст карточки — там уже есть полноценный
        # выбор баннера)
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        cancel_btn = QPushButton("Отмена")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        apply_btn = QPushButton(f"Применить ко всем ({len(self._urls)})")
        apply_btn.setObjectName("primary")
        apply_btn.clicked.connect(self._on_apply_all)
        btn_row.addWidget(apply_btn)
        self.root.addLayout(btn_row)

    def _on_apply_all(self):
        self.result = {
            "urls": self._urls,
            "channel_id": self.channel_id,
            "crop_rect": self.editor.current_crop(),
            "uniq_params": self.editor.current_params(),
            "banner_path": None,
            # эталон (первая ссылка) уже скачан — отдаём файл, чтобы панель
            # не качала его заново
            "ref_src": str(self._ref_src) if self._ref_src else None,
        }
        self.accept()

    # --- утилита ---

    def _clear_layout(self):
        while self.root.count():
            item = self.root.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
            layout = item.layout()
            if layout:
                while layout.count():
                    sub = layout.takeAt(0)
                    if sub.widget():
                        sub.widget().deleteLater()
