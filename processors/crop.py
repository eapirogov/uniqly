"""Пресет crop (центр-кроп + pad, кадр 1080×1920).

Раньше фильтр целиком переиспользовал crop_reels.FILTER (строкой). Для
поддержки ручного/автоопределённого crop_rect (см. app/gui/crop_editor.py,
processors/cropdetect.py) фильтр теперь строится здесь напрямую — числа
crop_reels.py остаются дефолтом, если crop_rect не передан.

Поверх — рекламный баннер (assets/banners/, чередуется через ролик) по центру
на границе видео/чёрного поля, появляется со 2-й секунды.
"""
import subprocess
from pathlib import Path

from processors import banner, uniq
from app import binaries, ffmpeg_error

# баннер crop: кадр 1080×1920. Ширина 490, центр Y 1600 — граница видео/фона
_BANNER_W = 490
_BANNER_CY = 1600


def _build_filter(p: dict, crop_rect: tuple[int, int, int, int] | None = None,
                   banner_path: Path | str | None = None) -> str:
    """crop-фильтр → уникализация → баннер → [out]. Уникализация до баннера.

    crop_rect = (w, h, x, y) — ручная/автоопределённая разметка кропа под
    конкретный исходник. Не передан — старый фиксированный кроп 1080:1280:0:422.

    Высота после scale считается из РЕАЛЬНОЙ пропорции crop_rect (не жёстко
    1280) — иначе кроп другой пропорции сплющивается/растягивается под старое
    соотношение сторон (баг, который был в processors/reels.py — тот же фикс).

    banner_path — явный путь к баннеру (ручной выбор в приложении). Если
    задан — баннер накладывается независимо от banner.is_enabled().
    """
    w, h, x, y = crop_rect if crop_rect else (1080, 1280, 0, 422)
    scaled_h = round(h * 1080 / w)
    scaled_h -= scaled_h % 2  # чётная высота для libx264
    uniq_chain = uniq.video_suffix(p) + "," + uniq.speed_filter(p)
    core = (
        f"[0:v]crop={w}:{h}:{x}:{y},"
        f"scale=1080:{scaled_h}:flags=lanczos,unsharp=5:5:0.8:5:5:0.0,"
        "eq=contrast=1.05:brightness=0.02:saturation=1.1,setsar=1,"
        "pad=1080:1920:0:(1920-ih)/2:black"
    )
    show_banner = banner_path is not None or banner.is_enabled()
    if not show_banner:
        return core + uniq_chain + "[out]"
    return (
        core + uniq_chain + "[base];"
        + banner.overlay_chain("base", "out", _BANNER_W, _BANNER_CY, banner_input=1)
    )


def uniqueize(input_path: Path, output_path: Path, *, channel_id: str = "",
              crop_rect: tuple[int, int, int, int] | None = None,
              params: dict | None = None,
              banner_path: Path | str | None = None, **_) -> Path:
    """Уникализирует ролик пресетом crop + рекламный баннер. Надпись не поддерживается.

    crop_rect — см. _build_filter. params — параметры уникализации (см.
    processors/uniq.make_params()), не переданы — генерируются случайно.
    banner_path — явный путь к баннеру, не передан — старое поведение по
    banner.ENABLED/banner.pick(channel_id).
    """
    input_path = Path(input_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    p = params if params is not None else uniq.make_params()
    cmd = [
        binaries.ffmpeg(), "-y", "-ss", str(0.5 + p["trim_start"]), "-i", str(input_path),
    ]
    show_banner = banner_path is not None or banner.is_enabled()
    if show_banner:
        ban = Path(banner_path) if banner_path is not None else banner.pick(channel_id)
        if not ban.exists():
            raise RuntimeError(f"нет баннера: {ban}")
        cmd += ["-stream_loop", "-1", "-i", str(ban)]  # зациклен до конца
    cmd += [
        "-filter_complex", _build_filter(p, crop_rect, banner_path),
        "-map", "[out]", "-map", "0:a?",  # аудио опционально (у некоторых нет)
    ]
    if uniq.has_audio(input_path):
        cmd += ["-af", uniq.audio_filter(p, uniq.sample_rate(input_path))]
    from app import quality
    cmd += [
        # crf ниже, чем было (20 вместо 23): здесь верх/низ кадра — чистая
        # чёрная заливка (pad), она почти не требует бит, из-за чего
        # crf-энкодер тратит на саму картинку МЕНЬШЕ бит, чем в reels.py, где
        # весь кадр заполнен контентом (картинка + блюр-фон) — субъективно
        # заметная просадка резкости при формально «том же» crf 23.
        # 20 — компромисс: заметно лучше 23, но не такой тяжёлый файл, как 18.
        "-c:v", "libx264", "-crf", str(quality.crf_for(20)), "-preset", "fast",
        "-c:a", "aac", "-b:a", "192k",
    ] + quality.scale_args()
    if show_banner:
        cmd += ["-shortest"]  # обрезаем зациклённый баннер по длине ролика
    cmd += [str(output_path)]
    result = binaries.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise ffmpeg_error.fail(result.stderr, cmd)
    return output_path
