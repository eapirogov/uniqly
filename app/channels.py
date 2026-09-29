"""Профили каналов: пресет уникализации + источник + нужен ли хук.

Каналы лежат в базе (таблица channels, см. app/storage.py) — раньше это был
жёсткий список прямо здесь. Наружу интерфейс не изменился: как и прежде, всё
приложение ходит через CHANNELS и by_id(), поэтому вызывающий код (pipeline,
main_window, process_panel, gallery, stats) трогать не пришлось.

CHANNELS — не обычный список, а «живой» объект: он перечитывает базу, когда
её меняют через экран настроек. Так `for ch in channels.CHANNELS` в уже
написанном коде продолжает работать и при этом видит свежие данные.
"""
import json
from dataclasses import dataclass, asdict

from app import storage
from processors import tiktok, reels, crop

# реестр пресетов: имя → модуль с uniqueize(input, output, caption=...)
PRESETS = {
    "tiktok": tiktok,
    "reels": reels,
    "crop": crop,
}

# --- перевод «понятных настроек» ↔ пресет ------------------------------------
# Человеку показываем два выбора: чем заполнить поля по краям и ставить ли
# логотип. Внутри это по-прежнему три пресета в processors/ — таблица ниже
# переводит одно в другое, чтобы кодовые слова reels/crop/tiktok не вылезали
# в интерфейс.

def _background_of(preset: str) -> str:
    """Каким фоном заполняет поля этот пресет."""
    return "blur" if preset == "reels" else "black"


def preset_for(background: str, use_hook: bool) -> str:
    """Настройки пользователя → имя пресета в processors/.

    размытый фон            → reels  (он же единственный с вотермарком)
    чёрные поля + надпись   → tiktok
    чёрные поля             → crop
    """
    if background == "blur":
        return "reels"
    return "tiktok" if use_hook else "crop"


BACKGROUND_LABELS = {
    "black": "Чёрные",
    "blur": "Размытое видео",
}



@dataclass
class Channel:
    id: str
    name: str            # @username
    preset: str          # ключ из PRESETS
    source: str          # "youtube" | "instagram" — откуда качаем
    use_hook: bool       # генерить и наносить хук-надпись
    desc_header: bool    # добавлять шапку "📹Телеграмм-канал…"
    desc_signature: str  # фикс. подпись в конце ("" = нет)
    ad_tracked: bool = False  # трекать сдачу рекламной статы (баннеры-реклама)
    payout_per_million: float = 0.0  # $ за 1 млн просмотров (для расчёта заработка)
    watermark_path: str = ""  # свой вотермарк ("" = без вотермарка)
    fixed_crop: str = ""      # JSON [w,h,x,y] — заданная рамка ("" = автодетект)
    sort_order: int = 0
    # Что человек реально выбирает в настройках (вместо кодовых имён пресетов):
    #   background    — чем заполнять поля по краям: "black" | "blur"
    #   use_watermark — ставить ли логотип (None = решает пресет, как раньше)
    background: str = ""
    use_watermark: bool | None = None
    watermark_scale: int = 0      # % ширины кадра (0 = как было раньше)
    watermark_position: str = ""  # см. processors.reels.WATERMARK_POSITIONS
    blur_strength: int = 0        # сила размытия полей (0 = по умолчанию)

    def crop_box(self) -> tuple[int, int, int, int] | None:
        """fixed_crop как (w,h,x,y) либо None, если рамка не задана.

        Заменяет прежние проверки конкретного id канала, зашитые в
        process_panel/batch_dialog: теперь «есть ли у канала готовая рамка» —
        свойство самого канала, а не список id в коде.
        """
        if not self.fixed_crop:
            return None
        try:
            w, h, x, y = json.loads(self.fixed_crop)
            return (int(w), int(h), int(x), int(y))
        except (ValueError, TypeError, json.JSONDecodeError):
            return None


def _tri_state(value) -> bool | None:
    """-1/None в базе → None («не задано»), 0 → False, всё прочее → True."""
    if value is None:
        return None
    value = int(value)
    return None if value < 0 else bool(value)


def _from_row(row: dict) -> Channel:
    """Строка БД → Channel (числа/флаги приводим к нужным типам)."""
    return Channel(
        id=row["id"],
        name=row["name"] or row["id"],
        preset=row["preset"] or "crop",
        source=row["source"] or "youtube",
        use_hook=bool(row["use_hook"]),
        desc_header=bool(row["desc_header"]),
        desc_signature=row["desc_signature"] or "",
        ad_tracked=bool(row["ad_tracked"]),
        payout_per_million=float(row["payout_per_million"] or 0),
        watermark_path=row["watermark_path"] or "",
        fixed_crop=row["fixed_crop"] or "",
        sort_order=int(row["sort_order"] or 0),
        background=row["background"] or _background_of(row["preset"] or "crop"),
        # -1 в базе = «не задано», решает пресет (так заведены каналы)
        # ВАЖНО: не писать `row[...] or -1` — 0 («логотип не ставить») в
        # Python ложное значение, и явное «нет» превращалось бы в «решай сам»,
        # из-за чего подхватывался общий watermark.png
        use_watermark=_tri_state(row["use_watermark"]),
        watermark_scale=int(row["watermark_scale"] or 0),
        watermark_position=row["watermark_position"] or "",
        blur_strength=int(row["blur_strength"] or 0),
    )


class _ChannelRegistry(list):
    """Список каналов, который сам подтягивается из базы.

    Наследуемся от list, чтобы весь существующий код (`for ch in CHANNELS`,
    `len()`, срезы, enumerate) работал без единой правки. Единственное
    отличие — содержимое обновляется вызовом reload() после правок в
    настройках, а не остаётся замороженным с момента импорта.
    """

    def reload(self) -> None:
        self[:] = [_from_row(r) for r in storage.channels_list()]


CHANNELS = _ChannelRegistry()
CHANNELS.reload()


def reload() -> None:
    """Перечитать каналы из базы (после сохранения/удаления в настройках)."""
    CHANNELS.reload()


def by_id(cid: str) -> Channel:
    """Канал по id.

    Раньше падал невнятным StopIteration от next() — теперь говорит, что
    именно не найдено. Это стало важнее: канал можно удалить в настройках,
    а в истории роликов его id останется (см. storage.channel_delete).
    """
    for c in CHANNELS:
        if c.id == cid:
            return c
    # база могла измениться в другом месте программы — перечитаем и повторим,
    # прежде чем сдаваться
    CHANNELS.reload()
    for c in CHANNELS:
        if c.id == cid:
            return c
    # Тип остаётся KeyError: его ловят девять мест (gallery, settings,
    # process_panel, main_window) и подставляют запасной канал — RuntimeError
    # прошёл бы мимо этих except и превратил бы штатную ситуацию в падение.
    # Меняем только текст: он может дойти до карточки ролика.
    raise KeyError(
        f"Канал «{cid}», для которого делался этот ролик, удалён. "
        "Выберите другой канал в списке наверху."
    )


def exists(cid: str) -> bool:
    """Есть ли такой канал (без исключения) — для мест, где id пришёл из
    истории и канал мог быть удалён."""
    return any(c.id == cid for c in CHANNELS)


def as_dict(ch: Channel) -> dict:
    """Channel → dict для storage.channel_save()."""
    return asdict(ch)


def initial_crop(channel_id: str, src) -> tuple[tuple[int, int, int, int] | None, bool]:
    """Стартовая рамка кропа для канала: (рамка, уверены ли в ней).

    Если у канала задан fixed_crop — берём его (так работает канал со
    своим INSTA_CROP, только теперь это настройка канала, а не «если id равен
    строке» в трёх местах интерфейса). Иначе — автодетект чёрных полос.

    Ошибки автодетекта не глушим: вызывающий код и раньше показывал их
    пользователю через set_failed/«не пересчитан».
    """
    from processors import cropdetect

    try:
        ch = by_id(channel_id)
    except KeyError:
        ch = None
    if ch is not None:
        box = ch.crop_box()
        if box:
            return box, True
    detected = cropdetect.detect(src)
    return (detected.as_tuple() if detected else None,
            detected.confident if detected else False)
