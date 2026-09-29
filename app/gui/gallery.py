"""Вкладка «Мои ролики»: галерея готовых файлов из workspace/output/."""
import subprocess
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
    QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from app import platform_utils
from app.config import OUTPUT, PREVIEWS

# суффикс имени готового файла → пресет (для восстановления старых роликов
# без записи в истории). Суффикс ставит pipeline: <исходник>_<пресет>.mp4,
# плюс "_hook" от пересборки с надписью. Канал ищется как первый с таким
# пресетом — раньше тут были прямо id конкретных каналов.
_PRESET_BY_SUFFIX = {
    "hook": "tiktok", "tiktok": "tiktok",
    "reels": "reels", "crop": "crop",
}


class GalleryItem(QFrame):
    def __init__(self, path: Path, on_delete, on_edit=None, on_toggle_posted=None,
                 on_queue=None):
        super().__init__()
        self.path = path
        self.on_delete = on_delete
        self.on_edit = on_edit
        self.on_toggle_posted = on_toggle_posted
        self.on_queue = on_queue
        self.setFrameShape(QFrame.StyledPanel)
        row = QHBoxLayout(self)

        thumb = QLabel()
        thumb.setFixedSize(130, 231)
        thumb.setAlignment(Qt.AlignCenter)
        thumb.setStyleSheet("background:#222;border-radius:6px;")
        prev = PREVIEWS / f"{path.stem}.jpg"
        if prev.exists():
            pix = QPixmap(str(prev))
            if not pix.isNull():
                thumb.setPixmap(pix.scaled(130, 231, Qt.KeepAspectRatio,
                                           Qt.SmoothTransformation))
        row.addWidget(thumb)

        from app import storage
        hist = storage.by_output(str(path))
        movie_name = (hist or {}).get("movie_name", "") if hist else ""
        description = (hist or {}).get("description", "") if hist else ""
        movie_id = (hist or {}).get("movie_id", "") if hist else ""
        rec = self._edit_record(path)

        # канал: из истории, иначе по суффиксу имени файла
        channel_id = (hist or {}).get("channel_id", "") if hist else ""
        if not channel_id and rec:
            channel_id = rec.get("channel_id", "")
        right = QVBoxLayout()
        # сверху — подпись канала
        if channel_id:
            from app import channels
            try:
                ch_name = channels.by_id(channel_id).name
            except KeyError:
                # канал удалён в настройках, но ролики от него остались —
                # показываем сырой id, чтобы карточка не пропадала
                ch_name = channel_id
            chan = QLabel(f"📺 {ch_name}")
            chan.setStyleSheet("color:#4a90d9;font-weight:600;font-size:11px;")
            right.addWidget(chan)
        # название фильма (если известно), иначе имя файла
        title_text = f"🎬 {movie_name}" if movie_name else path.name
        name = QLabel(title_text)
        name.setWordWrap(True)
        name.setStyleSheet("font-weight:700;font-size:14px;")
        right.addWidget(name)
        size_mb = path.stat().st_size / 1024 / 1024
        sub = QLabel(f"{path.name} · {size_mb:.1f} МБ")
        sub.setStyleSheet("color:#888;font-size:10px;")
        right.addWidget(sub)

        # Описание правится прямо здесь. Раньше поле было только для чтения,
        # и чтобы поменять запятую, приходилось открывать «Редактировать» —
        # а это полная пересборка видео ради текста. Сам ролик при правке
        # описания не трогается (см. storage.update_description).
        self._saved_desc = description
        self._has_record = hist is not None
        self._channel_id = channel_id
        self._movie_id = movie_id
        self._movie_name = movie_name
        # ссылки на фоновые задачи: без них Python соберёт объект вместе с
        # сигналами и карточка упадёт (тот же приём, что в GalleryTab)
        self._workers: list = []
        self._film_buttons: list[QPushButton] = []
        # Поиск фильма прямо здесь: описание можно собрать, не открывая
        # «Редактировать» (тот пересобирает видео целиком ради текста).
        if self._has_record:
            find_row = QHBoxLayout()
            self.movie_input = QLineEdit()
            self.movie_input.setPlaceholderText(
                f"Название фильма{' — ' + movie_name if movie_name else ''}")
            self.movie_input.returnPressed.connect(self._search_movie)
            self.find_btn = QPushButton("🔍 Найти фильм")
            self.find_btn.clicked.connect(self._search_movie)
            find_row.addWidget(self.movie_input, 1)
            find_row.addWidget(self.find_btn)
            right.addLayout(find_row)
            # сюда лягут кнопки найденных фильмов
            self.film_results = QVBoxLayout()
            self.film_results.setSpacing(3)
            right.addLayout(self.film_results)

        self.desc = QPlainTextEdit(description)
        self.desc.setMinimumHeight(140)
        self.desc.setPlaceholderText(
            "Описание не сохранено — можно написать своё"
            if self._has_record else
            "Этого ролика нет в истории — описание сохранить не получится")
        # без записи в истории сохранять некуда: файл лежит в папке, но
        # программа про него ничего не знает (напр. скопирован руками)
        self.desc.setReadOnly(not self._has_record)
        self.desc.textChanged.connect(self._on_desc_changed)
        right.addWidget(self.desc, 1)

        # строка сохранения — прячется, пока текст не тронули
        self.desc_row = QHBoxLayout()
        self.desc_hint = QLabel("")
        self.desc_hint.setStyleSheet("color:#888;font-size:11px;")
        self.desc_row.addWidget(self.desc_hint)
        self.desc_row.addStretch(1)
        self.desc_revert_btn = QPushButton("Отменить")
        self.desc_revert_btn.clicked.connect(self._revert_desc)
        self.desc_revert_btn.hide()
        self.desc_row.addWidget(self.desc_revert_btn)
        self.desc_save_btn = QPushButton("💾 Сохранить описание")
        self.desc_save_btn.setObjectName("primary")
        self.desc_save_btn.clicked.connect(self._save_desc)
        self.desc_save_btn.hide()
        self.desc_row.addWidget(self.desc_save_btn)
        right.addLayout(self.desc_row)

        is_posted = bool((hist or {}).get("posted", 0))
        btns = QHBoxLayout()
        copy_btn = QPushButton("Копировать описание")
        copy_btn.clicked.connect(self._copy_desc)
        btns.addWidget(copy_btn)
        # переключатель статуса «запощено»
        if self.on_toggle_posted:
            post_btn = QPushButton("↩ Вернуть" if is_posted else "✓ Запощено")
            if not is_posted:
                post_btn.setStyleSheet("color:#5cb85c;font-weight:600;")
            post_btn.clicked.connect(lambda: self.on_toggle_posted(path))
            btns.addWidget(post_btn)
        # в очередь Telegram — если фильм известен (есть movie_id)
        if movie_id and self.on_queue:
            q_btn = QPushButton("📅 Пост TG")
            q_btn.setToolTip("Открыть предпросмотр поста → правка → в очередь бота")
            q_btn.clicked.connect(
                lambda: self.on_queue(movie_id, movie_name, q_btn)
            )
            btns.addWidget(q_btn)
        if rec and self.on_edit:
            edit_btn = QPushButton("Редактировать")
            # Запись берём заново в момент нажатия, а не тот снимок, что
            # сделан при создании карточки: описание могли поправить прямо
            # здесь, и старый снимок увёз бы в панель прежний текст.
            edit_btn.clicked.connect(lambda: self.on_edit(self._fresh_record(rec)))
            btns.addWidget(edit_btn)
        open_btn = QPushButton(platform_utils.file_manager_name())
        open_btn.clicked.connect(
            lambda: platform_utils.reveal_in_file_manager(path))
        del_btn = QPushButton("Удалить")
        del_btn.setStyleSheet("color:#d9534f;")
        del_btn.clicked.connect(self._delete)
        btns.addWidget(open_btn)
        btns.addWidget(del_btn)
        right.addLayout(btns)
        row.addLayout(right, 1)

    def _fresh_record(self, fallback: dict) -> dict:
        """Актуальная запись из базы на момент нажатия «Редактировать».

        Если в поле описания есть несохранённые правки — подставляем их:
        человек написал текст, нажал «Редактировать» и вправе увидеть там
        то, что видел в карточке.
        """
        from app import storage
        rec = storage.by_output(str(self.path)) or dict(fallback)
        if self.has_unsaved_desc():
            rec = dict(rec)
            rec["description"] = self.desc.toPlainText()
        return rec

    # --- поиск фильма и сборка описания ---

    def _run(self, fn, on_done, on_fail):
        """Фоновая задача: запрос к КиноПоиску не должен морозить галерею."""
        from app.worker import Worker
        from PySide6.QtCore import QThreadPool
        worker = Worker(lambda _p: fn())

        def _cleanup(*_):
            if worker in self._workers:
                self._workers.remove(worker)

        worker.signals.done.connect(on_done)
        worker.signals.done.connect(_cleanup)
        worker.signals.failed.connect(on_fail)
        worker.signals.failed.connect(_cleanup)
        self._workers.append(worker)
        QThreadPool.globalInstance().start(worker)

    def _search_movie(self):
        from app import kinopoisk
        query = self.movie_input.text().strip()
        if not query:
            return
        self.find_btn.setEnabled(False)
        self.find_btn.setText("Ищу…")
        self._run(lambda: kinopoisk.search(query),
                  self._show_films, self._search_failed)

    def _search_failed(self, error):
        """Сервис не ответил — говорим прямо, а не «ничего не найдено»:
        иначе человек решит, что неверно написал название."""
        self.find_btn.setEnabled(True)
        self.find_btn.setText("🔍 Найти фильм")
        self.desc_hint.setText(f"поиск не удался: {error}")
        self.desc_hint.setStyleSheet("color:#d9534f;font-size:11px;")

    def _show_films(self, films: list):
        self.find_btn.setEnabled(True)
        self.find_btn.setText("🔍 Найти фильм")
        for b in self._film_buttons:
            b.deleteLater()
        self._film_buttons.clear()
        if not films:
            self.desc_hint.setText("ничего не найдено, уточните название")
            self.desc_hint.setStyleSheet("color:#d9a441;font-size:11px;")
            return
        self.desc_hint.setText("выберите фильм — описание подставится ниже")
        self.desc_hint.setStyleSheet("color:#888;font-size:11px;")
        for f in films[:5]:
            btn = QPushButton(f"{f['name']} ({f['year']})")
            btn.clicked.connect(
                lambda _=False, mid=f["id"], nm=f["name"]: self._pick_film(mid, nm))
            self._film_buttons.append(btn)
            self.film_results.addWidget(btn)

    def _pick_film(self, movie_id: int, movie_name: str):
        """Собирает описание по выбранному фильму — с шапкой и подписью
        того канала, которому принадлежит ролик."""
        from app import channels, kinopoisk
        self._movie_id = str(movie_id)
        self._movie_name = movie_name
        for b in self._film_buttons:
            b.deleteLater()
        self._film_buttons.clear()
        self.desc.setPlainText("Собираю описание…")
        try:
            ch = channels.by_id(self._channel_id) if self._channel_id else None
        except KeyError:
            ch = None      # канал удалён — соберём без шапки и подписи
        header = ch.desc_header if ch else True
        signature = ch.desc_signature if ch else ""
        self._run(
            lambda: kinopoisk.build_reels_description(
                movie_id, header=header, signature=signature),
            lambda text: self.desc.setPlainText(text or "Описание не найдено"),
            lambda e: self.desc.setPlainText(
                f"Не удалось получить описание: {e}.\n"
                "Нажмите на фильм ещё раз или впишите описание руками."),
        )

    def has_unsaved_desc(self) -> bool:
        """В описании есть правки, которые ещё не записаны."""
        return self.desc.toPlainText() != self._saved_desc

    def _on_desc_changed(self):
        """Текст изменили — показываем кнопки сохранения."""
        changed = self.desc.toPlainText() != self._saved_desc
        self.desc_save_btn.setVisible(changed)
        self.desc_revert_btn.setVisible(changed)
        self.desc_hint.setText("есть несохранённые правки" if changed else "")
        self.desc_hint.setStyleSheet(
            f"color:{'#d9a441' if changed else '#888'};font-size:11px;")

    def _save_desc(self):
        """Пишет новое описание в историю. Видео не пересобирается."""
        from app import storage
        text = self.desc.toPlainText().strip()
        # фильм сохраняем вместе с описанием: иначе название и кнопка
        # «Пост TG» остались бы от прежнего фильма
        if not storage.update_description(
                str(self.path), text,
                movie_name=self._movie_name or None,
                movie_id=self._movie_id or None):
            self.desc_hint.setText("не удалось сохранить: ролика нет в истории")
            self.desc_hint.setStyleSheet("color:#d9534f;font-size:11px;")
            return
        self._saved_desc = text
        self.desc_save_btn.hide()
        self.desc_revert_btn.hide()
        self.desc_hint.setText("описание сохранено")
        self.desc_hint.setStyleSheet("color:#5cb85c;font-size:11px;")

    def _revert_desc(self):
        """Возврат к сохранённому тексту."""
        self.desc.setPlainText(self._saved_desc)

    def _copy_desc(self):
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(self.desc.toPlainText())

    def _edit_record(self, path: Path) -> dict | None:
        """Запись для редактирования: из истории, иначе восстановить по имени файла."""
        from app import storage
        from app.config import DOWNLOADS
        rec = storage.by_output(str(path))
        if rec and rec.get("source_path") and Path(rec["source_path"]).exists():
            return rec
        # старый ролик без истории: <хеш>_<суффикс>.mp4 → исходник <хеш>.mp4
        stem = path.stem.split("_")[0]
        src = DOWNLOADS / f"{stem}.mp4"
        if not src.exists():
            return None
        # канал: из папки (output/<channel>/ или .../posted/), иначе по суффиксу
        from app import channels
        folder = path.parent.name
        if folder == "posted":
            folder = path.parent.parent.name  # файл в posted/ — канал уровнем выше
        if channels.exists(folder):
            channel_id = folder
        else:
            # старый ролик без истории: угадываем канал по суффиксу имени
            # файла (суффикс — это ПРЕСЕТ), беря первый канал с таким
            # пресетом. Раньше тут был жёсткий словарь с id конкретных каналов,
            # что у другого пользователя всегда промахивалось.
            suffix = path.stem[len(stem) + 1:]
            preset = _PRESET_BY_SUFFIX.get(suffix, suffix)
            match = next((c for c in channels.CHANNELS if c.preset == preset), None)
            if match is None:
                match = channels.CHANNELS[0] if channels.CHANNELS else None
            channel_id = match.id if match else folder
        return {"url": "", "channel_id": channel_id, "source_path": str(src),
                "output_path": str(path), "hook": "", "hook_base": "",
                "description": ""}

    def _delete(self):
        confirm = QMessageBox.question(
            self, "Удалить ролик?",
            f"Удалить готовый файл?\n{self.path.name}",
            QMessageBox.Yes | QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass
        from app import storage
        storage.delete(str(self.path))  # чистим и запись истории
        self.on_delete(self)


class GalleryTab(QWidget):
    """Список роликов из workspace/output/. show_posted: показывать запощенные."""

    def __init__(self, on_edit=None, show_posted=False, on_status_change=None,
                 on_open_tg_post=None, allow_posted=None):
        super().__init__()
        self.on_edit = on_edit
        self.show_posted = show_posted  # False = «Мои ролики», True = «Запощено»
        # Показывать ли кнопку «✓ Запощено». Она переносит файл в подпапку
        # posted/ и убирает из этого списка — без вкладки «Запощено» ролик
        # оказался бы недостижим. По умолчанию привязано к наличию той вкладки
        # (она личная, см. app/gui/main_window.py).
        self.allow_posted = (show_posted if allow_posted is None
                             else allow_posted)
        self.on_status_change = on_status_change  # обновить обе вкладки
        self.on_open_tg_post = on_open_tg_post  # callback(movie_id, movie_name)
        self._workers = []  # ссылки на воркеры (иначе GC соберёт сигналы → краш)
        root = QVBoxLayout(self)

        from PySide6.QtWidgets import QComboBox
        from app import channels
        head = QHBoxLayout()
        self.count_label = QLabel("Готовые ролики:")
        head.addWidget(self.count_label)
        head.addWidget(QLabel("Канал:"))
        self.filter_box = QComboBox()
        self._fill_channel_filter()
        self.filter_box.currentIndexChanged.connect(self.reload)
        head.addWidget(self.filter_box)
        head.addStretch()
        clean_btn = QPushButton("Очистить исходники")
        clean_btn.setToolTip("Удалить скачанные исходники из downloads/ "
                             "(после этого нельзя будет пересобрать ролики)")
        clean_btn.clicked.connect(self._clean_downloads)
        head.addWidget(clean_btn)
        refresh = QPushButton("↻ Обновить")
        refresh.clicked.connect(self.reload)
        head.addWidget(refresh)
        root.addLayout(head)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self._container = QWidget()
        self._list = QVBoxLayout(self._container)
        self._list.addStretch()
        scroll.setWidget(self._container)
        root.addWidget(scroll, 1)

        self.reload()

    def _fill_channel_filter(self):
        """Заполняет фильтр каналов (вызывается при создании и после правок
        в настройках — см. main_window._reload_channel_lists)."""
        from app import channels
        self.filter_box.addItem("Все", "")
        for ch in channels.CHANNELS:
            self.filter_box.addItem(ch.name, ch.id)

    def reload_channel_filter(self):
        """Перестраивает фильтр после изменения списка каналов, сохраняя
        текущий выбор, если такой канал ещё есть."""
        current = self.filter_box.currentData()
        self.filter_box.blockSignals(True)
        self.filter_box.clear()
        self._fill_channel_filter()
        idx = self.filter_box.findData(current)
        self.filter_box.setCurrentIndex(idx if idx >= 0 else 0)
        self.filter_box.blockSignals(False)
        self.reload()

    def reload(self):
        from app import storage
        # Карточки с незаписанным описанием не трогаем: галерея сама
        # перезагружается при переключении вкладок, и человек, отвлёкшийся
        # на «Работу», возвращался к пустому полю — текст пропадал молча.
        keep = {}
        while self._list.count() > 1:
            item = self._list.takeAt(0)
            w = item.widget()
            if w is None:
                continue
            if isinstance(w, GalleryItem) and w.has_unsaved_desc():
                keep[w.path] = w
            else:
                w.deleteLater()
        want = self.filter_box.currentData()  # "" = все
        files = sorted(OUTPUT.rglob("*.mp4"), key=lambda p: p.stat().st_mtime,
                       reverse=True)
        shown = 0
        for f in files:
            hist = storage.by_output(str(f))
            # разделение по статусу: «Мои ролики» = не запощенные, «Запощено» = да
            is_posted = bool((hist or {}).get("posted", 0))
            if is_posted != self.show_posted:
                continue
            if want and (hist or {}).get("channel_id", "") != want:
                continue
            # кнопку «Пост TG» показываем только если вкладка «Посты TG»
            # вообще существует (см. app/gui/main_window.py — её нет без
            # DATABASE_URL); иначе кнопка была бы мёртвой и молча ничего
            # не делала
            self._list.insertWidget(
                self._list.count() - 1,
                keep.pop(f) if f in keep else
                GalleryItem(f, self._remove, self.on_edit,
                            self._toggle_posted if self.allow_posted else None,
                            self._queue_telegram if self.on_open_tg_post else None),
            )
            shown += 1
        # правки в карточках, отфильтрованных по каналу/статусу, сохранить
        # некуда — но и терять молча нельзя, поэтому записываем их в базу
        for path, widget in keep.items():
            widget._save_desc()
            widget.deleteLater()
        label = "Запощенные" if self.show_posted else "Готовые ролики"
        self.count_label.setText(f"{label}: {shown}")

    def _queue_telegram(self, movie_id, movie_name, btn):
        """Открывает предпросмотр поста во вкладке «Посты TG»."""
        if self.on_open_tg_post:
            self.on_open_tg_post(movie_id, movie_name)

    def _toggle_posted(self, path):
        from app import storage
        # запощено → переносим файл в posted/, снимаем → обратно; путь в БД обновится
        storage.move_posted(str(path), not self.show_posted)
        if self.on_status_change:
            self.on_status_change()  # обновить обе вкладки
        else:
            self.reload()

    def _clean_downloads(self):
        from app.config import DOWNLOADS
        files = list(DOWNLOADS.glob("*.mp4"))
        if not files:
            QMessageBox.information(self, "Исходники", "Папка downloads пуста.")
            return
        ans = QMessageBox.question(
            self, "Очистить исходники?",
            f"Удалить {len(files)} исходников из downloads/?\n"
            "После этого нельзя будет пересобрать (Редактировать) готовые ролики.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if ans != QMessageBox.Yes:
            return
        for f in files:
            f.unlink(missing_ok=True)
        self.reload()

    def _remove(self, item: GalleryItem):
        item.deleteLater()
        # обновляем счётчик после удаления
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, self.reload)
