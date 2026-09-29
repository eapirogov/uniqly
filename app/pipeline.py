"""Оркестрация этапов обработки одного ролика (MVP: download → uniqueize → preview)."""
import json
import subprocess
import uuid
from pathlib import Path
from typing import Callable

from app import binaries, ffmpeg_error, downloader, preview, channels
from app.config import channel_output_dir, DOWNLOADS, PREVIEWS, WORKSPACE
from processors import uniq


# --- Кодеки JSON для полей history.crop_rect/uniq_params (app/gui/process_panel.py) ---
# Собраны в одном месте, чтобы формат не расползался по нескольким файлам
# (см. риск рассинхронизации схемы, план app/gui/process_panel.py).

def encode_crop(crop_rect: tuple[int, int, int, int] | None) -> str:
    """(w,h,x,y) → JSON-строка для storage.save(). None → ''."""
    return json.dumps(list(crop_rect)) if crop_rect else ""


def decode_crop(text: str) -> tuple[int, int, int, int] | None:
    """JSON-строка из history.crop_rect → (w,h,x,y). '' → None."""
    if not text:
        return None
    try:
        w, h, x, y = json.loads(text)
        return (int(w), int(h), int(x), int(y))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def encode_params(params: dict | None) -> str:
    """dict уникализации → JSON-строка. None → ''."""
    return json.dumps(params) if params else ""


def decode_params(text: str) -> dict:
    """JSON-строка из history.uniq_params → dict. '' или битые данные →
    новый случайный набор (make_params()) — старые записи до этой фичи
    никогда не сохраняли параметры, откатываемся на прежнее поведение."""
    if not text:
        return uniq.make_params()
    try:
        return json.loads(text)
    except (ValueError, json.JSONDecodeError):
        return uniq.make_params()


def download_step(url: str, progress: Callable[[str], None]) -> Path:
    """Скачивание исходника. Дальше главный поток показывает окно разметки
    кропа, а рендер идёт через render_with_params (app/gui/process_panel.py)."""
    return downloader.download(url, progress=progress)


# Форматы, которые есть смысл открывать. Список для диалога выбора файла;
# что-то экзотическое всё равно можно выбрать через «Все файлы».
VIDEO_SUFFIXES = (".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".flv",
                  ".wmv", ".mpg", ".mpeg", ".3gp")


def save_links(urls: list[str], channel_id: str = "") -> Path | None:
    """Складывает список ссылок в файл ДО обработки.

    Ссылка попадает в историю только после «Применить» — то есть когда ролик
    уже готов. Если пачка упала на скачивании (пропал интернет, VPN закрыл
    Instagram), список пропадал целиком, и восстановить его было неоткуда:
    имена скачанных файлов случайные, самой ссылки они не хранят.

    Файл лежит в workspace/saved_links/ рядом с данными. Не бросает
    исключений: это подстраховка, она не должна мешать основной работе.
    """
    from datetime import datetime
    try:
        folder = Path(WORKSPACE) / "saved_links"
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        path = folder / f"{stamp}.txt"
        head = f"# {len(urls)} ссылок"
        if channel_id:
            head += f", канал {channel_id}"
        path.write_text(head + "\n" + "\n".join(urls) + "\n", encoding="utf-8")
        # оставляем последние 50 списков, чтобы папка не росла бесконечно
        old = sorted(folder.glob("*.txt"), key=lambda f: f.stat().st_mtime)
        for extra in old[:-50]:
            extra.unlink(missing_ok=True)
        return path
    except OSError:
        return None


def import_file(path: Path | str, progress: Callable[[str], None] | None = None) -> Path:
    """Копирует ролик с диска пользователя в рабочую папку и возвращает копию.

    Именно копирует, а не берёт файл на месте. Причин две:
    - исходник может лежать на флешке или во «Входящих» мессенджера — стоит
      её вынуть или почистить папку, и «Повторить» перестанет работать;
    - программа сама чистит downloads/ по кнопке «Очистить исходники», и
      чужой файл в другом месте она бы не тронула, зато потеряла бы связь.

    Имя даём случайное, как скачанному, — два файла с одинаковым названием
    с разных дисков иначе затёрли бы друг друга.
    """
    src = Path(path).expanduser()
    if not src.exists():
        raise RuntimeError(
            "Файл не найден — возможно, его переместили или удалили."
        )
    if src.is_dir():
        raise RuntimeError("Это папка, а не видеофайл. Выберите файл с роликом.")

    if progress:
        progress("Читаю файл…")
    # места нужно вдвое больше размера файла: сама копия плюс будущий рендер
    try:
        import shutil as _shutil
        need = src.stat().st_size * 2
        free = _shutil.disk_usage(DOWNLOADS).free
        if free < need:
            raise RuntimeError(
                f"Не хватает места на диске: нужно примерно "
                f"{need // (1024 * 1024)} МБ, свободно {free // (1024 * 1024)} МБ."
            )
    except OSError:
        pass          # не смогли посчитать — не мешаем работать

    dst = DOWNLOADS / f"{uuid.uuid4().hex[:10]}{src.suffix.lower()}"
    try:
        import shutil
        shutil.copy2(src, dst)
    except OSError as e:
        raise RuntimeError(
            f"Не удалось скопировать файл: {e.strerror or e}. "
            "Проверьте, что файл не занят другой программой."
        ) from e

    # Проверяем, что это действительно видео: ffprobe должен найти видеодорожку.
    # Иначе пользователь узнал бы о проблеме только на рендере, невнятной
    # ошибкой ffmpeg, и уже после копирования.
    try:
        from processors import cropdetect
        cropdetect.probe_size(dst)
    except Exception as e:  # noqa: BLE001 — любая причина = файл не подходит
        dst.unlink(missing_ok=True)
        raise RuntimeError(
            f"Это не видеофайл или он повреждён ({src.name}). "
            "Выберите другой файл."
        ) from e
    return dst


def render_with_params(
    src: Path, channel_id: str,
    progress: Callable[[str], None],
    crop_rect: tuple[int, int, int, int] | None,
    uniq_params: dict,
    caption: str = "",
    banner_path: Path | str | None = None,
) -> dict:
    """Рендер с ЯВНЫМИ параметрами (ручная разметка/слайдеры) — точка входа
    для app/gui/process_panel.py. НЕ скачивает (src уже готов), сразу зовёт
    uniqueize с переданными crop_rect/params/banner_path вместо того, чтобы
    пресет генерировал их сам. OCR-хук здесь НЕ читается автоматически —
    панель показывает всё сразу, а не пошагово; хук передаётся явно через
    caption, если нужен (использует _wrap_hook, как reapply_hook)."""
    ch = channels.by_id(channel_id)
    preset = channels.PRESETS[ch.preset]
    src = Path(src)

    progress("Уникализация…")
    # Без суффикса пресета в имени: «_crop»/«_reels» — внутренние кодовые
    # слова, пользователю они ничего не говорят. Канал берётся из папки
    # (output/<канал>/), а суффикс был лишь запасным способом опознать
    # СТАРЫЕ файлы без записи в истории — там он и остаётся работать
    # (см. app/gui/gallery.py, _PRESET_BY_SUFFIX).
    out = channel_output_dir(channel_id) / f"{src.stem}.mp4"
    preset.uniqueize(
        src, out, channel_id=channel_id, crop_rect=crop_rect,
        params=uniq_params, banner_path=banner_path,
        caption=_wrap_hook(caption) if caption else "",
    )

    progress("Превью…")
    prev = preview.make_preview(out)

    progress("Готово")
    return {
        "source": str(src),
        "output": str(out),
        "preview": str(prev),
        "ai": {},
        "channel": channel_id,
    }


def _letterbox_chain(crop_rect, background: str, blur: int = 0) -> str:
    """Фильтр «вписать кроп в 1080×1920» — общий для превью-кадра и мини-плеера.

    background="blur" повторяет фон пресета reels: копия кадра растягивается
    на весь холст и размывается, поверх ложится сам кроп. Раньше превью всегда
    рисовало ЧЁРНЫЕ поля, а готовый ролик у канала с размытым фоном выходил
    размытым — человек настраивал одно, получал другое.
    """
    if not crop_rect:
        return ""
    w, h, x, y = crop_rect
    scaled_h = round(h * 1080 / w)
    scaled_h -= scaled_h % 2
    if background == "blur":
        from processors.reels import DEFAULT_BLUR
        strength = blur or DEFAULT_BLUR
        return (
            f"crop={w}:{h}:{x}:{y},split[main][blursrc];"
            "[blursrc]scale=1080:1920:force_original_aspect_ratio=increase,"
            f"crop=1080:1920,boxblur={strength}:2[bg];"
            f"[main]scale=1080:{scaled_h}:flags=lanczos[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2,"
        )
    return (
        f"crop={w}:{h}:{x}:{y},"
        f"scale=1080:{scaled_h}:flags=lanczos,"
        "pad=1080:1920:0:(1920-ih)/2:black,"
    )


def preview_frame_with_params(
    src: Path, crop_rect: tuple[int, int, int, int] | None,
    uniq_params: dict, at_sec: float = 1.0,
    background: str = "black", blur: int = 0,
) -> Path:
    """Быстрый превью-кадр С применённым кропом+цветокором (без полного
    кодирования видео) — для debounce-обновления слайдеров и кнопки
    «Обрезать» в app/gui/uniq_editor.py.

    Не собирает полный композит (вотермарк/баннер) — цель показать, как
    влияют слайдеры. Но ФОН повторяет настройку канала: background="blur"
    даёт тот же размытый фон, что в готовом ролике. Иначе выходил обман —
    в превью чёрные поля, а в результате размытая картинка.
    Путь превью — отдельный файл (не PREVIEWS/{stem}.jpg, чтобы не путать
    с реальным превью готового ролика).

    Кроп НЕ растягивается до заполнения 1080×1920 (это исказило бы
    пропорции — тот же баг, что чинили в processors/reels.py/crop.py).
    Вместо этого — letterbox: кроп вписывается по ширине 1080 с сохранением
    пропорций, свободное место сверху/снизу заполняется фоном.
    """
    src = Path(src)
    # Свой файл на каждый вызов: раньше все рендеры писали в один
    # {stem}_editpreview.jpg, и параллельные обновления (быстрая правка рамки
    # или ползунков) читали чужой, наполовину записанный кадр — превью не
    # совпадало с текущей обрезкой. Прошлые файлы подчищаем тут же.
    for old_file in PREVIEWS.glob(f"{src.stem}_editpreview*.jpg"):
        try:
            old_file.unlink()
        except OSError:
            pass          # файл занят/уже удалён — не повод падать
    out = PREVIEWS / f"{src.stem}_editpreview_{uuid.uuid4().hex[:8]}.jpg"
    crop_part = _letterbox_chain(crop_rect, background, blur)
    vf = crop_part + uniq.video_suffix(uniq_params).lstrip(",")
    cmd = [
        binaries.ffmpeg(), "-y", "-ss", str(at_sec), "-i", str(src),
        "-vf", vf, "-frames:v", "1", "-q:v", "3", str(out),
    ]
    result = binaries.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0 or not out.exists():
        raise ffmpeg_error.fail(result.stderr, cmd)
    return out


def preview_clip_with_params(
    src: Path, crop_rect: tuple[int, int, int, int] | None,
    uniq_params: dict, duration: float = 5.0,
    background: str = "black", blur: int = 0,
) -> Path:
    """Первые duration секунд (видео+аудио) с применённым кроп+уникализация-
    цветокор — для мини-плеера «▶» поверх превью в app/gui/uniq_editor.py.

    Честный ffmpeg-рендер (не live-имитация через QGraphicsVideoItem —
    пробовали, на реальном macOS-экране видео-слой рендерится отдельным
    GPU-composited native layer и игнорирует Qt scene-клиппинг, поэтому
    "обрезка" в live-плеере на деле не была видна несмотря на то, что
    offscreen-тесты давали правильную картинку). Только начало ролика — не
    весь целиком: полный рендер длинного исходника занимает 15-30+ секунд и
    не нужен для оценки, как выглядят кроп+фильтры (первых 5с достаточно).

    Тот же letterbox-подход, что и preview_frame_with_params (без растяжения,
    фон по настройке канала), плюс speed_filter/audio_filter из
    processors/uniq.py — скорость/тон звука как в финальном рендере.
    Вотермарк и баннер не накладываются, см. preview_frame_with_params.
    """
    src = Path(src)
    out = PREVIEWS / f"{src.stem}_editclip.mp4"
    crop_part = _letterbox_chain(crop_rect, background, blur)
    vf = crop_part + uniq.video_suffix(uniq_params).lstrip(",") + "," + uniq.speed_filter(uniq_params)
    trim_start = uniq_params.get("trim_start", 0.0)
    cmd = [
        binaries.ffmpeg(), "-y", "-ss", str(trim_start), "-i", str(src),
        "-t", str(duration), "-vf", vf,
    ]
    if uniq.has_audio(src):
        cmd += ["-af", uniq.audio_filter(uniq_params, uniq.sample_rate(src))]
    else:
        cmd += ["-an"]
    # это только предпросмотр (не финальный ролик) — лёгкий для realtime-
    # декодирования QMediaPlayer важнее качества: baseline-профиль без
    # B-кадров, ограниченный битрейт, фиксированный fps (VFR из исходника
    # иначе даёт рывки в софт-плеере на macOS)
    cmd += [
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "fastdecode",
        "-profile:v", "baseline", "-level", "3.1", "-bf", "0",
        "-crf", "26", "-maxrate", "4M", "-bufsize", "8M",
        "-r", "30", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out),
    ]
    result = binaries.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0 or not out.exists():
        raise ffmpeg_error.fail(result.stderr, cmd)
    return out


def clean_hook(text: str) -> str:
    """Чистит распознанную OCR-надпись: убирает мусорные символы, оставляет
    слова. Зовётся из app/gui/process_panel.py (кнопка «Прочитать с видео»)."""
    import re
    words = [w for w in text.split() if re.search(r"[А-Яа-яЁёA-Za-z0-9]", w)]
    cleaned = " ".join(words)
    return re.sub(r"^[^А-Яа-яЁёA-Za-z0-9]+|[^А-Яа-яЁёA-Za-z0-9]+$", "", cleaned).upper()


def _balanced_split(words: list[str], n: int) -> list[str]:
    """Делит слова на n строк, минимизируя длину самой длинной строки (перебор)."""
    from itertools import combinations

    best, best_max = None, float("inf")
    for cuts in combinations(range(1, len(words)), n - 1):
        bounds = [0, *cuts, len(words)]
        lines = [" ".join(words[a:b]) for a, b in zip(bounds, bounds[1:])]
        longest = max(len(ln) for ln in lines)
        if longest < best_max:
            best, best_max = lines, longest
    return best


def _wrap_hook(hook: str) -> str:
    """Готовит хук для нанесения. Строки 1 и 3 оранжевые (дефолт), средняя синяя [k].

    Если в hook есть переносы строк (пользователь сам расставил Enter) — берём их как есть.
    Иначе авторазбивка по смыслу на 3 строки (как в эталоне).
    """
    # ручные переносы важнее авто: если есть — используем их
    manual = [ln.strip() for ln in hook.splitlines() if ln.strip()]
    if len(manual) > 1:
        lines = manual
    else:
        words = hook.split()
        if not words:
            return ""
        lines = _balanced_split(words, min(3, len(words)))
    # средняя строка синяя [k] (только если строк 3), остальные оранжевые
    mid = len(lines) // 2
    return "\n".join(
        f"[k]{ln}" if i == mid and len(lines) == 3 else ln
        for i, ln in enumerate(lines)
    )


def reapply_hook(source: str, hook: str, channel_id: str,
                 progress: Callable[[str], None], old_output: str = "",
                 crop_rect: tuple[int, int, int, int] | None = None,
                 uniq_params: dict | None = None,
                 banner_path: str | None = None) -> dict:
    """Перерендеривает ролик из исходника с новым хуком. Старое видео удаляет.

    crop_rect/uniq_params/banner_path — сохранённые значения предыдущего
    рендера (из storage.by_output() + decode_crop/decode_params на стороне
    вызывающего кода). Раньше эта функция вызывала uniqueize БЕЗ них — при
    пересборке с новым хуком кроп сбрасывался в дефолт, а параметры
    уникализации генерировались заново случайно (баг, найден при разборе
    существующего поведения перед добавлением ручной уникализации).
    """
    ch = channels.by_id(channel_id)
    preset = channels.PRESETS[ch.preset]
    src = Path(source)
    progress("Наношу хук…")
    out = channel_output_dir(channel_id) / f"{src.stem}_hook.mp4"
    preset.uniqueize(
        src, out, caption=_wrap_hook(hook), channel_id=channel_id,
        crop_rect=crop_rect, params=uniq_params, banner_path=banner_path,
    )

    progress("Превью…")
    prev = preview.make_preview(out)

    # удаляем прежний файл, если он был другим (напр. _tiktok при первом хуке)
    if old_output and str(out) != old_output:
        Path(old_output).unlink(missing_ok=True)

    progress("Готово")
    return {"source": source, "output": str(out), "preview": str(prev)}
