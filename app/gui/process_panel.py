"""Единая панель обработки ролика: и для нового ролика по ссылке, и для
редактирования из галереи, и для пачечной загрузки (app/gui/batch_dialog.py
передаёт preset_*).

Секции сверху вниз (одноэкранная композиция, без пошагового wizard):
заголовок/статус → видео+кроп+превью (UniqEditorWidget) → слайдеры
(тот же UniqEditorWidget) → описание (КиноПоиск) → баннер (библиотека) →
действия (применить/finder/telegram/повторить).

Три режима инициализации:
- ProcessPanel(url, channel_id) — новый ролик, сам качает + автодетект кропа
  + случайные параметры (make_params()).
- ProcessPanel() пустая + load_from_history(rec) — редактирование готового
  ролика из галереи, без повторного скачивания.
- ProcessPanel(url, channel_id, preset_crop, preset_params, preset_banner) —
  batch-режим (app/gui/batch_dialog.py): качает сама, но не запускает
  автодетект/рандом — сразу проставляет переданные значения.
"""
import os
from functools import partial
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QPlainTextEdit, QProgressBar, QPushButton, QVBoxLayout, QWidget,
)

from app import channels, kinopoisk, pipeline, platform_utils, storage
from app.config import PREVIEWS
from app.gui import theme
from app.gui.uniq_editor import UniqEditorWidget
from app.worker import Worker

HOOK_BTN_STYLE = "text-align:left;padding:4px 8px;"

# Кнопка «Прочитать с видео» (OCR надписи через tesseract) — личная фича:
# tesseract это отдельная программа, в сборку для друзей она не поедет (см.
# план, шаг 6). Признак «личная версия» тот же, что у личных вкладок в
# app/gui/main_window.py — заданный DATABASE_URL. Само поле хука при этом
# остаётся у всех: канал с надписью может завести и посторонний.
OCR_ENABLED = bool(os.getenv("DATABASE_URL"))
# личная версия — от неё зависят баннер, кнопка «Пост TG», OCR
PERSONAL = OCR_ENABLED


class ProcessPanel(QFrame):
    def __init__(self, url: str = "", channel_id: str = "",
                 preset_crop: tuple[int, int, int, int] | None = None,
                 preset_params: dict | None = None,
                 preset_banner: str | None = None,
                 pool=None, autostart: bool = False,
                 ready_src: str | None = None, local_name: str = ""):
        """autostart — запускать рендер сразу, как только кадр готов (пачечная
        загрузка: параметры уже заданы на эталоне, подтверждать нечего).
        ready_src — уже готовый файл: эталон пачки или ролик, который
        пользователь выбрал у себя на диске (тогда url пустой).
        local_name — имя файла с диска, для заголовка панели."""
        super().__init__()
        self.local_name = local_name
        self.autostart = autostart
        self.ready_src = ready_src
        self.url = url
        self.channel_id = channel_id
        # пул для фоновых задач этой панели — batch-режим передаёт свой
        # ограниченный QThreadPool (main_window.batch_pool, maxThreadCount=2),
        # чтобы не перегружать CPU параллельными ffmpeg-процессами; обычные
        # панели используют глобальный пул (не ограничен, там редко больше
        # 2-3 роликов одновременно от ручных кликов пользователя)
        from PySide6.QtCore import QThreadPool
        self._pool = pool or QThreadPool.globalInstance()
        self.source_path: str | None = None
        self.output_path: str | None = None
        self.movie_name: str = ""
        self.movie_id: str = ""
        self.chosen_hook: str = ""
        self.crop_rect: tuple[int, int, int, int] | None = None
        self.uniq_params: dict = {}
        self.banner_path: str | None = None
        self.on_saved = None          # callback() после сохранения → обновить галерею
        self.on_retry = None          # callback(panel) — повторить скачивание
        self.on_open_tg_post = None   # callback(movie_id, movie_name)
        self.on_removed = None        # callback() — панель убрала себя
        self.on_batch_done = None     # callback(ok: bool) — для окна прогресса
                                      # пачечной загрузки (см. batch_progress.py)
        self._workers = []  # держим ссылки — см. паттерн против SIGSEGV
        self._applied_desc = ""
        self._applied_hook = ""
        self._batch_preset = (preset_crop is not None or preset_params is not None
                               or preset_banner is not None)

        self.setFrameShape(QFrame.StyledPanel)
        root = QVBoxLayout(self)

        # --- заголовок/статус ---
        title_row = QHBoxLayout()
        # Заголовок: ссылка, имя файла с диска или режим правки из галереи
        self.title = QLabel(url or local_name or "Редактирование ролика")
        self.title.setWordWrap(True)
        self.title.setStyleSheet("font-weight:600;")
        title_row.addWidget(self.title, 1)
        self.remove_btn = QPushButton("✕")
        self.remove_btn.setToolTip("Убрать ролик из «Работы» (если уже применён — "
                                    "останется сохранённым в «Мои ролики»)")
        self.remove_btn.setFixedWidth(28)
        self.remove_btn.clicked.connect(self._remove_self)
        title_row.addWidget(self.remove_btn)
        root.addLayout(title_row)

        # --- канал (можно сменить прямо в панели, до «Применить») ---
        channel_row = QHBoxLayout()
        channel_row.addWidget(QLabel("Канал:"))
        self.channel_combo = QComboBox()
        for ch in channels.CHANNELS:
            self.channel_combo.addItem(ch.name, ch.id)
        if channel_id:
            idx = self.channel_combo.findData(channel_id)
            if idx >= 0:
                self.channel_combo.setCurrentIndex(idx)
        self.channel_combo.currentIndexChanged.connect(self._on_channel_changed)
        channel_row.addWidget(self.channel_combo, 1)
        root.addLayout(channel_row)

        status_row = QHBoxLayout()
        self.status = QLabel("в очереди")
        self.status.setStyleSheet(f"color:{theme.ACCENT};")
        status_row.addWidget(self.status)
        # busy-индикатор (неопределённый QProgressBar) — виден, пока идёт
        # фоновая задача (скачивание/рендер): без него долгий полный рендер
        # (реально ~30-60с — watermark/баннер/цветокор на весь ролик, не
        # 5-секундный превью-клип) визуально неотличим от зависшего окна,
        # т.к. кнопка просто гаснет, а статус-текст легко не заметить
        self.busy_bar = QProgressBar()
        self.busy_bar.setRange(0, 0)  # 0,0 = indeterminate/busy режим Qt
        self.busy_bar.setFixedWidth(80)
        self.busy_bar.setFixedHeight(6)
        self.busy_bar.setTextVisible(False)
        self.busy_bar.hide()
        status_row.addWidget(self.busy_bar)
        status_row.addStretch(1)
        root.addLayout(status_row)
        self.retry_btn = QPushButton("↻ Повторить")
        self.retry_btn.setToolTip("Попробовать скачать и обработать снова")
        self.retry_btn.clicked.connect(self._retry)
        self.retry_btn.hide()
        root.addWidget(self.retry_btn)

        # --- кроп + слайдеры (общий виджет) ---
        self.uniq_editor = UniqEditorWidget(channel_id=channel_id)
        root.addWidget(self.uniq_editor)
        # Пока ролик качается, показывать монтаж нечего: превью пустое, кнопка
        # обрезки неактивна. Прячем весь блок до появления кадра — на экране
        # остаётся только строка загрузки с процентами.
        self.uniq_editor.hide()

        # --- хук (надпись поверх видео) — только личная версия ---
        # Виджеты СОЗДАЮТСЯ только в личной версии, а не создаются-и-прячутся: скрытие
        # через setVisible(False) не срабатывало, секция всё равно появлялась
        # на экране. У постороннего пользователя её нет вовсе.
        self.hook_edit = None
        if PERSONAL:
            hook_head = QHBoxLayout()
            self.hook_label = QLabel("Хук — надпись на видео:")
            self.hook_label.setStyleSheet(f"color:{theme.TEXT_MUTED};font-size:11px;")
            self.ocr_btn = QPushButton("👁 Прочитать с видео")
            self.ocr_btn.setToolTip(
                "Распознать надпись автора на кадрах исходника (tesseract) "
                "и подставить её в поле — дальше можно править руками"
            )
            self.ocr_btn.clicked.connect(self._read_hook_from_video)
            hook_head.addWidget(self.hook_label, 1)
            hook_head.addWidget(self.ocr_btn)
            root.addLayout(hook_head)

            self.hook_edit = QPlainTextEdit()
            self.hook_edit.setPlaceholderText("Текст хука (Enter — новая строка)…")
            self.hook_edit.setStyleSheet(f"font-weight:700;color:{theme.WARNING};")
            self.hook_edit.setFixedHeight(70)
            root.addWidget(self.hook_edit)
            # Результат распознавания пишем ЗДЕСЬ, а не в строку состояния
            # панели: та живёт у самого верха (y≈70), а кнопка — внизу
            # (y≈1000+). Человек жал кнопку, смотрел на неё же и не видел
            # ни «Читаю…», ни ответа — выглядело как «кнопка не работает».
            self.hook_status = QLabel("")
            self.hook_status.setStyleSheet(
                f"color:{theme.TEXT_MUTED};font-size:11px;")
            self.hook_status.setWordWrap(True)
            root.addWidget(self.hook_status)

        # Всё, что имеет смысл только после скачивания (описание, заготовки,
        # баннер, кнопки) — в одном блоке: он прячется вместе с монтажом, пока
        # ролик качается, чтобы панель не висела пустой формой.
        self.after_download = QWidget()
        root.addWidget(self.after_download)
        root = QVBoxLayout(self.after_download)
        root.setContentsMargins(0, 0, 0, 0)
        self.after_download.hide()

        # --- описание (поиск фильма в КиноПоиске) ---
        find_row = QHBoxLayout()
        self.movie_input = QLineEdit()
        self.movie_input.setPlaceholderText("Название фильма для описания…")
        self.movie_input.returnPressed.connect(self._search_movie)
        self.find_btn = QPushButton("Найти")
        self.find_btn.clicked.connect(self._search_movie)
        find_row.addWidget(self.movie_input, 1)
        find_row.addWidget(self.find_btn)
        root.addLayout(find_row)

        self.movie_results = QVBoxLayout()
        self.movie_results.setSpacing(3)
        self.movie_buttons: list[QPushButton] = []
        root.addLayout(self.movie_results)

        self.desc = QPlainTextEdit()
        self.desc.setPlaceholderText(
            "Описание. Найдите фильм — данные подставятся сюда, "
            "дальше допишите своё."
        )
        self.desc.setFixedHeight(90)
        root.addWidget(self.desc)

        # Заготовки описания: свой текст (призыв подписаться, теги, ссылка)
        # пишется один раз и вставляется в пару кликов, а не набирается заново
        # под каждый ролик.
        tpl_row = QHBoxLayout()
        tpl_row.addWidget(QLabel("Заготовка:"))
        self.tpl_combo = QComboBox()
        self.tpl_combo.setToolTip("Сохранённый текст — вставится в конец описания")
        self._reload_templates()
        tpl_row.addWidget(self.tpl_combo, 1)
        self.tpl_insert_btn = QPushButton("Вставить")
        self.tpl_insert_btn.clicked.connect(self._insert_template)
        self.tpl_save_btn = QPushButton("Сохранить…")
        self.tpl_save_btn.setToolTip("Сохранить текущее описание как заготовку")
        self.tpl_save_btn.clicked.connect(self._save_template)
        self.tpl_del_btn = QPushButton("✕")
        self.tpl_del_btn.setFixedWidth(28)
        self.tpl_del_btn.setToolTip("Удалить выбранную заготовку")
        self.tpl_del_btn.clicked.connect(self._delete_template)
        tpl_row.addWidget(self.tpl_insert_btn)
        tpl_row.addWidget(self.tpl_save_btn)
        tpl_row.addWidget(self.tpl_del_btn)
        root.addLayout(tpl_row)

        # --- баннер (библиотека) ---
        # Баннер — рекламная плашка поверх ролика. Личная вещь: у
        # постороннего нет ни рекламодателей, ни файлов баннеров, и кнопка
        # «Добавить» вела бы в пустоту. Скрываем по тому же признаку, что и
        # остальное личное (DATABASE_URL); в личной версии всё работает как раньше.
        banner_row = QHBoxLayout()
        self.banner_label = QLabel("Баннер:")
        banner_row.addWidget(self.banner_label)
        self.banner_combo = QComboBox()
        self._reload_banner_combo()
        self.banner_combo.currentIndexChanged.connect(self._on_banner_changed)
        banner_row.addWidget(self.banner_combo, 1)
        self.add_banner_btn = QPushButton("+ Добавить")
        self.add_banner_btn.clicked.connect(self._add_banner)
        banner_row.addWidget(self.add_banner_btn)
        root.addLayout(banner_row)
        if not PERSONAL:
            self.banner_label.hide()
            self.banner_combo.hide()
            self.add_banner_btn.hide()

        # --- действия ---
        btn_row = QHBoxLayout()
        self.apply_btn = QPushButton("Применить")
        self.apply_btn.setObjectName("primary")
        self.apply_btn.setEnabled(False)
        self.apply_btn.clicked.connect(self._apply)
        self.open_btn = QPushButton(
            f"Открыть в {platform_utils.file_manager_name()}")
        self.open_btn.setEnabled(False)
        self.open_btn.clicked.connect(self._open)
        self.queue_btn = QPushButton("📅 Пост для Telegram")
        self.queue_btn.setEnabled(False)
        self.queue_btn.setToolTip("Открыть предпросмотр поста → правка → в очередь бота")
        self.queue_btn.clicked.connect(self._queue_telegram)
        # кнопка ведёт во вкладку «Посты TG», которой без DATABASE_URL нет
        # (см. app/gui/main_window.py) — тогда прячем её совсем
        if not os.getenv("DATABASE_URL"):
            self.queue_btn.hide()
        btn_row.addWidget(self.apply_btn)
        btn_row.addWidget(self.open_btn)
        btn_row.addWidget(self.queue_btn)
        root.addLayout(btn_row)

        self._sync_hook_visible()

        # Старт: новый ролик — по ссылке (обычной или из пачки) либо уже
        # готовым файлом с диска пользователя. Пустая панель без того и
        # другого (url == "" и ready_src == None) ждёт load_from_history().
        if url or ready_src:
            self._preset_crop = preset_crop
            self._preset_params = preset_params
            self._preset_banner = preset_banner
            self._start_download()

        # --- заготовки описаний ---

    def _reload_templates(self):
        self.tpl_combo.blockSignals(True)
        self.tpl_combo.clear()
        items = storage.desc_templates()
        if not items:
            self.tpl_combo.addItem("нет сохранённых", None)
        for t in items:
            self.tpl_combo.addItem(t["name"], t["text"])
        self.tpl_combo.blockSignals(False)

    def _insert_template(self):
        """Дописывает заготовку в конец описания, не затирая найденное."""
        text = self.tpl_combo.currentData()
        if not text:
            return
        current = self.desc.toPlainText().rstrip()
        self.desc.setPlainText(f"{current}\n\n{text}" if current else text)

    def _save_template(self):
        from PySide6.QtWidgets import QInputDialog
        text = self.desc.toPlainText().strip()
        if not text:
            self.set_status("заготовка: сначала напишите описание")
            return
        name, ok = QInputDialog.getText(self, "Сохранить заготовку",
                                        "Название заготовки:")
        if not ok or not name.strip():
            return
        storage.desc_template_save(name.strip(), text)
        self._reload_templates()
        idx = self.tpl_combo.findText(name.strip())
        if idx >= 0:
            self.tpl_combo.setCurrentIndex(idx)
        self.set_status(f"заготовка «{name.strip()}» сохранена")

    def _delete_template(self):
        name = self.tpl_combo.currentText()
        if not self.tpl_combo.currentData():
            return
        storage.desc_template_delete(name)
        self._reload_templates()
        self.set_status(f"заготовка «{name}» удалена")

    # --- запуск/скачивание ---

    def _run_worker(self, fn, on_done, on_fail, on_stage=None):
        worker = Worker(fn)
        self._workers.append(worker)

        def _cleanup(*_):
            if worker in self._workers:
                self._workers.remove(worker)

        if on_stage:
            worker.signals.stage.connect(on_stage)
        worker.signals.done.connect(on_done)
        worker.signals.done.connect(_cleanup)
        worker.signals.failed.connect(on_fail)
        worker.signals.failed.connect(_cleanup)
        self._pool.start(worker)

    def is_local(self) -> bool:
        """Ролик пришёл файлом с диска, а не по ссылке."""
        return bool(self.local_name) and not self.url

    def _start_download(self):
        # Пачечная загрузка уже скачала первый ролик как эталонный (на нём
        # настраивались параметры) — качать его второй раз незачем. Каждый
        # вызов downloader.download() даёт файлу новое случайное имя, то есть
        # кэша нет и это была бы честная повторная закачка.
        if self.ready_src and Path(self.ready_src).exists():
            src, self.ready_src = self.ready_src, None
            self._on_downloaded(src)
            return
        if self.is_local():
            # Ролик брали файлом с диска — качать нечего. Сюда попадаем, если
            # копия в рабочей папке пропала (напр. нажали «Очистить
            # исходники»); «Повторить» без этой ветки ушло бы скачивать
            # пустую ссылку и выдало бы невнятную ошибку.
            self.set_failed(
                "Файл больше не найден в рабочей папке. "
                "Добавьте ролик заново кнопкой «📁 Файл с компьютера»."
            )
            return
        self.busy_bar.show()
        self._run_worker(
            partial(pipeline.download_step, self.url),
            self._on_downloaded,
            self._on_failed,
            on_stage=self.set_status,
        )

    def _on_downloaded(self, src):
        from processors import cropdetect
        from app import preview

        self.source_path = str(src)
        self.set_status("Читаю кадр…")
        try:
            frame = preview.make_preview(Path(src), at_sec=1.0)
            fw, fh = cropdetect.probe_size(src)
            if self._batch_preset:
                # batch-режим: не автодетект, а переданные значения. Если
                # разрешение исходника отличается от эталонного (на котором
                # снят preset_crop) — пересчитываем пропорционально, чтобы
                # рамка не улетела за пределы кадра другого видео.
                initial = self._rescale_preset_crop(fw, fh)
                confident = True
            else:
                initial, confident = channels.initial_crop(self.channel_id, src)
        except Exception as e:  # noqa: BLE001 — не валим панель на этапе кадра
            # без префикса «разметка кропа:» — текст ошибки уже человеческий
            # (см. processors/cropdetect.probe_size, app/ffmpeg_error),
            # а приписка делала его похожим на технический лог
            self.set_failed(str(e))
            return

        pix = QPixmap(str(frame))
        self.uniq_editor.show()
        self.after_download.show()
        self.uniq_editor.set_frame(Path(src), pix, fw, fh, initial, confident)
        if self._batch_preset and self._preset_params:
            self.uniq_editor.set_params(self._preset_params)
        elif not self._batch_preset:
            # Новый ролик — пресет по умолчанию из настроек, а если он не
            # задан, нейтраль: пользователь сначала видит исходник как есть и
            # сам решает, что менять. Не минимумы QSlider (те дали бы contrast
            # 0.90) и не случайный make_params(): случайный набор незаметно
            # менял картинку ещё до того, как человек тронул хоть один
            # ползунок. Свой набор — «Мой пресет», случайный — «Случайные
            # настройки».
            self.uniq_editor.apply_default_preset()
        if self._batch_preset and self._preset_banner:
            self._select_banner_by_path(self._preset_banner)

        self.busy_bar.hide()
        self.set_status("Готово к применению")
        self.apply_btn.setEnabled(True)
        self.retry_btn.hide()

        # Пачечная загрузка: параметры уже настроены на эталонном ролике и
        # применены ко всем — жать «Применить» на каждой панели незачем,
        # весь смысл пачки в том, чтобы запустить всё разом. Стартуем сами.
        if self._batch_preset and self.autostart:
            self._apply()

    def _preset_name(self) -> str:
        """Пресет текущего канала ("" если канала нет) — нужен make_params,
        чтобы стартовые значения ползунков подходили этому пресету."""
        try:
            return channels.by_id(self.channel_id).preset if self.channel_id else ""
        except KeyError:
            return ""

    def _on_channel_changed(self, _index: int):
        """Смена канала прямо в панели (до «Применить») — влияет на пресет
        рендера, шапку/подпись описания и, если исходник уже скачан, на
        рекомендованный кроп (канал с заданной рамкой fixed_crop — её,
        остальные — автодетект чёрных полос). banner_combo не трогаем — сам
        выбор баннера не привязан к каналу технически (crop.py/reels.py оба
        поддерживают banner_path), но список каналов, где раньше был баннер
        по умолчанию, тоже может отличаться — оставляем пользователю решать."""
        new_id = self.channel_combo.currentData()
        if new_id == self.channel_id:
            return
        self.channel_id = new_id
        # фон полей у нового канала может быть другим (чёрный/размытый) —
        # превью должно показывать то же, что получится в ролике
        self.uniq_editor.channel_id = new_id
        self._sync_hook_visible()

        if self.source_path and Path(self.source_path).exists():
            from processors import cropdetect
            try:
                fw, fh = cropdetect.probe_size(self.source_path)
                initial, confident = channels.initial_crop(
                    self.channel_id, self.source_path)
                self.uniq_editor.reset_crop(initial, confident)
                self.set_status(f"канал сменён — кроп пересчитан под {new_id}")
            except Exception as e:  # noqa: BLE001 — не валим панель на смене канала
                self.set_status(f"канал сменён, но кроп не пересчитан: {e}")

        # описание/хук собирались под старый канал (шапка/подпись/формат
        # хука зависят от channel_id) — явно просим перепроверить, а не
        # тихо оставляем нерелевантный текст
        if self.desc.toPlainText().strip():
            self.desc.setPlainText(
                self.desc.toPlainText() + "\n\n⚠️ канал сменён — проверь описание/хук"
            )

    def _rescale_preset_crop(self, fw: int, fh: int) -> tuple[int, int, int, int] | None:
        """Пересчитывает preset_crop под разрешение ЭТОГО видео, если оно
        отличается от того, на котором снята эталонная рамка (см. риск в
        плане batch-режима: разные ссылки могут иметь разное разрешение)."""
        crop = self._preset_crop
        if not crop:
            return None
        # эталонное разрешение неизвестно напрямую здесь — берём как есть,
        # если совпадает с текущим кадром; иначе масштабируем от предположения,
        # что crop уже в пикселях кадра 1080×1920 (стандарт для всех трёх
        # источников проекта — см. app/channels.py) и пропорционально
        # приводим к fw×fh, если они отличаются.
        ref_w, ref_h = 1080, 1920
        if (fw, fh) == (ref_w, ref_h):
            return crop
        w, h, x, y = crop
        sx, sy = fw / ref_w, fh / ref_h
        rescaled = (round(w * sx), round(h * sy), round(x * sx), round(y * sy))
        self.set_status("кроп пересчитан под другое разрешение, проверь рамку")
        return rescaled

    def _on_failed(self, err):
        self.set_failed(err)

    def _retry(self):
        if not self.on_retry:
            return
        self.retry_btn.setEnabled(False)
        self.retry_btn.setText("↻ …")
        self.set_status("Повтор…")
        self.on_retry(self)

    # --- режим редактирования из галереи ---

    def load_from_history(self, rec: dict):
        """Восстанавливает панель из записи истории (галерея → редактировать).
        НЕ качает заново — используется сохранённый source_path/output_path."""
        self.url = rec.get("url", "")
        self.channel_id = rec.get("channel_id", "")
        self.source_path = rec.get("source_path") or None
        self.output_path = rec.get("output_path")
        self.movie_name = rec.get("movie_name", "")
        self.movie_id = rec.get("movie_id", "") or ""
        self.title.setText(self.url or self.output_path or "")
        # синхронизируем комбобокс канала БЕЗ срабатывания _on_channel_changed
        # (кадр ещё не загружен на этом этапе — пере-детект кропа упадёт)
        self.channel_combo.blockSignals(True)
        idx = self.channel_combo.findData(self.channel_id)
        if idx >= 0:
            self.channel_combo.setCurrentIndex(idx)
        self.channel_combo.blockSignals(False)
        self.queue_btn.setEnabled(bool(self.movie_id))
        self.desc.setPlainText(rec.get("description", ""))
        self._applied_desc = self.desc.toPlainText().strip()
        self.chosen_hook = rec.get("hook", "") or ""
        if self.hook_edit is not None:
            self.hook_edit.setPlainText(self.chosen_hook)
        self._applied_hook = self.chosen_hook
        self._sync_hook_visible()

        # превью самого готового ролика показывать негде в этой панели
        # (секция превью — для кадра с фильтрами при редактировании
        # кропа/слайдеров), open_btn ниже даёт посмотреть готовый файл напрямую
        self.open_btn.setEnabled(bool(self.output_path and Path(self.output_path).exists()))

        saved_crop = pipeline.decode_crop(rec.get("crop_rect", ""))
        saved_params = pipeline.decode_params(rec.get("uniq_params", ""))
        self.banner_path = rec.get("banner_path") or None

        if not self.source_path or not Path(self.source_path).exists():
            # исходник удалён (см. GalleryTab._clean_downloads) — без него
            # нельзя пересобрать видео, оставляем только описание/пост
            self.set_status("исходник удалён — доступно только описание")
            self.uniq_editor.setEnabled(False)
            self.apply_btn.setEnabled(False)
            return

        from processors import cropdetect
        from app import preview
        try:
            frame = preview.make_preview(Path(self.source_path), at_sec=1.0)
            fw, fh = cropdetect.probe_size(self.source_path)
        except Exception as e:  # noqa: BLE001
            self.set_status(f"не удалось прочитать кадр: {e}")
            return

        pix = QPixmap(str(frame))
        self.uniq_editor.show()
        self.after_download.show()
        self.uniq_editor.set_frame(Path(self.source_path), pix, fw, fh, saved_crop, True)
        self.uniq_editor.set_params(saved_params)
        if self.banner_path:
            self._select_banner_by_path(self.banner_path)
        self.set_status("из истории")
        self.status.setStyleSheet(f"color:{theme.SUCCESS};")
        self.apply_btn.setEnabled(True)

    # --- статус ---

    def set_status(self, text: str):
        self.status.setText(text)
        self.status.setStyleSheet(f"color:{theme.ACCENT};")

    def set_failed(self, text: str):
        # окно прогресса ждёт ответа по каждому ролику — иначе счётчик
        # «N из M» не дойдёт до конца и полоса застрянет
        if self.on_batch_done:
            self.on_batch_done(False, self.url, text)
            self.on_batch_done = None      # только один раз на панель
        self.status.setText(f"ошибка: {text}")
        self.status.setStyleSheet(f"color:{theme.DANGER};")
        self.busy_bar.hide()
        self.retry_btn.show()
        self.retry_btn.setEnabled(True)
        self.retry_btn.setText("↻ Повторить")

    # --- применение (рендер) ---

    def _apply(self):
        # Проверяем именно НАЛИЧИЕ ФАЙЛА, а не непустую строку пути: при
        # редактировании старого ролика из галереи путь в истории есть, а сам
        # файл мог быть удалён (чистка папки downloads). Раньше карточка
        # писала «Применяю… 30-60с» и падала позже с ошибкой ffmpeg.
        if not self.source_path:
            self.set_failed("Сначала скачайте ролик по ссылке.")
            return
        if not Path(self.source_path).exists():
            self.set_failed(
                "Исходный файл ролика не найден — его удалили или перенесли. "
                "Скачайте ссылку заново.")
            return
        self.crop_rect = self.uniq_editor.current_crop()
        self.uniq_params = self.uniq_editor.current_params()
        # хук берём из поля прямо перед рендером (поле пустое или скрыто у
        # каналов без use_hook → надпись просто не наносится)
        if self.hook_edit is not None:
            self.chosen_hook = self.hook_edit.toPlainText().strip()
        self.apply_btn.setEnabled(False)
        self.set_status("Применяю… (полный рендер занимает 30-60с)")
        self.busy_bar.show()
        self._run_worker(
            partial(
                pipeline.render_with_params, Path(self.source_path), self.channel_id,
                crop_rect=self.crop_rect, uniq_params=self.uniq_params,
                caption=self.chosen_hook, banner_path=self.banner_path,
            ),
            self._on_applied,
            self._on_failed,
            on_stage=self.set_status,
        )

    def _on_applied(self, result: dict):
        if self.on_batch_done:
            self.on_batch_done(True, self.url, "")
            self.on_batch_done = None      # только один раз на панель
        self.output_path = result["output"]
        self.apply_btn.setEnabled(True)
        self.open_btn.setEnabled(True)
        self.busy_bar.hide()
        self.set_status("готово")
        self.status.setStyleSheet(f"color:{theme.SUCCESS};")
        self._save_history()
        # ролик сохранён в БД и уже виден в галерее «Мои ролики» — панель на
        # вкладке «Работа» больше не нужна, убираем её (решение пользователя:
        # не копить готовые панели списком, редактировать дальше — из галереи)
        if self.on_removed:
            self.on_removed()
        self.setParent(None)
        self.deleteLater()

    def _rename_by_movie(self):
        """Переименовывает готовый файл в «<Название фильма>.mp4» (для
        ориентира в Finder/галерее)."""
        if not self.movie_name or not self.output_path:
            return
        import re
        old = Path(self.output_path)
        if not old.exists():
            return
        # безопасное имя файла из названия фильма
        safe = re.sub(r"[^\w\s-]", "", self.movie_name, flags=re.UNICODE).strip()
        safe = re.sub(r"\s+", "_", safe)
        if not safe:
            return
        new = old.parent / f"{safe}.mp4"
        # если такое имя занято другим файлом — добавляем хвост из старого stem
        if new.exists() and new != old:
            new = old.parent / f"{safe}_{old.stem.split('_')[0]}.mp4"
        if new == old:
            return
        try:
            old.rename(new)
            # переносим и превью
            old_prev = PREVIEWS / f"{old.stem}.jpg"
            if old_prev.exists():
                old_prev.rename(PREVIEWS / f"{new.stem}.jpg")
            self.output_path = str(new)
        except OSError:
            pass

    def _save_history(self):
        if not self.output_path:
            return
        self._rename_by_movie()
        storage.save(
            url=self.url, channel_id=self.channel_id,
            source_path=self.source_path or "", output_path=self.output_path,
            hook=self.chosen_hook, description=self.desc.toPlainText().strip(),
            movie_name=self.movie_name, movie_id=self.movie_id,
            crop_rect=pipeline.encode_crop(self.crop_rect),
            uniq_params=pipeline.encode_params(self.uniq_params),
            banner_path=self.banner_path or "",
        )
        self._applied_desc = self.desc.toPlainText().strip()
        if self.hook_edit is not None:
            self._applied_hook = self.hook_edit.toPlainText().strip()
        if self.on_saved:
            self.on_saved()

    def has_unsaved(self) -> bool:
        if self.desc.toPlainText().strip() != self._applied_desc:
            return True
        if self.hook_edit is None:
            return False
        return self.hook_edit.toPlainText().strip() != self._applied_hook

    # --- описание (КиноПоиск) ---

    def _search_movie(self):
        query = self.movie_input.text().strip()
        if not query:
            return
        self.find_btn.setEnabled(False)
        self.find_btn.setText("Ищу…")
        self._run_worker(
            lambda _p: kinopoisk.search(query),
            self._show_movie_results,
            self._search_failed,
        )

    def _search_failed(self, error: Exception):
        """Поиск не дошёл до сервиса — говорим об этом прямо, а не «ничего
        не найдено»: человек иначе решит, что дело в названии фильма."""
        self.find_btn.setEnabled(True)
        self.find_btn.setText("Найти")
        self.set_status(f"поиск фильма: {error} — попробуй ещё раз")
        self.status.setStyleSheet(f"color:{theme.DANGER};")

    def _show_movie_results(self, films: list):
        self.find_btn.setEnabled(True)
        self.find_btn.setText("Найти")
        for btn in self.movie_buttons:
            btn.deleteLater()
        self.movie_buttons.clear()
        if not films:
            self.movie_input.setPlaceholderText("Ничего не найдено, уточни название")
            return
        for f in films[:5]:
            btn = QPushButton(f"{f['name']} ({f['year']})")
            btn.setStyleSheet(HOOK_BTN_STYLE)
            btn.clicked.connect(
                lambda _=False, mid=f["id"], nm=f["name"]: self._pick_movie(mid, nm)
            )
            self.movie_buttons.append(btn)
            self.movie_results.addWidget(btn)

    def _pick_movie(self, movie_id: int, movie_name: str = ""):
        self.movie_name = movie_name
        self.movie_id = str(movie_id)
        self.queue_btn.setEnabled(True)
        self.desc.setPlainText("Собираю описание…")
        try:
            ch = channels.by_id(self.channel_id) if self.channel_id else None
        except KeyError:
            ch = None  # канал удалён — описание соберём без шапки/подписи
        header = ch.desc_header if ch else True
        signature = ch.desc_signature if ch else ""
        self._run_worker(
            lambda _p: kinopoisk.build_reels_description(
                movie_id, header=header, signature=signature
            ),
            lambda text: self.desc.setPlainText(text or "Описание не найдено"),
            lambda e: self.desc.setPlainText(
                f"Не удалось получить описание: {e}.\n"
                "Нажми на фильм ещё раз или впиши описание руками."
            ),
        )

    def _queue_telegram(self):
        if not self.movie_id or not self.on_open_tg_post:
            return
        self.on_open_tg_post(self.movie_id, self.movie_name)

    # --- хук (надпись на видео) ---

    def _sync_hook_visible(self):
        """Секция хука нужна только каналам с use_hook (tiktok) — остальным
        прячем целиком, чтобы не занимала место и не путала.

        Само поле хука остаётся у всех: канал с надписью может завести и
        посторонний пользователь. А вот кнопка OCR — личная (OCR_ENABLED),
        она требует установленного tesseract, которого у друзей не будет."""
        # надпись — личная фича: она жёстко свёрстана под его стиль
        # (шрифт, размер 78, палитра с синей серединой) и постороннему не
        # настраивается, поэтому в его версии секции нет вовсе
        # Секция видна на любом канале. Раньше она показывалась только там,
        # где use_hook=True, и при переносе
        # ролика из галереи на другом канале поле с надписью и кнопка
        # распознавания просто исчезали — со стороны это выглядело как
        # поломка. Распознать надпись полезно на любом ролике: текст можно
        # забрать в описание. Нанесётся она всё равно только тем пресетом,
        # который это умеет (tiktok), так что ничего не ломается.
        show = PERSONAL
        if self.hook_edit is None:
            return   # версия без хука — виджетов нет вовсе
        self.hook_label.setVisible(show)
        self.ocr_btn.setVisible(show and OCR_ENABLED)
        self.hook_edit.setVisible(show)
        if getattr(self, "hook_status", None) is not None:
            self.hook_status.setVisible(show)

    def _hook_msg(self, text: str, bad: bool = False):
        """Сообщение под полем хука — там, куда человек смотрит, нажимая
        кнопку распознавания."""
        if getattr(self, "hook_status", None) is None:
            return
        self.hook_status.setText(text)
        color = theme.DANGER if bad else theme.TEXT_MUTED
        self.hook_status.setStyleSheet(f"color:{color};font-size:11px;")

    def _read_hook_from_video(self):
        """Распознаёт надпись автора на кадрах исходника (tesseract) и кладёт
        её в поле хука. Результат — заготовка для правки руками, а не финал:
        OCR по анимированной надписи часто читает с ошибками.

        Tesseract — внешняя программа, её может не быть (для сборки под друзей
        он не обязателен) — тогда просто говорим об этом, не роняя панель."""
        if not self.source_path or not Path(self.source_path).exists():
            self._hook_msg("исходник не найден — распознавать нечего", bad=True)
            return
        self.ocr_btn.setEnabled(False)
        self.ocr_btn.setText("Читаю…")
        self._hook_msg("читаю надпись с кадров…")

        def done(text: str):
            self.ocr_btn.setEnabled(True)
            self.ocr_btn.setText("👁 Прочитать с видео")
            if text:
                self.hook_edit.setPlainText(text)
                self._hook_msg("надпись распознана — проверьте текст выше")
            else:
                self._hook_msg("надпись не найдена на кадрах — впишите руками",
                               bad=True)

        def fail(e):
            self.ocr_btn.setEnabled(True)
            self.ocr_btn.setText("👁 Прочитать с видео")
            if isinstance(e, FileNotFoundError):
                self._hook_msg("не установлен tesseract "
                               "(brew install tesseract tesseract-lang)", bad=True)
            else:
                self._hook_msg(f"не прочиталось — {e}", bad=True)

        # импорт внутри обработчика: в сборке для друзей app/ocr.py не нужен
        # вовсе, и модуль не тянется при старте
        from app import ocr

        self._run_worker(
            lambda _p: pipeline.clean_hook(ocr.read_onscreen_text(Path(self.source_path))),
            done, fail,
        )

    # --- баннер (библиотека) ---

    def _reload_banner_combo(self):
        self.banner_combo.blockSignals(True)
        self.banner_combo.clear()
        self.banner_combo.addItem("Без баннера", None)
        for b in storage.banner_library_list():
            self.banner_combo.addItem(b["name"], b["path"])
        self.banner_combo.blockSignals(False)

    def _select_banner_by_path(self, path: str):
        idx = self.banner_combo.findData(path)
        if idx >= 0:
            self.banner_combo.setCurrentIndex(idx)

    def _on_banner_changed(self, _index: int):
        self.banner_path = self.banner_combo.currentData()

    def _add_banner(self):
        from processors import banner
        import shutil

        path, _ = QFileDialog.getOpenFileName(
            self, "Выбери файл баннера", filter="Видео (*.mov *.mp4 *.webm)"
        )
        if not path:
            return
        dst = banner.library_dir() / Path(path).name
        try:
            shutil.copy2(path, dst)
        except OSError as e:
            self.set_status(f"не удалось скопировать баннер: {e}")
            return
        storage.banner_library_add(Path(path).stem, str(dst))
        self._reload_banner_combo()
        self._select_banner_by_path(str(dst))

    # --- открыть в Finder ---

    def _open(self):
        if self.output_path:
            import subprocess
            platform_utils.reveal_in_file_manager(self.output_path)

    # --- убрать панель из «Работы» ---

    def _remove_self(self):
        """Кнопка «✕» — убирает панель со вкладки «Работа» вручную (напр.
        неудачное скачивание, передумал, или просто расчистить список).
        Если что-то ещё считается в фоне (скачивание/рендер) — спрашиваем
        подтверждение, т.к. воркер продолжит работать в пуле, но результат
        будет некуда показать (панель уже удалена)."""
        if self._workers:
            from PySide6.QtWidgets import QMessageBox
            ans = QMessageBox.question(
                self, "Идёт обработка",
                "Ролик ещё обрабатывается в фоне. Убрать панель сейчас?\n"
                "(результат может не сохраниться, если это не финальный рендер)",
                QMessageBox.Yes | QMessageBox.No,
            )
            if ans != QMessageBox.Yes:
                return
        if self.on_removed:
            self.on_removed()
        self.setParent(None)
        self.deleteLater()
