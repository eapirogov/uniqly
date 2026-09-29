"""Главное окно: выбор канала + ссылка → панель обработки ролика в списке."""
import os
from pathlib import Path

from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QPushButton,
    QScrollArea, QTabWidget, QVBoxLayout, QWidget,
)

from app import channels, storage
from app.gui import theme
from app.gui.batch_dialog import BatchDialog
from app.gui.process_panel import ProcessPanel
from app.gui.gallery import GalleryTab
from app.gui.settings import SettingsTab


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Уникализатор видео")
        self.resize(680, 900)
        self.pool = QThreadPool.globalInstance()
        # batch-задачи (пачечная загрузка) идут через ОТДЕЛЬНЫЙ пул с
        # ограничением на 2 параллельных ffmpeg-процесса — иначе batch из
        # 10+ ссылок монополизирует globalInstance() (по умолчанию =
        # idealThreadCount() потоков), конкурируя с поиском в КиноПоиске/
        # OCR/обычными одиночными карточками. Одиночные ProcessPanel по
        # прежнему используют QThreadPool.globalInstance() напрямую (внутри
        # себя), это ок — там объективно редко больше 2-3 роликов сразу.
        self.batch_pool = QThreadPool()
        self.batch_pool.setMaxThreadCount(2)
        # Скачивание — отдельная очередь: оно ждёт сеть, а не процессор,
        # поэтому ролики качаются одновременно (раньше сначала скачивался
        # эталонный, остальные ждали своей очереди). Потолок держим, чтобы
        # десяток параллельных загрузок не забил канал целиком.
        self.download_pool = QThreadPool()
        self.download_pool.setMaxThreadCount(6)

        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)

        # Личные вкладки: «Посты TG» (очередь публикаций телеграм-бота),
        # «Статистика» (трекинг сдачи рекламной статы) и «Расписание»
        # (справочная сетка часов постинга под его каналы). Показываются
        # только когда задан DATABASE_URL. У постороннего пользователя этой
        # настройки нет — он их не видит, и заодно физически не может
        # дописать что-то в чужую очередь публикаций.
        personal = bool(os.getenv("DATABASE_URL"))

        # вкладка «Работа» — обработка роликов
        work = QWidget()
        root = QVBoxLayout(work)
        self.tabs.addTab(work, "Работа")

        # вкладка «Мои ролики» — незапощенные
        self.gallery = GalleryTab(
            on_edit=self.edit_from_history, show_posted=False,
            on_status_change=self._reload_galleries,
            on_open_tg_post=self.open_tg_post if personal else None,
            allow_posted=personal,   # см. комментарий ниже про «Запощено»
        )
        self.tabs.addTab(self.gallery, "Мои ролики")
        # Вкладка «Запощено» — личная: она про учёт публикаций в своих каналах
        # (пометил ролик — он ушёл из рабочего списка в архив). Стороннему
        # пользователю такой учёт не нужен, он просто забирает готовый файл.
        #
        # Прячем вкладку ВМЕСТЕ с кнопкой «✓ Запощено» (см. GalleryTab):
        # пометка физически переносит файл в подпапку posted/ и убирает его из
        # «Моих роликов». Без вкладки «Запощено» вернуть его было бы неоткуда —
        # ролик просто пропал бы с глаз.
        self.posted_gallery = None
        if personal:
            self.posted_gallery = GalleryTab(
                on_edit=self.edit_from_history, show_posted=True,
                on_status_change=self._reload_galleries,
                on_open_tg_post=self.open_tg_post,
            )
            self.tabs.addTab(self.posted_gallery, "Запощено")

        # Импорт личных вкладок — внутри условия: без DATABASE_URL не нужны.
        self.tg_posts = None
        self.stats = None
        self.schedule = None
        if personal:
            # если чего-то из этого нет в сборке (напр. psycopg2 выброшен) —
            # не роняем всё приложение, просто не показываем вкладку
            try:
                from app.gui.tg_posts import TgPostsTab
                from app.gui.stats import StatsTab
                from app.gui.schedule import ScheduleTab
            except ImportError as e:  # noqa: BLE001
                print(f"[main_window] личные вкладки недоступны: {e}")
            else:
                self.tg_posts = TgPostsTab()
                self.tabs.addTab(self.tg_posts, "Посты TG")
                self.stats = StatsTab()
                self.tabs.addTab(self.stats, "Статистика")
                self.schedule = ScheduleTab()
                self.tabs.addTab(self.schedule, "Расписание")

        # вкладка «Настройки» — каналы и параметры, нужна всем
        self.settings = SettingsTab(on_channels_changed=self._reload_channel_lists)
        self.tabs.addTab(self.settings, "Настройки")
        self.tabs.currentChanged.connect(self._on_tab)

        # выбор канала
        chan_row = QHBoxLayout()
        chan_row.addWidget(QLabel("Канал:"))
        self.channel_box = QComboBox()
        for ch in channels.CHANNELS:
            self.channel_box.addItem(ch.name, ch.id)
        self.channel_box.currentIndexChanged.connect(self._update_hint)
        chan_row.addWidget(self.channel_box, 1)
        root.addLayout(chan_row)

        # ссылка
        top = QHBoxLayout()
        self.url_input = QLineEdit()
        self.url_input.returnPressed.connect(self.add_video)
        self.add_btn = QPushButton("Добавить")
        self.add_btn.clicked.connect(self.add_video)
        self.file_btn = QPushButton("📁 Файл с компьютера…")
        self.file_btn.setToolTip(
            "Взять готовый ролик с диска вместо ссылки —\n"
            "например снятый на телефон или скачанный вручную")
        self.file_btn.clicked.connect(self.add_files)
        self.batch_btn = QPushButton("📦 Пачечная загрузка…")
        self.batch_btn.setToolTip("Несколько ссылок с одним набором параметров уникализации")
        self.batch_btn.clicked.connect(self._open_batch_dialog)
        top.addWidget(self.url_input, 1)
        top.addWidget(self.add_btn)
        top.addWidget(self.file_btn)
        top.addWidget(self.batch_btn)
        root.addLayout(top)
        self._update_hint()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        # контейнер внутри QScrollArea по умолчанию красится системным цветом
        # (на светлой теме macOS — белым пятном под тёмной вкладкой)
        container.setStyleSheet("background: transparent;")
        self.cards_layout = QVBoxLayout(container)
        self.cards_layout.setContentsMargins(0, 8, 0, 0)
        self.cards_layout.setSpacing(10)
        # подсказка на пустой вкладке — что делать дальше
        self.work_hint = QLabel(
            "Вставьте ссылку на ролик и нажмите «Добавить».\n"
            "Ролик появится здесь — можно будет обрезать и настроить."
        )
        self.work_hint.setAlignment(Qt.AlignCenter)
        self.work_hint.setStyleSheet(
            f"color:{theme.TEXT_MUTED};font-size:12px;padding:40px;")
        self.cards_layout.addWidget(self.work_hint)
        self.cards_layout.addStretch()
        scroll.setWidget(container)
        root.addWidget(scroll, 1)

    def _on_tab(self, index: int):
        w = self.tabs.widget(index)
        if w is self.gallery or (self.posted_gallery is not None
                                 and w is self.posted_gallery):
            w.reload()
        elif self.tg_posts is not None and w is self.tg_posts:
            self.tg_posts.reload_queue()
        elif self.stats is not None and w is self.stats:
            self.stats.reload()
        elif w is self.settings:
            # пресеты уникализации создаются на «Работе» — список в настройках
            # должен показывать их сразу, без перезапуска программы
            self.settings._reload_presets()

    def open_tg_post(self, movie_id: str, movie_name: str = ""):
        """Открывает редактор поста во вкладке «Посты TG» (с панели/галереи).
        Без DATABASE_URL вкладки нет — кнопка просто ничего не делает."""
        if not movie_id or self.tg_posts is None:
            return
        self.tg_posts.open_post(movie_id, movie_name)
        self.tabs.setCurrentWidget(self.tg_posts)

    def open_settings(self, new_channel: bool = False):
        """Открывает вкладку «Настройки». new_channel=True — сразу с пустой
        формой нового канала (из приветствия при первом запуске)."""
        self.tabs.setCurrentWidget(self.settings)
        if new_channel:
            self.settings._add_channel()

    def _reload_channel_lists(self):
        """Каналы изменились в настройках — обновляем выпадающие списки.

        Панели, уже открытые на вкладке «Работа», не трогаем: у них свой
        выбранный канал и незавершённая работа, менять его под руками
        пользователя нельзя.
        """
        current = self._current_channel_id()
        self.channel_box.blockSignals(True)
        self.channel_box.clear()
        for ch in channels.CHANNELS:
            self.channel_box.addItem(ch.name, ch.id)
        idx = self.channel_box.findData(current)
        if idx >= 0:
            self.channel_box.setCurrentIndex(idx)
        self.channel_box.blockSignals(False)
        self._update_hint()
        # фильтр каналов в галереях тоже перестроим
        for g in (self.gallery, self.posted_gallery):
            if g is not None and hasattr(g, "reload_channel_filter"):
                g.reload_channel_filter()

    def _reload_galleries(self):
        """Обновляет галереи (при смене статуса «запощено»).

        Вкладки «Запощено» может не быть — она личная (см. создание вкладок).
        """
        self.gallery.reload()
        if self.posted_gallery is not None:
            self.posted_gallery.reload()

    def _unsaved_cards(self) -> list:
        """Панели с несохранёнными правками."""
        cards = []
        for i in range(self.cards_layout.count()):
            w = self.cards_layout.itemAt(i).widget()
            if isinstance(w, ProcessPanel) and w.has_unsaved():
                cards.append(w)
        return cards

    def _confirm_unsaved(self) -> bool:
        """Спрашивает про несохранённые правки. True = можно продолжать."""
        from PySide6.QtWidgets import QMessageBox
        cards = self._unsaved_cards()
        if not cards:
            return True
        ans = QMessageBox.question(
            self, "Несохранённые изменения",
            f"Есть {len(cards)} ролик(ов) с несохранёнными изменениями.\n"
            "Сохранить перед выходом?",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
        )
        if ans == QMessageBox.Cancel:
            return False
        if ans == QMessageBox.Save:
            for c in cards:
                c._save_history()
        return True

    def closeEvent(self, event):
        if self._confirm_unsaved():
            event.accept()
        else:
            event.ignore()

    def _current_channel_id(self) -> str:
        return self.channel_box.currentData()

    def _update_hint(self):
        cid = self._current_channel_id()
        if not cid:
            # каналов ещё нет (первый запуск нового пользователя)
            self.url_input.setPlaceholderText("Сначала заведите канал…")
            return
        try:
            ch = channels.by_id(cid)
        except KeyError:
            self.url_input.setPlaceholderText("Канал не найден — выберите другой")
            return
        # площадку не называем: скачивается с любого сайта, который понимает
        # yt-dlp (YouTube, Instagram, TikTok и десятки других) — определяется
        # по самой ссылке
        self.url_input.setPlaceholderText(
            "Ссылка на ролик, можно несколько через пробел…")

    def add_video(self):
        # пакетно: несколько ссылок через пробел/перенос строки — каждая
        # получает СВОЮ независимую панель со своим автодетектом/слайдерами
        # (это не то же самое, что «Пачечная загрузка» — там один набор
        # параметров на всех, здесь — обычное независимое добавление нескольких)
        raw = self.url_input.text().strip()
        urls = [u for u in raw.replace("\n", " ").split() if u]
        if not urls:
            return
        channel_id = self._current_channel_id()
        self.url_input.clear()

        from PySide6.QtWidgets import QMessageBox
        for url in urls:
            # защита от дублей: ссылка уже делалась для этого канала
            if storage.exists_for_channel(url, channel_id):
                ans = QMessageBox.question(
                    self, "Уже обрабатывалось",
                    f"Этот ролик уже был для канала.\n{url}\nВсё равно сделать?",
                    QMessageBox.Yes | QMessageBox.No,
                )
                if ans != QMessageBox.Yes:
                    continue
            self._start_card(url, channel_id)

    def add_files(self):
        """Ролик (или несколько) с диска пользователя вместо ссылки.

        Файл копируется в рабочую папку (см. pipeline.import_file) и дальше
        идёт обычным путём: кадр, разметка кропа, ползунки, «Применить».
        """
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        from app import pipeline

        channel_id = self._current_channel_id()
        if not channel_id:
            QMessageBox.information(
                self, "Сначала канал",
                "Сначала заведите канал во вкладке «Настройки» — "
                "программе нужно знать, куда складывать готовый ролик.")
            return

        mask = " ".join("*" + s for s in pipeline.VIDEO_SUFFIXES)
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Выберите ролики",
            self._last_file_dir(),
            f"Видео ({mask});;Все файлы (*)",
        )
        if not paths:
            return
        # запоминаем папку: в следующий раз диалог откроется там же
        storage.kv_set("last_file_dir", str(Path(paths[0]).parent))

        for path in paths:
            try:
                src = pipeline.import_file(path)
            except RuntimeError as e:
                QMessageBox.warning(self, "Не удалось открыть файл", str(e))
                continue
            self._start_card("", channel_id, ready_src=str(src),
                             local_name=Path(path).name)

    def _last_file_dir(self) -> str:
        """Папка, откуда брали файл в прошлый раз (иначе — домашняя)."""
        saved = storage.kv_get("last_file_dir", "")
        return saved if saved and Path(saved).is_dir() else str(Path.home())

    def _sync_work_hint(self):
        """Подсказка видна, пока на вкладке нет ни одной панели."""
        has_panels = any(
            isinstance(self.cards_layout.itemAt(i).widget(), ProcessPanel)
            for i in range(self.cards_layout.count())
        )
        self.work_hint.setVisible(not has_panels)

    def _start_card(self, url: str, channel_id: str,
                    ready_src: str | None = None, local_name: str = ""):
        panel = ProcessPanel(url, channel_id, ready_src=ready_src,
                             local_name=local_name)
        panel.on_saved = self._reload_galleries
        panel.on_retry = self._retry_panel
        panel.on_open_tg_post = self.open_tg_post
        panel.on_removed = self._sync_work_hint
        self.cards_layout.insertWidget(self.cards_layout.count() - 1, panel)
        self._sync_work_hint()

    def _retry_panel(self, panel: ProcessPanel):
        """Повторная попытка скачивания/обработки той же панели (после,
        напр., включения VPN) — панель начинает свой цикл заново."""
        panel._start_download()

    def edit_from_history(self, rec: dict):
        """Открывает запись истории в новой панели на вкладке «Работа»."""
        panel = ProcessPanel()
        panel.on_saved = self._reload_galleries
        panel.on_open_tg_post = self.open_tg_post
        panel.on_removed = self._sync_work_hint
        panel.load_from_history(rec)
        self.cards_layout.insertWidget(self.cards_layout.count() - 1, panel)
        self._sync_work_hint()
        self.tabs.setCurrentIndex(0)  # переключаемся на «Работа»

    # --- пачечная загрузка ---

    def _open_batch_dialog(self):
        channel_id = self._current_channel_id()
        dlg = BatchDialog(channel_id, parent=self)
        dlg.exec()
        if not dlg.result:
            return
        self._run_batch(**dlg.result)

    def _run_batch(self, urls: list[str], channel_id: str,
                   crop_rect, uniq_params: dict, banner_path,
                   ref_src: str | None = None):
        """Обрабатывает все ссылки фоном, показывая одно окно прогресса.

        Карточек на вкладке «Работа» не создаём: пачка — это «отдал список,
        забрал готовые ролики», а не редактирование каждого. Всё происходит
        в окне прогресса, готовые ролики появляются в «Моих роликах».

        Две очереди с разными ограничениями: скачивание ждёт сеть, поэтому
        все ссылки качаются разом; рендер занимает процессор, поэтому идёт
        по 2 за раз (иначе параллельные ffmpeg забивают машину).
        """
        from app.gui.batch_progress import BatchProgressDialog
        from app.batch_runner import DownloadItem

        progress = BatchProgressDialog(len(urls), parent=self)
        progress.set_current("Скачиваю ролики…")
        progress.show()
        # Держим ссылки на задачи: иначе Python соберёт объект вместе с его
        # сигналами и окно перестанет обновляться (тот же приём против
        # SIGSEGV, что в app/gui/gallery.py).
        self._batch_items = []
        self._batch_settings = (channel_id, crop_rect, uniq_params, banner_path)

        for i, url in enumerate(urls):
            item = DownloadItem(url, ready_src=ref_src if i == 0 else None)
            item.signals.stage.connect(self._batch_stage(progress))
            item.signals.downloaded.connect(self._batch_downloaded(progress))
            item.signals.failed.connect(self._batch_failed(progress))
            self._batch_items.append(item)
            self.download_pool.start(item)

    def _batch_downloaded(self, progress):
        """Ролик скачан — ставим его в очередь на уникализацию."""
        def downloaded(url: str, src: str):
            from app.batch_runner import RenderItem
            channel_id, crop_rect, uniq_params, banner_path = self._batch_settings
            item = RenderItem(url, src, channel_id, crop_rect, uniq_params,
                              banner_path)
            item.signals.stage.connect(self._batch_stage(progress))
            item.signals.done.connect(self._batch_done(progress))
            item.signals.failed.connect(self._batch_failed(progress))
            self._batch_items.append(item)
            self.batch_pool.start(item)
        return downloaded

    def _batch_stage(self, progress):
        """Строка «что сейчас происходит» в окне прогресса."""
        def stage(url: str, text: str):
            try:
                progress.set_stage(url, text)
            except RuntimeError:
                pass       # окно свернули — C++-объект уже удалён
        return stage

    def _batch_done(self, progress):
        def done(url: str, _output: str):
            self._reload_galleries()
            try:
                progress.mark_done(True, url)
            except RuntimeError:
                pass
        return done

    def _batch_failed(self, progress):
        def failed(url: str, error: str):
            try:
                progress.mark_done(False, url, error)
            except RuntimeError:
                pass
        return failed
