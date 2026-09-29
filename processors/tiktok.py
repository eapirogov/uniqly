"""Пресет tiktok: белый фон, надпись-хук над видео.

Базовый фильтр и отрисовка надписи лежат в processors/caption.py и не
дублируются здесь — этот модуль отвечает за сборку команды ffmpeg и пути.
"""
import subprocess
import tempfile
from pathlib import Path

from processors import caption as tr
from processors import uniq
from app import binaries, ffmpeg_error


# Окно видео у инста-исходников moviessguide (1080×1920): берём кадр фильма
# снизу (y=643), окно 1080×840 — то же соотношение, что в базовом пресете
# (720×560). Тогда pad делает белое поле сверху и надпись ложится над видео.
INSTA_CROP = dict(crop_w=1080, crop_h=840, crop_x=0, crop_y=643)


# Во сколько раз базовый фильтр пресета ускоряет видео: в
# caption.make_base_filter стоит setpts=0.985*PTS. Держим числом здесь,
# чтобы на столько же ускорить звук — иначе дорожки разъезжаются.
BASE_SPEED = 0.985


def uniqueize(
    input_path: Path,
    output_path: Path,
    caption: str = "",
    crf: int = 23,          # 18 давал ~75 Mbps / 800МБ; 23 = ~8-10 Mbps, на глаз то же
    preset: str = "fast",   # slow кодировал долго; fast — в разы быстрее
    crop: dict | None = None,
    crop_rect: tuple[int, int, int, int] | None = None,
    params: dict | None = None,
    banner_path: Path | str | None = None,  # принимается для единого API с
    # reels/crop, но не используется — этот пресет не накладывает баннер
    # (только надпись-хук); если понадобится, добавить тем же overlay_chain
    **_,  # channel_id и пр. игнорируем
) -> Path:
    """Уникализирует ролик пресетом TikTok. Возвращает путь к результату.

    caption — надпись (формат [x]); пустой → без надписи.
    crop — окно исходного видео {crop_w,crop_h,crop_x,crop_y};
    по умолчанию INSTA_CROP (инста 1080×1920, надпись автора отсекается).
    crop_rect — (w,h,x,y) из окна разметки в приложении (app/gui/crop_editor.py),
    тот же смысл, что и crop, просто другой формат (tuple вместо dict) —
    единый с processors/reels.py и processors/crop.py. Если передан, побеждает
    над crop. make_base_filter уже сохраняет пропорции сам (video_h считается
    из crop_h/crop_w), растяжение тут не воспроизводится.
    params — параметры уникализации (см. processors/uniq.make_params()), не
    переданы — генерируются случайно (обратная совместимость).
    """
    input_path = Path(input_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if crop_rect is not None:
        w, h, x, y = crop_rect
        crop = dict(crop_w=w, crop_h=h, crop_x=x, crop_y=y)
    base_filter = tr.make_base_filter(**(crop or INSTA_CROP))
    p = params if params is not None else uniq.make_params(preset="tiktok")
    # уникализация ложится на кадр ДО caption — надпись поверх не искажается.
    # Зерно раньше выключалось жёстко («этот пресет и так сильно уникализирует,
    # зерно только раздувает вес») — из-за этого у любого другого пользователя
    # ползунок «зерно» двигался, но ни на что не влиял. Теперь решает сам
    # ползунок: 0 = без зерна.
    uniq_filter = (base_filter + uniq.video_suffix(p, noise=bool(p.get("noise", 0)))
                   + "," + uniq.speed_filter(p))

    with tempfile.TemporaryDirectory() as td:
        if caption.strip():
            caption_png = Path(td) / "caption.png"
            tr.render_caption_png(caption, caption_png)
            # Обрезка старта — фильтром trim, а НЕ через -ss. С -ss не сходятся
            # два требования разом: как input-опция он ломает хук (overlay ищет
            # PNG по сдвинутым меткам), как output-опция — оставляет PTS 0.033
            # и Buffer рисует чёрный первый кадр. trim держит оба входа на одной
            # шкале: setpts-STARTPTS обнуляет её ДО наложения надписи.
            cmd = [
                binaries.ffmpeg(), "-y", "-i", str(input_path),
                "-loop", "1", "-i", str(caption_png),
                "-filter_complex",
                f"[0:v]trim=start={p['trim_start']},setpts=PTS-STARTPTS,"
                f"{uniq_filter}[v];"
                f"[v][1:v]overlay=0:0:eof_action=repeat:shortest=1[out]",
                "-map", "[out]", "-map", "0:a?",
            ]
        else:
            cmd = [
                binaries.ffmpeg(), "-y", "-ss", str(p["trim_start"]), "-i", str(input_path),
                "-vf", uniq_filter,
                "-map", "0:v", "-map", "0:a?",
            ]
        if uniq.has_audio(input_path):
            # Базовый фильтр пресета дополнительно ускоряет ВИДЕО на 1.5%
            # (setpts=0.985*PTS в caption.make_base_filter — это часть
            # уникализации). Звук надо ускорить ровно так же, иначе он
            # отстаёт: на 54-секундном ролике расхождение доходило до 0.8 с.
            af = uniq.audio_filter(p, uniq.sample_rate(input_path),
                                   extra_speed=BASE_SPEED)
            if caption.strip():
                # в ветке с хуком старт режет trim, а не -ss — аудио подрезаем
                # тем же способом, иначе звук уедет вперёд картинки
                af = (f"atrim=start={p['trim_start']},asetpts=PTS-STARTPTS,{af}")
            cmd += ["-af", af]
        from app import quality
        cmd += [
            "-c:v", "libx264", "-crf", str(quality.crf_for(crf)),
            "-preset", preset,
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
        ] + quality.scale_args() + [str(output_path)]
        result = binaries.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        raise ffmpeg_error.fail(result.stderr, cmd)
    return output_path
