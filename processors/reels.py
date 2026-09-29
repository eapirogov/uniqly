"""Пресет reels: размытый фон из самого кадра + вотермарк + баннер.

Фон — то же видео, растянутое на весь кадр 1080×1920 и размытое boxblur;
поверх по центру — исходный кадр в оригинальных пропорциях. Рекламный баннер
(assets/banners/, чередуется через ролик) кладётся на границе видео и нижнего
блюра, появляется со 2-й секунды.
"""
import subprocess
from pathlib import Path

from processors import banner, uniq
from app import binaries, ffmpeg_error
from app.config import ROOT

# баннер reels: ширина 550px, центр на Y=1600 (граница видео/нижнего блюра)
_BANNER_W = 550
_BANNER_CY = 1600

# Куда ставить логотип. Значения — выражения ffmpeg для overlay: W,H это кадр
# (1080×1920), w,h — сам логотип. MARGIN — отступ от края.
_WM_MARGIN = 40
WATERMARK_POSITIONS = {
    "bottom_center": ("внизу по центру", f"(W-w)/2:H-h-{_WM_MARGIN}"),
    "bottom_right":  ("внизу справа",    f"W-w-{_WM_MARGIN}:H-h-{_WM_MARGIN}"),
    "bottom_left":   ("внизу слева",     f"{_WM_MARGIN}:H-h-{_WM_MARGIN}"),
    "top_right":     ("вверху справа",   f"W-w-{_WM_MARGIN}:{_WM_MARGIN}"),
    "top_left":      ("вверху слева",    f"{_WM_MARGIN}:{_WM_MARGIN}"),
    "center":        ("по центру",       "(W-w)/2:(H-h)/2"),
}
DEFAULT_WM_POSITION = "bottom_right"

# Прежнее положение вотермарка: во всю ширину кадра, 210px от низа.
# Оставлено отдельным вариантом, чтобы его ролики выглядели ровно как раньше —
# формула другая (H-210, а не H-h-отступ), на глаз это разные места.
WATERMARK_POSITIONS["legacy"] = ("как было (внизу, во всю ширину)", "(W-w)/2:H-210")

# Размер логотипа — доля от ширины кадра. Прежнее поведение (вотермарк
# во всю ширину) — это 100%; для обычной картинки-логотипа столько не нужно.
DEFAULT_WM_SCALE = 22   # проценты

# Сила размытия фона (boxblur). Было жёстко 20; теперь регулируется
# ползунком «Размытие полей» в приложении.
DEFAULT_BLUR = 20
# общий вотермарк-фоллбэк: watermark.png в корне проекта. Файла может не
# быть (в репозиторий он не входит) — тогда пресет рендерит без логотипа.
DEFAULT_WATERMARK = ROOT / "watermark.png"

LEGACY_WM_SCALE = 100   # как было раньше: scale=1080:-1


def _watermark_for(channel_id: str):
    """Файл вотермарка для канала или None.

    Приоритет: настройка канала watermark_path → общий watermark.png в корне
    (прежнее поведение) → ничего. Импорт channels внутри функции, чтобы не
    было кольца импортов: app.channels импортирует processors.reels.
    """
    try:
        from app import channels
        ch = channels.by_id(channel_id)
        # пользователь явно снял галочку «ставить логотип»
        if ch.use_watermark is False:
            return None
        own = ch.watermark_path
        if own and Path(own).exists():
            return Path(own)
        if own:
            # Файл логотипа выбран, но пропал (удалили/переименовали/лежал на
            # отключённом диске). Раньше ролик молча собирался БЕЗ логотипа —
            # человек замечал это уже на опубликованном ролике. Пусть лучше
            # скажет прямо: настройка есть, а файла нет.
            raise RuntimeError(
                f"Файл логотипа не найден: {Path(own).name}. "
                "Выберите его заново в «Настройках» или снимите галочку "
                "«Ставить логотип»."
            )
        # галочка стоит, но файл не выбран — вотермарка нет
        if ch.use_watermark is True:
            return None
    except RuntimeError:
        raise            # наше сообщение про пропавший файл — наружу
    except Exception:  # noqa: BLE001 — канал мог быть удалён/база недоступна
        pass
    return DEFAULT_WATERMARK if DEFAULT_WATERMARK.exists() else None


def _build_filter(p: dict, crop_rect: tuple[int, int, int, int] | None = None,
                   banner_path: Path | str | None = None, has_watermark: bool = True,
                   wm_scale: int = LEGACY_WM_SCALE,
                   wm_position: str = "bottom_center",
                   blur: int = DEFAULT_BLUR) -> str:
    """reels-фильтр: блюр-фон + watermark → уникализация → баннер → [out].

    Уникализация (p) ложится на кадр с watermark ДО баннера — баннер не искажается.

    crop_rect = (w, h, x, y) в пикселях исходника — приходит из ручной разметки
    в приложении (app/gui/crop_editor.py) или автодетекта (processors/cropdetect.py).
    None — старое поведение, фиксированный кроп под привычный формат исходников.

    banner_path — явный путь к баннеру (выбран пользователем в приложении из
    библиотеки, см. app/gui/process_panel.py). Если задан — баннер накладывается
    независимо от banner.is_enabled() (ручной выбор побеждает глобальный флаг).
    """
    w, h, x, y = crop_rect if crop_rect else (1080, 1280, 0, 422)
    # передний план масштабируем по ширине 1080, высоту считаем по пропорции
    # РЕАЛЬНОГО кропа (не жёстко 1280!) — иначе кроп другой пропорции (напр.
    # из ручной разметки в приложении) насильно сплющивается/растягивается
    # под старое соотношение 1080:1280, искажая картинку.
    fg_h = round(h * 1080 / w)
    fg_h -= fg_h % 2  # чётная высота для libx264
    show_banner = banner_path is not None or banner.is_enabled()
    # размер и место логотипа: у каждого свой файл и свои привычки, жёстко
    # прибитые «во всю ширину, 210px от низа» годились только под конкретный
    # полупрозрачный полупрозрачный watermark.png — чужой квадратный логотип так
    # растянулся бы на пол-экрана
    wm_w = max(2, round(1080 * wm_scale / 100))
    wm_w -= wm_w % 2
    wm_pos = WATERMARK_POSITIONS.get(
        wm_position, WATERMARK_POSITIONS[DEFAULT_WM_POSITION])[1]
    bg_chain = f"crop=1080:1920,boxblur={blur}:2[bg];"
    return (
        f"[0:v]crop={w}:{h}:{x}:{y}[cropped];"
        "[cropped]split[main][blursrc];"
        "[blursrc]scale=1080:1920:force_original_aspect_ratio=increase,"
        + bg_chain
        + f"[main]scale=1080:{fg_h}:flags=lanczos,unsharp=5:5:0.8:5:5:0.0,"
        "eq=contrast=1.05:brightness=0.02:saturation=1.1,setsar=1[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2[video];"
        # вотермарк опционален: у канала может не быть своего файла (у друзей
        # его точно не будет). Без него просто пропускаем оверлей — раньше
        # uniqueize() падал с "нет watermark" и пресет не работал вовсе.
        + (f"[1:v]scale={wm_w}:-1[wm];[video][wm]overlay={wm_pos}"
           if has_watermark else "[video]null")
        + uniq.video_suffix(p) + "," + uniq.speed_filter(p)
        # индекс входа баннера съезжает, если вотермарка нет: с ним входы это
        # 0=видео, 1=вотермарк, 2=баннер, без него баннер становится первым
        + ("[withwm];" + banner.overlay_chain("withwm", "out", _BANNER_W, _BANNER_CY,
                                              banner_input=2 if has_watermark else 1)
           if show_banner else "[out]")
    )


def _watermark_look(channel_id: str) -> tuple[int, str]:
    """Размер (%) и положение логотипа для канала.

    Канал без явных настроек — прежнее поведение (во всю ширину,
    210px от низа), чтобы его ролики не изменились после обновления.
    """
    try:
        from app import channels
        ch = channels.by_id(channel_id)
        scale = ch.watermark_scale or 0
        position = ch.watermark_position or ""
        if scale and position:
            return int(scale), position
    except Exception:  # noqa: BLE001 — канала нет/база недоступна
        pass
    return LEGACY_WM_SCALE, "legacy"


def uniqueize(input_path: Path, output_path: Path, *, channel_id: str = "",
              crop_rect: tuple[int, int, int, int] | None = None,
              params: dict | None = None,
              banner_path: Path | str | None = None, **_) -> Path:
    """Уникализирует ролик пресетом reels + рекламный баннер. Требует watermark.png.

    crop_rect = (w, h, x, y) — ручная/автоопределённая разметка кропа под
    конкретный исходник. Не передан — старый фиксированный кроп 1080:1280:0:422.

    params — параметры уникализации (см. processors/uniq.make_params()). Не
    переданы — генерируются случайно как раньше (обратная совместимость).

    banner_path — явный путь к баннеру, выбранному пользователем в приложении
    (см. app/gui/process_panel.py). Не передан — старое поведение по
    banner.ENABLED/banner.pick(channel_id).
    """
    input_path = Path(input_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # вотермарк берём у канала (настройка watermark_path), иначе — общий
    # watermark.png в корне, как было раньше. Если ни того ни другого нет —
    # рендерим без вотермарка вместо падения: у постороннего пользователя
    # своего файла может не быть, и пресет reels обязан работать и так.
    wm = _watermark_for(channel_id)
    p = params if params is not None else uniq.make_params()
    cmd = [
        binaries.ffmpeg(), "-y", "-ss", str(0.5 + p["trim_start"]), "-i", str(input_path),
    ]
    if wm is not None:
        cmd += ["-i", str(wm)]
    show_banner = banner_path is not None or banner.is_enabled()
    if show_banner:
        ban = Path(banner_path) if banner_path is not None else banner.pick(channel_id)
        if not ban.exists():
            raise RuntimeError(f"нет баннера: {ban}")
        cmd += ["-stream_loop", "-1", "-i", str(ban)]  # зациклен до конца видео
    cmd += [
        "-filter_complex", _build_filter(p, crop_rect, banner_path, wm is not None,
                                          *_watermark_look(channel_id),
                                          blur=DEFAULT_BLUR),
        "-map", "[out]", "-map", "0:a?",  # аудио опционально (у некоторых нет)
    ]
    if uniq.has_audio(input_path):  # -af упадёт без аудиопотока
        cmd += ["-af", uniq.audio_filter(p, uniq.sample_rate(input_path))]
    # Размер и crf — из общей настройки «Качество готового ролика»
    # (app/quality.py). По умолчанию 1080p и crf 23, как было.
    from app import quality
    cmd += [
        "-c:v", "libx264", "-crf", str(quality.crf_for(23)), "-preset", "fast",
        "-c:a", "aac", "-b:a", "192k",
    ] + quality.scale_args()
    if show_banner:
        cmd += ["-shortest"]  # обрезаем зациклённый баннер по длине ролика
    cmd += [str(output_path)]
    result = binaries.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise ffmpeg_error.fail(result.stderr, cmd)
    return output_path
