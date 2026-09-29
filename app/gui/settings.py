"""Вкладка «Настройки»: каналы пользователя + параметры приложения.

Форма нарочно короткая. Всё, что касается конкретного ролика — обрезка,
ползунки уникализации, размытие полей — живёт на вкладке «Работа», а не
здесь: канал это «куда постим», а не «как обработать вот этот ролик».

Личные поля (шапка описания, ставка $/млн, трекинг рекламы, готовая
рамка кропа, логотип, надпись) показываются только в его версии — признак
тот же, что у личных вкладок: заданный DATABASE_URL.
"""
import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from app import channels, downloader, quality, storage
from app.gui import theme
from processors.reels import (
    DEFAULT_WM_POSITION, DEFAULT_WM_SCALE, WATERMARK_POSITIONS,
)

PERSONAL = bool(os.getenv("DATABASE_URL"))

# Фиксированные ширины: без них QFormLayout тянет подписи по самой длинной
# строке, и поля выстраиваются «лесенкой» — разной ширины и с разных мест.
LABEL_W = 110
FIELD_W = 280
CARD_W = 460


class SettingsTab(QWidget):
    def __init__(self, on_channels_changed=None):
        super().__init__()
        self.on_channels_changed = on_channels_changed
        self._current_id: str | None = None
        self._loading = False
        self._crop_raw = ""

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(10)

        head = QLabel("<b>Каналы</b>")
        head.setStyleSheet("font-size:16px;")
        root.addWidget(head)
        hint = QLabel("Куда вы постите ролики. Обрезка настраивается для "
                      "каждого ролика на вкладке «Работа». Настройки самой "
                      "программы — внизу страницы.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{theme.TEXT_MUTED};font-size:11px;")
        root.addWidget(hint)

        body = QHBoxLayout()
        body.setSpacing(16)
        root.addLayout(body, 1)

        # ── слева: список каналов ────────────────────────────────────────
        left = QVBoxLayout()
        left.setSpacing(6)
        self.list = QListWidget()
        self.list.setFixedWidth(190)
        self.list.currentItemChanged.connect(self._on_select)
        # без рамки пустой список выглядел дырой в фоне
        self.list.setStyleSheet(f"""
            QListWidget {{
                background: {theme.SURFACE};
                border: 1px solid {theme.BORDER};
                border-radius: {theme.RADIUS}px;
                padding: 4px;
            }}
            QListWidget::item {{
                padding: 7px 8px;
                border-radius: {theme.RADIUS_SM}px;
            }}
            QListWidget::item:selected {{
                background: {theme.ACCENT};
                color: white;
            }}
        """)
        left.addWidget(self.list, 1)
        # подсказка поверх пустого списка — чтобы человек понял, что делать
        self.empty_hint = QLabel("Каналов пока нет.\nНажмите «+ Добавить».")
        self.empty_hint.setAlignment(Qt.AlignCenter)
        self.empty_hint.setWordWrap(True)
        self.empty_hint.setParent(self.list)
        self.empty_hint.setStyleSheet(
            f"color:{theme.TEXT_MUTED};font-size:11px;background:transparent;")
        btns = QHBoxLayout()
        btns.setSpacing(6)
        add_btn = QPushButton("+ Добавить")
        add_btn.clicked.connect(self._add_channel)
        self.del_btn = QPushButton("Удалить")
        self.del_btn.setStyleSheet(f"color:{theme.DANGER};")
        self.del_btn.clicked.connect(self._delete_channel)
        btns.addWidget(add_btn)
        btns.addWidget(self.del_btn)
        left.addLayout(btns)
        body.addLayout(left)

        # ── справа: карточка канала ──────────────────────────────────────
        # Обёртка со стретчами по бокам — карточка держит свою ширину и стоит
        # по центру свободного места, а не растягивается на всю вкладку.
        card_area = QVBoxLayout()
        card_area.setSpacing(0)
        card_area.addStretch(1)
        card_row = QHBoxLayout()
        card_row.addStretch(1)

        self.form_frame = QFrame()
        self.form_frame.setObjectName("channelCard")
        self.form_frame.setFixedWidth(CARD_W)
        self.form_frame.setStyleSheet(f"""
            QFrame#channelCard {{
                background: {theme.SURFACE};
                border: 1px solid {theme.BORDER};
                border-radius: {theme.RADIUS}px;
            }}
        """)
        outer = QVBoxLayout(self.form_frame)
        outer.setContentsMargins(24, 22, 24, 22)
        outer.setSpacing(10)

        self.card_title = QLabel("Новый канал")
        self.card_title.setStyleSheet("font-size:15px;font-weight:600;")
        self.card_title.setAlignment(Qt.AlignCenter)
        outer.addWidget(self.card_title)
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet(f"color:{theme.BORDER};")
        outer.addWidget(line)
        outer.addSpacing(4)

        form = _new_form()
        outer.addLayout(form)

        self.f_name = QLineEdit()
        self.f_name.setPlaceholderText("@my_channel")
        self.f_name.setFixedWidth(FIELD_W)
        form.addRow(_label("Название:"), self.f_name)

        self.f_id = QLineEdit()
        self.f_id.setPlaceholderText("заполнится само")
        self.f_id.setFixedWidth(FIELD_W)
        self.f_id.setToolTip("Имя папки, куда складываются готовые ролики. "
                             "У созданного канала не меняется.")
        form.addRow(_label("Папка:"), self.f_id)

        folder_hint = QLabel("папка с готовыми роликами, латиницей")
        folder_hint.setStyleSheet(f"color:{theme.TEXT_MUTED};font-size:11px;")
        form.addRow(_label(""), folder_hint)

        # ── личные поля ────────────────────────────────────────────
        if PERSONAL:
            outer.addSpacing(6)
            sub = QLabel("<b>Личное (только в твоей версии)</b>")
            sub.setStyleSheet(f"font-size:13px;color:{theme.TEXT_MUTED};")
            outer.addWidget(sub)
            mine = _new_form()
            outer.addLayout(mine)

            self.f_desc_header = QCheckBox("Добавлять шапку к описанию")
            mine.addRow(_label("Описание:"), self.f_desc_header)

            self.f_signature = QLineEdit()
            self.f_signature.setFixedWidth(FIELD_W)
            self.f_signature.setPlaceholderText("текст в конце описания")
            mine.addRow(_label("Подпись:"), self.f_signature)

            self.f_payout = QDoubleSpinBox()
            self.f_payout.setRange(0, 10000)
            self.f_payout.setDecimals(2)
            self.f_payout.setPrefix("$ ")
            self.f_payout.setFixedWidth(110)
            mine.addRow(_label("Ставка /1 млн:"), self.f_payout)

            self.f_ad_tracked = QCheckBox("Трекать сдачу статистики")
            mine.addRow(_label("Реклама:"), self.f_ad_tracked)

            wm_row = QHBoxLayout()
            wm_row.setSpacing(6)
            self.f_use_watermark = QCheckBox("Ставить")
            self.f_use_watermark.toggled.connect(self._on_watermark_toggled)
            self.f_watermark = QLineEdit()
            self.f_watermark.setPlaceholderText("файл PNG")
            self.f_watermark.setReadOnly(True)
            self.wm_pick = QPushButton("Выбрать…")
            self.wm_pick.clicked.connect(self._pick_watermark)
            wm_row.addWidget(self.f_use_watermark)
            wm_row.addWidget(self.f_watermark, 1)
            wm_row.addWidget(self.wm_pick)
            mine.addRow(_label("Логотип:"), _wrap(wm_row, FIELD_W))

            look = QHBoxLayout()
            look.setSpacing(6)
            self.f_wm_scale = QSpinBox()
            self.f_wm_scale.setRange(5, 100)
            self.f_wm_scale.setSuffix(" %")
            self.f_wm_scale.setFixedWidth(80)
            self.f_wm_pos = QComboBox()
            for key, (label, _e) in WATERMARK_POSITIONS.items():
                self.f_wm_pos.addItem(label, key)
            look.addWidget(self.f_wm_scale)
            look.addWidget(self.f_wm_pos, 1)
            self.wm_look_row = _wrap(look, FIELD_W)
            mine.addRow(_label(""), self.wm_look_row)

            self.f_use_hook = QCheckBox("Наносить")
            mine.addRow(_label("Надпись:"), self.f_use_hook)

            crop_row = QHBoxLayout()
            crop_row.setSpacing(6)
            self.f_crop = QLineEdit()
            self.f_crop.setPlaceholderText("пусто — определять автоматически")
            self.f_crop.setReadOnly(True)
            crop_clear = QPushButton("✕")
            crop_clear.setFixedWidth(28)
            crop_clear.clicked.connect(self._clear_crop)
            crop_row.addWidget(self.f_crop, 1)
            crop_row.addWidget(crop_clear)
            mine.addRow(_label("Готовая рамка:"), _wrap(crop_row, FIELD_W))

        outer.addSpacing(6)
        save_row = QHBoxLayout()
        save_row.addStretch(1)
        self.save_btn = QPushButton("Сохранить")
        self.save_btn.setObjectName("primary")
        self.save_btn.setFixedWidth(140)
        self.save_btn.clicked.connect(self._save_channel)
        save_row.addWidget(self.save_btn)
        save_row.addStretch(1)
        outer.addLayout(save_row)

        card_row.addWidget(self.form_frame)
        card_row.addStretch(1)
        card_area.addLayout(card_row)
        card_area.addStretch(1)
        body.addLayout(card_area, 1)

        # ── Настройки программы ─────────────────────────────────────────
        # Всё, что ниже, относится к программе целиком, а не к отдельному
        # каналу. Раньше эти поля висели внизу россыпью и выглядели как
        # продолжение карточки канала — теперь собраны под своим заголовком
        # и выровнены по одной сетке.
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet(f"color:{theme.BORDER};")
        root.addWidget(sep)

        app_head = QLabel("<b>Настройки программы</b>")
        app_head.setStyleSheet("font-size:14px;")
        root.addWidget(app_head)
        app_hint = QLabel("Действуют на все каналы и все ролики.")
        app_hint.setStyleSheet(f"color:{theme.TEXT_MUTED};font-size:11px;")
        root.addWidget(app_hint)

        app_form = QFormLayout()
        app_form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        app_form.setFormAlignment(Qt.AlignLeft | Qt.AlignTop)
        app_form.setFieldGrowthPolicy(QFormLayout.FieldsStayAtSizeHint)
        app_form.setHorizontalSpacing(12)
        app_form.setVerticalSpacing(10)

        # качество готового ролика
        self.f_quality = QComboBox()
        self.f_quality.setFixedWidth(300)
        for key, label in quality.levels():
            self.f_quality.addItem(label, key)
        i = self.f_quality.findData(quality.current())
        if i >= 0:
            self.f_quality.setCurrentIndex(i)
        self.f_quality.currentIndexChanged.connect(self._save_quality)
        self.f_quality.setToolTip(
            "Размер готового ролика, который вы выкладываете.\n"
            "Ролик всегда СКАЧИВАЕТСЯ в лучшем качестве — эта настройка\n"
            "влияет только на то, каким он собирается на выходе.\n"
            "Меньше размер — легче файл и быстрее обработка."
        )
        app_form.addRow(_app_label("Качество роликов:"), self.f_quality)

        # пресет уникализации по умолчанию
        self.f_preset = QComboBox()
        self.f_preset.setFixedWidth(300)
        self.f_preset.currentIndexChanged.connect(self._save_default_preset)
        self.f_preset.setToolTip(
            "Набор ползунков, который сам применяется к каждому новому ролику.\n"
            "Наборы сохраняются на «Работе» кнопкой «Сохранить» рядом с ползунками.\n"
            "«Без уникализации» — ползунки в нейтрали, ролик не меняется."
        )
        app_form.addRow(_app_label("Уникализация:"), self.f_preset)

        # браузер для кук Instagram
        self.f_browser = QComboBox()
        self.f_browser.setFixedWidth(300)
        for b in downloader.COOKIE_BROWSERS:
            self.f_browser.addItem(b.capitalize(), b)
        i = self.f_browser.findData(downloader.cookie_browser())
        if i >= 0:
            self.f_browser.setCurrentIndex(i)
        self.f_browser.currentIndexChanged.connect(self._save_browser)
        self.f_browser.setToolTip(
            "Ролики из Instagram скачиваются, только если вы вошли в Instagram "
            "в этом браузере. Для YouTube и других сайтов вход не нужен."
        )
        app_form.addRow(_app_label("Вход в Instagram:"), self.f_browser)

        root.addLayout(app_form)
        self._reload_presets()

        self.reload()

    # --- список ---

    def reload(self):
        channels.reload()
        self.list.blockSignals(True)
        self.list.clear()
        for ch in channels.CHANNELS:
            item = QListWidgetItem(ch.name)
            item.setData(Qt.UserRole, ch.id)
            self.list.addItem(item)
        self.list.blockSignals(False)
        self._sync_empty_hint()
        if self.list.count():
            self.list.setCurrentRow(0)
        else:
            self._clear_form()
            self._set_form_enabled(False)

    def _sync_empty_hint(self):
        """Подсказка видна только когда список каналов пуст."""
        empty = self.list.count() == 0
        self.empty_hint.setVisible(empty)
        if empty:
            self.empty_hint.setGeometry(6, 12, self.list.width() - 12, 60)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_empty_hint()

    def _on_select(self, current, _previous):
        if current is not None:
            self._load_channel(current.data(Qt.UserRole))

    def _load_channel(self, cid: str):
        try:
            ch = channels.by_id(cid)
        except KeyError:
            self._clear_form()
            return
        self._loading = True
        self._current_id = ch.id
        self.card_title.setText(ch.name or ch.id)
        self.f_id.setText(ch.id)
        self.f_id.setReadOnly(True)   # папка уже создана, ролики лежат в ней
        self.f_name.setText(ch.name)

        if PERSONAL:
            self.f_desc_header.setChecked(ch.desc_header)
            self.f_signature.setText(ch.desc_signature)
            self.f_payout.setValue(ch.payout_per_million)
            self.f_ad_tracked.setChecked(ch.ad_tracked)
            has_wm = (ch.use_watermark if ch.use_watermark is not None
                      else (ch.preset == "reels"))
            self.f_use_watermark.setChecked(bool(has_wm))
            self.f_watermark.setText(ch.watermark_path)
            self.f_wm_scale.setValue(ch.watermark_scale or DEFAULT_WM_SCALE)
            i = self.f_wm_pos.findData(ch.watermark_position or "legacy")
            self.f_wm_pos.setCurrentIndex(i if i >= 0 else 0)
            self.f_use_hook.setChecked(ch.use_hook)
            box = ch.crop_box()
            self.f_crop.setText(
                f"{box[0]}×{box[1]}, отступ {box[2]}:{box[3]}" if box else "")
        self._crop_raw = ch.fixed_crop
        self._loading = False
        self._set_form_enabled(True)
        if PERSONAL:
            self._on_watermark_toggled(self.f_use_watermark.isChecked())

    def _clear_form(self):
        self._loading = True
        self._current_id = None
        self._crop_raw = ""
        self.card_title.setText("Новый канал")
        self.f_id.clear()
        self.f_id.setReadOnly(False)
        self.f_name.clear()
        if PERSONAL:
            self.f_desc_header.setChecked(False)
            self.f_signature.clear()
            self.f_payout.setValue(0)
            self.f_ad_tracked.setChecked(False)
            self.f_use_watermark.setChecked(False)
            self.f_watermark.clear()
            self.f_wm_scale.setValue(DEFAULT_WM_SCALE)
            i = self.f_wm_pos.findData(DEFAULT_WM_POSITION)
            self.f_wm_pos.setCurrentIndex(i if i >= 0 else 0)
            self.f_use_hook.setChecked(False)
            self.f_crop.clear()
        self._loading = False
        if PERSONAL:
            self._on_watermark_toggled(False)

    def _set_form_enabled(self, on: bool):
        self.form_frame.setEnabled(on)
        self.del_btn.setEnabled(on and self._current_id is not None)

    def _on_watermark_toggled(self, on: bool):
        if not PERSONAL:
            return
        self.f_watermark.setEnabled(on)
        self.wm_pick.setEnabled(on)
        self.wm_look_row.setEnabled(on)

    # --- действия ---

    def _add_channel(self):
        self.list.setCurrentItem(None)
        self._clear_form()
        self._set_form_enabled(True)
        self.f_id.setReadOnly(False)
        self.f_name.setFocus()

    def _save_channel(self):
        name = self.f_name.text().strip()
        cid = self.f_id.text().strip()
        if not name and not cid:
            QMessageBox.warning(self, "Пустой канал", "Впишите название канала.")
            return
        # имя папки можно не придумывать — делаем из названия
        if not cid:
            cid = _slug(name)
        if not cid:
            QMessageBox.warning(
                self, "Нужно имя папки",
                "В названии нет латинских букв — впишите имя папки вручную, "
                "например my_channel.")
            return
        if not _valid_id(cid):
            QMessageBox.warning(self, "Неподходящее имя папки",
                                "Только латинские буквы, цифры, _ и -.")
            return
        if self._current_id is None and channels.exists(cid):
            QMessageBox.warning(self, "Уже есть", f"Канал «{cid}» уже заведён.")
            return

        use_hook = PERSONAL and self.f_use_hook.isChecked()
        row = dict(
            id=cid,
            name=name or cid,
            preset=channels.preset_for("black", use_hook),
            source="youtube",     # площадка определяется по самой ссылке
            background="black",
            use_hook=int(use_hook),
            fixed_crop=self._crop_raw,
            sort_order=self._sort_order_for(cid),
        )
        if PERSONAL:
            row.update(
                use_watermark=int(self.f_use_watermark.isChecked()),
                watermark_path=self.f_watermark.text().strip(),
                watermark_scale=self.f_wm_scale.value(),
                watermark_position=self.f_wm_pos.currentData(),
                desc_header=int(self.f_desc_header.isChecked()),
                desc_signature=self.f_signature.text().strip(),
                payout_per_million=self.f_payout.value(),
                ad_tracked=int(self.f_ad_tracked.isChecked()),
            )
        else:
            row["use_watermark"] = 0
        # У существующего канала пресет и фон не перебиваем: форма про фон
        # больше не спрашивает (он выбирается на «Работе»), а в личной версии каналы
        # могут быть настроены на размытый фон — сохранение имени не должно
        # это сбрасывать.
        if self._current_id is not None:
            try:
                existing = channels.by_id(cid)
                row["preset"] = existing.preset
                row["background"] = existing.background
            except KeyError:
                pass
        storage.channel_save(row)
        self._current_id = cid
        self.reload()
        self._select_in_list(cid)
        if self.on_channels_changed:
            self.on_channels_changed()

    def _sort_order_for(self, cid: str) -> int:
        try:
            return channels.by_id(cid).sort_order
        except KeyError:
            return len(channels.CHANNELS)

    def _select_in_list(self, cid: str):
        for i in range(self.list.count()):
            if self.list.item(i).data(Qt.UserRole) == cid:
                self.list.setCurrentRow(i)
                return

    def _delete_channel(self):
        if not self._current_id:
            return
        cid = self._current_id
        has_videos = cid in storage.history_channel_ids()
        extra = "\n\nГотовые ролики останутся в галерее." if has_videos else ""
        ans = QMessageBox.question(self, "Удалить канал?", f"Удалить «{cid}»?{extra}",
                                   QMessageBox.Yes | QMessageBox.No)
        if ans != QMessageBox.Yes:
            return
        storage.channel_delete(cid)
        self.reload()
        if self.on_channels_changed:
            self.on_channels_changed()

    def _pick_watermark(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Логотип канала", "", "Изображения (*.png *.PNG)")
        if path:
            self.f_watermark.setText(path)

    def _clear_crop(self):
        self._crop_raw = ""
        self.f_crop.clear()

    def _reload_presets(self):
        """Список сохранённых пресетов + текущий выбор из настроек."""
        from app import storage
        self.f_preset.blockSignals(True)
        self.f_preset.clear()
        self.f_preset.addItem("Без уникализации", "")
        for row in storage.uniq_presets():
            self.f_preset.addItem(row["name"], row["name"])
        i = self.f_preset.findData(storage.kv_get("default_uniq_preset", ""))
        self.f_preset.setCurrentIndex(i if i >= 0 else 0)
        self.f_preset.blockSignals(False)

    def _save_default_preset(self):
        from app import storage
        storage.kv_set("default_uniq_preset", self.f_preset.currentData() or "")

    def _save_browser(self):
        storage.kv_set("cookie_browser", self.f_browser.currentData())

    def _save_quality(self):
        """Качество готового ролика. Применяется со следующего рендера —
        уже готовые ролики не пересобираются."""
        quality.set_current(self.f_quality.currentData())


def _new_form() -> QFormLayout:
    """Форма с одинаковой сеткой: подписи справа, поля от одной вертикали."""
    f = QFormLayout()
    f.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
    f.setFormAlignment(Qt.AlignLeft | Qt.AlignTop)
    f.setFieldGrowthPolicy(QFormLayout.FieldsStayAtSizeHint)
    f.setHorizontalSpacing(12)
    f.setVerticalSpacing(8)
    return f


# Подписи настроек программы длиннее, чем в карточке канала («Вход в
# Instagram:» не влезал в LABEL_W и обрезался слева).
APP_LABEL_W = 150


def _app_label(text: str) -> QLabel:
    """Подпись в блоке «Настройки программы»."""
    lbl = QLabel(text)
    lbl.setFixedWidth(APP_LABEL_W)
    lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
    return lbl


def _label(text: str) -> QLabel:
    """Подпись поля фиксированной ширины — иначе поля разъезжаются лесенкой."""
    lbl = QLabel(text)
    lbl.setFixedWidth(LABEL_W)
    lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
    return lbl


def _wrap(layout, width: int | None = None) -> QWidget:
    w = QWidget()
    layout.setContentsMargins(0, 0, 0, 0)
    w.setLayout(layout)
    if width:
        w.setFixedWidth(width)
    return w


def _slug(name: str) -> str:
    """«@My Channel» → «my_channel». Пусто, если латиницы в названии нет."""
    out = []
    for ch in name.strip().lower():
        if ch.isascii() and (ch.isalnum() or ch in "_-"):
            out.append(ch)
        elif ch in " .":
            out.append("_")
    return "".join(out).strip("_-")


def _valid_id(cid: str) -> bool:
    return bool(cid) and cid.isascii() and all(c.isalnum() or c in "_-" for c in cid)
