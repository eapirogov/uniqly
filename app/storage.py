"""История обработанных роликов: SQLite (workspace/app_state.db).

Хранит связь исходник↔готовый файл↔канал↔хук↔описание для пересборки из галереи.
"""
import json
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from app.config import DB_PATH


def backup_db(keep: int = 20) -> Path | None:
    """Копирует app_state.db в workspace/backups/ с датой. Хранит последние keep."""
    if not DB_PATH.exists():
        return None
    backups = DB_PATH.parent / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    dst = backups / f"app_state_{stamp}.db"
    try:
        shutil.copy2(DB_PATH, dst)
    except OSError:
        return None
    # ротация: удаляем самые старые сверх лимита
    files = sorted(backups.glob("app_state_*.db"), key=lambda p: p.stat().st_mtime)
    for old in files[:-keep]:
        old.unlink(missing_ok=True)
    return dst


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT,
            channel_id TEXT,
            source_path TEXT,
            output_path TEXT,
            hook TEXT,
            hook_base TEXT,
            description TEXT,
            movie_name TEXT,
            created_at REAL
        )
    """)
    # отдельная таблица статистики (не связана с роликами-файлами):
    # вручную: канал + название + ссылка + дата публикации + просмотры
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ad_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT,
            title TEXT,
            ig_url TEXT,
            posted_at REAL,
            views INTEGER DEFAULT 0,
            submitted INTEGER DEFAULT 0,
            created_at REAL
        )
    """)
    # миграции: добавить недостающие столбцы к существующей таблице
    cols = [r[1] for r in conn.execute("PRAGMA table_info(history)").fetchall()]
    if "movie_name" not in cols:
        conn.execute("ALTER TABLE history ADD COLUMN movie_name TEXT DEFAULT ''")
    if "posted" not in cols:
        conn.execute("ALTER TABLE history ADD COLUMN posted INTEGER DEFAULT 0")
    if "movie_id" not in cols:
        conn.execute("ALTER TABLE history ADD COLUMN movie_id TEXT DEFAULT ''")
    # трекинг рекламной статистики
    if "posted_at" not in cols:
        conn.execute("ALTER TABLE history ADD COLUMN posted_at REAL")
    if "ig_url" not in cols:
        conn.execute("ALTER TABLE history ADD COLUMN ig_url TEXT DEFAULT ''")
    if "ad_code" not in cols:
        conn.execute("ALTER TABLE history ADD COLUMN ad_code TEXT DEFAULT ''")
    if "stat_submitted" not in cols:
        conn.execute("ALTER TABLE history ADD COLUMN stat_submitted INTEGER DEFAULT 0")
    if "views" not in cols:
        conn.execute("ALTER TABLE history ADD COLUMN views INTEGER DEFAULT 0")
    # ручная разметка кропа + слайдеры уникализации + баннер (app/gui/process_panel.py)
    # — JSON-строки, сериализация/десериализация на стороне pipeline.py, чтобы
    # storage не был завязан на конкретный формат данных
    if "crop_rect" not in cols:
        conn.execute("ALTER TABLE history ADD COLUMN crop_rect TEXT DEFAULT ''")
    if "uniq_params" not in cols:
        conn.execute("ALTER TABLE history ADD COLUMN uniq_params TEXT DEFAULT ''")
    if "banner_path" not in cols:
        conn.execute("ALTER TABLE history ADD COLUMN banner_path TEXT DEFAULT ''")
    # kv-хранилище для мелких настроек (счётчик чередования баннеров и т.п.)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS kv (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    # библиотека пользовательских баннеров (app/gui/process_panel.py) — файлы
    # копируются в processors/banner.library_dir(), путь + имя хранятся тут
    conn.execute("""
        CREATE TABLE IF NOT EXISTS banners (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            path TEXT,
            created_at REAL
        )
    """)
    # заготовки описаний: человек пишет свой текст один раз и вставляет его
    # к описанию фильма, не набирая каждый раз заново
    conn.execute("""
        CREATE TABLE IF NOT EXISTS desc_templates (
            name TEXT PRIMARY KEY,
            text TEXT,
            created_at REAL
        )
    """)
    # пресеты уникализации: человек выставляет ползунки под себя один раз и
    # применяет тот же набор к следующим роликам, не двигая всё заново.
    # params — JSON вида uniq.make_params() (см. processors/uniq.py)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS uniq_presets (
            name TEXT PRIMARY KEY,
            params TEXT,
            created_at REAL
        )
    """)
    # каналы пользователя (раньше — жёсткий список в app/channels.py).
    # Поля повторяют dataclass Channel плюс три новых:
    #   watermark_path — свой вотермарк на канал (был один общий на всех)
    #   fixed_crop     — заданная рамка кропа, JSON [w,h,x,y] ('' = автодетект);
    #                    заменяет проверки конкретного id канала
    #   sort_order     — порядок в выпадающих списках
    conn.execute("""
        CREATE TABLE IF NOT EXISTS channels (
            id TEXT PRIMARY KEY,
            name TEXT,
            preset TEXT,
            source TEXT,
            use_hook INTEGER DEFAULT 0,
            desc_header INTEGER DEFAULT 0,
            desc_signature TEXT DEFAULT '',
            ad_tracked INTEGER DEFAULT 0,
            payout_per_million REAL DEFAULT 0,
            watermark_path TEXT DEFAULT '',
            fixed_crop TEXT DEFAULT '',
            sort_order INTEGER DEFAULT 0
        )
    """)
    # поля, которые видит обычный пользователь вместо кодовых имён пресетов:
    #   background — чем заполнять поля по краям: 'black' | 'blur'
    #   use_watermark — ставить ли логотип
    # Пресет (reels/crop/tiktok) вычисляется из них — см. app/channels.py.
    ch_cols = [r[1] for r in conn.execute("PRAGMA table_info(channels)").fetchall()]
    if "background" not in ch_cols:
        conn.execute("ALTER TABLE channels ADD COLUMN background TEXT DEFAULT ''")
    if "use_watermark" not in ch_cols:
        conn.execute("ALTER TABLE channels ADD COLUMN use_watermark INTEGER DEFAULT -1")
    # размер (% ширины кадра) и место логотипа — у каждого свой файл и свои
    # привычки; 0/'' = прежнее поведение (во всю ширину, как раньше)
    if "watermark_scale" not in ch_cols:
        conn.execute("ALTER TABLE channels ADD COLUMN watermark_scale INTEGER DEFAULT 0")
    if "watermark_position" not in ch_cols:
        conn.execute("ALTER TABLE channels ADD COLUMN watermark_position TEXT DEFAULT ''")
    # сила размытия полей по краям (0 = взять значение по умолчанию)
    if "blur_strength" not in ch_cols:
        conn.execute("ALTER TABLE channels ADD COLUMN blur_strength INTEGER DEFAULT 0")
    return conn


def kv_get(key: str, default: str = "") -> str:
    """Читает значение из kv-хранилища."""
    conn = _conn()
    try:
        row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default
    finally:
        conn.close()


def kv_set(key: str, value: str) -> None:
    """Пишет значение в kv-хранилище."""
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
        conn.commit()
    finally:
        conn.close()


def next_banner_index(channel_id: str, count: int = 2) -> int:
    """Возвращает индекс баннера [0..count-1] для канала и продвигает счётчик.

    Чередование отдельное на канал: каждый вызов даёт следующий баннер по кругу.
    """
    key = f"banner_idx_{channel_id}"
    idx = int(kv_get(key, "0") or "0") % count
    kv_set(key, str((idx + 1) % count))
    return idx


def move_posted(output_path: str, posted: bool) -> str:
    """Перемещает файл в/из подпапки posted/ и обновляет БД. Возвращает новый путь.

    posted=True:  output/<канал>/файл.mp4 → output/<канал>/posted/файл.mp4
    posted=False: output/<канал>/posted/файл.mp4 → output/<канал>/файл.mp4
    """
    src = Path(output_path)
    if posted:
        dst = src.parent / "posted" / src.name
    else:
        # снять пометку: если файл в posted/ — поднять на уровень канала
        dst = src.parent.parent / src.name if src.parent.name == "posted" else src
    if dst != src and src.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
    # обновляем статус и путь (статистика теперь отдельная таблица ad_stats)
    conn = _conn()
    try:
        conn.execute(
            "UPDATE history SET posted = ?, output_path = ? WHERE output_path = ?",
            (1 if posted else 0, str(dst), output_path),
        )
        conn.commit()
    finally:
        conn.close()
    return str(dst)


# --- Рекламная статистика (отдельная таблица ad_stats, ручной ввод) ---

def ad_stats_all() -> list[dict]:
    """Все записи статистики (ручной ввод)."""
    conn = _conn()
    try:
        rows = conn.execute("SELECT * FROM ad_stats ORDER BY posted_at ASC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def ad_stats_add(channel_id: str, title: str, ig_url: str, posted_at: float,
                 views: int = 0) -> int:
    """Добавляет запись статистики. Возвращает id."""
    conn = _conn()
    try:
        cur = conn.execute(
            "INSERT INTO ad_stats (channel_id, title, ig_url, posted_at, views, "
            "submitted, created_at) VALUES (?,?,?,?,?,0,?)",
            (channel_id, title, ig_url, posted_at, views, time.time()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def ad_stats_update(stat_id: int, **fields) -> None:
    """Обновляет поля записи (channel_id/title/ig_url/posted_at/views/submitted)."""
    allowed = {"channel_id", "title", "ig_url", "posted_at", "views", "submitted"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    cols = ", ".join(f"{k} = ?" for k in sets)
    conn = _conn()
    try:
        conn.execute(f"UPDATE ad_stats SET {cols} WHERE id = ?",
                     (*sets.values(), stat_id))
        conn.commit()
    finally:
        conn.close()


def ad_stats_delete(stat_id: int) -> None:
    conn = _conn()
    try:
        conn.execute("DELETE FROM ad_stats WHERE id = ?", (stat_id,))
        conn.commit()
    finally:
        conn.close()


def save(url: str, channel_id: str, source_path: str, output_path: str,
         hook: str = "", hook_base: str = "", description: str = "",
         movie_name: str = "", movie_id: str = "", crop_rect: str = "",
         uniq_params: str = "", banner_path: str = "") -> int:
    """Сохраняет/обновляет запись по output_path. Возвращает id.

    crop_rect/uniq_params — JSON-строки (см. app/pipeline.py encode_crop/
    encode_params), banner_path — путь к файлу баннера или "". Пустая строка
    в любом из трёх полей = «не задано» (старые записи до этой фичи, или
    ролик без баннера) — pipeline.py декодирует пустую строку в None/дефолт.
    """
    conn = _conn()
    try:
        # если ролик с таким output уже есть — обновляем (перенанесение хука)
        row = conn.execute(
            "SELECT id FROM history WHERE output_path = ?", (output_path,)
        ).fetchone()
        if row:
            # movie_id обновляем только если передан непустой — иначе сохраняем прежний
            conn.execute(
                "UPDATE history SET hook=?, description=?, movie_name=?, "
                "movie_id=COALESCE(NULLIF(?, ''), movie_id), "
                "crop_rect=?, uniq_params=?, banner_path=?, "
                "created_at=? WHERE id=?",
                (hook, description, movie_name, movie_id,
                 crop_rect, uniq_params, banner_path, time.time(), row["id"]),
            )
            conn.commit()
            return row["id"]
        cur = conn.execute(
            "INSERT INTO history (url, channel_id, source_path, output_path, "
            "hook, hook_base, description, movie_name, movie_id, "
            "crop_rect, uniq_params, banner_path, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (url, channel_id, source_path, output_path, hook, hook_base,
             description, movie_name, movie_id,
             crop_rect, uniq_params, banner_path, time.time()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def update_description(output_path: str, description: str,
                       movie_name: str | None = None,
                       movie_id: str | None = None) -> bool:
    """Меняет описание готового ролика (и фильм, если его выбрали заново).

    Отдельно от save(): тот требует все поля разом и обновляет created_at,
    из-за чего ролик прыгал бы наверх галереи при правке одной запятой.
    Здесь ни дата, ни сам файл не трогаются — правка текста не повод
    пересобирать видео.

    movie_name/movie_id = None означает «не трогать»: описание правят чаще,
    чем меняют фильм, и затирать привязку пустыми значениями нельзя —
    от movie_id зависит кнопка «Пост TG».
    """
    fields = ["description = ?"]
    values: list = [description]
    if movie_name is not None:
        fields.append("movie_name = ?")
        values.append(movie_name)
    if movie_id is not None:
        fields.append("movie_id = ?")
        values.append(movie_id)
    values.append(output_path)
    conn = _conn()
    try:
        cur = conn.execute(
            f"UPDATE history SET {', '.join(fields)} WHERE output_path = ?",
            values,
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def by_output(output_path: str) -> dict | None:
    """Запись по пути готового файла (для галереи)."""
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT * FROM history WHERE output_path = ?", (output_path,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def exists_for_channel(url: str, channel_id: str) -> bool:
    """Обрабатывалась ли эта ссылка для этого канала (защита от дублей)."""
    if not url:
        return False
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM history WHERE url = ? AND channel_id = ? LIMIT 1",
            (url, channel_id),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def delete(output_path: str) -> None:
    conn = _conn()
    try:
        conn.execute("DELETE FROM history WHERE output_path = ?", (output_path,))
        conn.commit()
    finally:
        conn.close()


# --- Библиотека пользовательских баннеров (app/gui/process_panel.py) ---

def banner_library_add(name: str, path: str) -> int:
    """Добавляет баннер в библиотеку. Возвращает id."""
    conn = _conn()
    try:
        cur = conn.execute(
            "INSERT INTO banners (name, path, created_at) VALUES (?, ?, ?)",
            (name, path, time.time()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def banner_library_list() -> list[dict]:
    """Все баннеры библиотеки, новые сверху."""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM banners ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def banner_library_delete(banner_id: int) -> None:
    """Удаляет запись из библиотеки и сам файл (best-effort, как move_posted)."""
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT path FROM banners WHERE id = ?", (banner_id,)
        ).fetchone()
        conn.execute("DELETE FROM banners WHERE id = ?", (banner_id,))
        conn.commit()
        if row and row["path"]:
            Path(row["path"]).unlink(missing_ok=True)
    finally:
        conn.close()


# --- каналы (см. app/channels.py — там из этих строк собираются Channel) ---

CHANNEL_FIELDS = (
    "id", "name", "preset", "source", "use_hook", "desc_header",
    "desc_signature", "ad_tracked", "payout_per_million",
    "watermark_path", "fixed_crop", "sort_order",
    "background", "use_watermark", "watermark_scale", "watermark_position",
    "blur_strength",
)


def channels_list() -> list[dict]:
    """Все каналы в порядке sort_order. Пустой список = каналов ещё нет
    (первый запуск у нового пользователя — см. app/channels.py)."""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM channels ORDER BY sort_order, id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def channel_save(data: dict) -> None:
    """Создаёт или обновляет канал по id (экран настроек).

    Пишем только известные поля — лишние ключи из формы молча игнорируются,
    чтобы случайный мусор не ломал запрос.
    """
    row = {k: data[k] for k in CHANNEL_FIELDS if k in data}
    if not row.get("id"):
        raise ValueError("у канала должен быть id")
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    updates = ", ".join(f"{k} = excluded.{k}" for k in row if k != "id")
    conn = _conn()
    try:
        conn.execute(
            f"INSERT INTO channels ({cols}) VALUES ({marks}) "
            f"ON CONFLICT(id) DO UPDATE SET {updates}",
            tuple(row.values()),
        )
        conn.commit()
    finally:
        conn.close()


def channel_delete(channel_id: str) -> None:
    """Удаляет канал. Историю роликов НЕ трогает — записи остаются с этим
    channel_id, галерея покажет их под сырым id вместо имени (см.
    app/gui/gallery.py). Так безопаснее, чем каскадом сносить 400+ записей."""
    conn = _conn()
    try:
        conn.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
        conn.commit()
    finally:
        conn.close()


def history_channel_ids() -> list[str]:
    """Уникальные channel_id, встречающиеся в истории роликов.

    Нужно двум местам: определить, что база не пустая (app/channels.py —
    перенос старых каналов), и показать в галерее ролики каналов, которые
    пользователь успел удалить.
    """
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT DISTINCT channel_id FROM history "
            "WHERE channel_id IS NOT NULL AND channel_id != ''"
        ).fetchall()
        return [r["channel_id"] for r in rows]
    finally:
        conn.close()


# --- пресеты уникализации (app/gui/uniq_editor.py) ---

def uniq_presets() -> list[dict]:
    """Сохранённые наборы ползунков, новые сверху. params — уже распакованный
    dict, вызывающему коду не нужно знать про JSON в базе."""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM uniq_presets ORDER BY created_at DESC"
        ).fetchall()
    finally:
        conn.close()
    result = []
    for r in rows:
        row = dict(r)
        try:
            row["params"] = json.loads(row["params"] or "{}")
        except (ValueError, TypeError):
            continue          # битая запись — пропускаем, а не роняем список
        result.append(row)
    return result


def uniq_preset_save(name: str, params: dict) -> None:
    """Сохраняет набор (или перезаписывает одноимённый)."""
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO uniq_presets (name, params, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET params = excluded.params",
            (name, json.dumps(params), time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def uniq_preset_delete(name: str) -> None:
    conn = _conn()
    try:
        conn.execute("DELETE FROM uniq_presets WHERE name = ?", (name,))
        conn.commit()
    finally:
        conn.close()


# --- заготовки описаний (app/gui/process_panel.py) ---

def desc_templates() -> list[dict]:
    """Сохранённые заготовки описаний, новые сверху."""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM desc_templates ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def desc_template_save(name: str, text: str) -> None:
    """Добавляет заготовку (или обновляет одноимённую)."""
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO desc_templates (name, text, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET text = excluded.text",
            (name, text, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def desc_template_delete(name: str) -> None:
    conn = _conn()
    try:
        conn.execute("DELETE FROM desc_templates WHERE name = ?", (name,))
        conn.commit()
    finally:
        conn.close()
