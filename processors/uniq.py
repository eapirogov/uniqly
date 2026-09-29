"""Уникализация: рандомные микро-искажения на каждый ролик (пакет «средний»).

Каждый вызов даёт СВОИ случайные значения в узких диапазонах — два ролика из
одного исходника получаются разными, ломается автосравнение (perceptual hash,
Content ID, аудио-fingerprint). Зрителю изменения незаметны.

Пакет «средний»:
- рандомный доп. цветокор (eq поверх пресетного): контраст/яркость/насыщенность/gamma
- микро-поворот кадра на 0.2–0.5°
- лёгкое временнóе зерно (noise) — разрушает пиксельный паттерн
- рандомная скорость 0.96–0.99 (video setpts + audio atempo синхронно)
- рандомный сдвиг тона аудио ±1.5% + atempo (ломает Shazam-подобный fingerprint)
- обрезка 0.3–0.8с с начала (меняет стартовый кадр и длину)

Значения детерминируются seed'ом, если нужен повтор; иначе — случайные.
"""
import random
import subprocess
from pathlib import Path
from app import binaries


def has_audio(path: Path | str) -> bool:
    """True, если в файле есть аудиопоток (иначе -af упадёт)."""
    try:
        out = binaries.run(
            [binaries.ffprobe(), "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return bool(out.stdout.strip())
    except (subprocess.SubprocessError, OSError):
        return False


def make_params(seed: int | None = None, preset: str = "") -> dict:
    """Случайный набор параметров уникализации для одного ролика.

    preset — имя пресета, если известно. Нужен из-за зерна: пресет tiktok
    раньше выключал его жёстко внутри себя, и ползунок «зерно» там ничего не
    делал. Теперь зерном управляет ползунок, а прежнее поведение сохраняется
    стартовым нулём — ролики, собранные без ручных правок, выглядят как
    раньше. Кому нужно зерно на tiktok — двигает ползунок сам.
    """
    rnd = random.Random(seed)
    noise = 0 if preset == "tiktok" else rnd.randint(6, 12)
    return {
        "contrast": round(rnd.uniform(0.97, 1.03), 3),   # доп. к пресетному eq
        "brightness": round(rnd.uniform(-0.015, 0.015), 3),
        "saturation": round(rnd.uniform(0.97, 1.03), 3),
        "gamma": round(rnd.uniform(0.97, 1.03), 3),
        "rotate": round(rnd.uniform(-0.5, 0.5), 3),      # градусы
        "noise": noise,                                  # сила зерна (0 = нет)
        "speed": round(rnd.uniform(0.96, 0.99), 3),      # 1..4% ускорение
        "pitch": round(rnd.uniform(0.985, 1.015), 4),    # ±1.5% тон
        "trim_start": round(rnd.uniform(0.3, 0.8), 2),   # сек с начала
    }


def neutral_params() -> dict:
    """Параметры «без уникализации» — нейтральные позиции всех ползунков.

    Стартовое состояние нового ролика: пользователь сначала видит исходник как
    есть и сам решает, что менять. Значения подобраны так, чтобы фильтры не
    меняли картинку и звук: множители = 1, сдвиги = 0.

    Не совпадает с минимумами QSlider (те дали бы contrast 0.90 и т.п.) и не
    совпадает с make_params() — тот даёт случайный набор для автоматической
    уникализации, см. его docstring.
    """
    return {
        "contrast": 1.0,      # eq: множитель, 1 = без изменений
        "brightness": 0.0,    # eq: сдвиг, 0 = без изменений
        "saturation": 1.0,
        "gamma": 1.0,
        "rotate": 0.0,        # градусы
        "noise": 0,           # зерно выключено
        "speed": 1.0,         # setpts: 1 = исходный темп
        "pitch": 1.0,         # тон звука без сдвига
        "trim_start": 0.0,    # с начала ролика, ничего не срезаем
    }


def video_suffix(p: dict, noise: bool = True, out_w: int = 1080, out_h: int = 1920) -> str:
    """Хвост видео-фильтра (добавляется В КОНЕЦ пресетного фильтра, ПЕРЕД pad/scale
    не вставлять — только там, где кадр уже финального размера).

    Порядок: доп.цветокор → зерно → микро-поворот. Финал приводит кадр ТОЧНО к
    out_w×out_h с SAR 1:1 — иначе выходит 1078xN и кривой SAR (2937:2936), из-за
    чего Buffer вставляет чёрный первый кадр. setpts — отдельно (speed_filter).

    noise=False — без зерна (для пресета tiktok: и так сильно уникализирует,
    зерно только раздувает вес файла).
    """
    rot = p["rotate"] * 3.14159265 / 180  # в радианы
    # alls=0 — это «зерна нет»; фильтр всё равно прогонял бы кадр впустую
    noise_part = (f",noise=alls={p['noise']}:allf=t"
                  if noise and p["noise"] else "")
    # Поворот оставляет по краям треугольные клинья фона — их прячет микро-зум.
    # Но при rotate=0 клиньев нет, а зум всё равно срезал бы края и мылил кадр
    # пересэмплингом: на нейтральных значениях (neutral_params) кадр обязан
    # совпадать с исходником, поэтому и поворот, и зум просто не добавляем.
    if p["rotate"]:
        zoom = 1.02
        geom = (f",rotate={rot:.5f}:ow=iw:oh=ih"
                f",scale=iw*{zoom}:ih*{zoom},crop=iw/{zoom}:ih/{zoom}")
    else:
        geom = ""
    return (
        f",eq=contrast={p['contrast']}:brightness={p['brightness']}:"
        f"saturation={p['saturation']}:gamma={p['gamma']}"
        f"{noise_part}"
        f"{geom}"
        f",scale={out_w}:{out_h}:flags=lanczos,setsar=1"  # точный размер + SAR 1:1
    )


def speed_filter(p: dict) -> str:
    """setpts для рандомной скорости + обнуление старта PTS.

    -STARTPTS обязателен: после -ss (обрезка старта) первый кадр имеет ненулевой
    PTS (напр. 0.033), и плеер/Buffer рисуют чёрный кадр-заглушку до него.
    Обнуляем — первый кадр ровно на t=0, чёрного кадра нет.
    """
    return f"setpts={p['speed']}*PTS-STARTPTS"


DEFAULT_SAMPLE_RATE = 44100


def sample_rate(path: Path | str) -> int:
    """Частота звука в файле, Гц. Не определили — DEFAULT_SAMPLE_RATE.

    Нужна audio_filter: asetrate объявляет «играй эти сэмплы с такой
    частотой», поэтому считать надо от ИСТИННОЙ частоты файла.
    """
    try:
        out = binaries.run(
            [binaries.ffprobe(), "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=sample_rate", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return int(out.stdout.strip() or DEFAULT_SAMPLE_RATE)
    except (subprocess.SubprocessError, OSError, ValueError):
        return DEFAULT_SAMPLE_RATE


def audio_filter(p: dict, rate: int = DEFAULT_SAMPLE_RATE,
                 extra_speed: float = 1.0) -> str:
    """Аудио-фильтр: подгон темпа под скорость видео + сдвиг тона.

    atempo компенсирует ускорение (1/speed), asetrate сдвигает тон.

    extra_speed — во сколько раз пресет ускоряет видео САМ, помимо ползунка
    скорости (см. processors/tiktok.BASE_SPEED).

    rate — ИСТИННАЯ частота звука исходника. Раньше здесь было жёстко
    44100, и на файле с 48000 Гц звук растягивался в 48000/44100 раз, то
    есть на 8.8%: к концу минутного ролика звук уезжал от картинки на
    пять секунд. Хуже всего, что это происходило даже при нейтральных
    ползунках, когда звук вообще не просили трогать.
    """
    # extra_speed — ускорение, которое пресет применяет к видео помимо
    # ползунка (у tiktok в базовом фильтре зашито setpts=0.985*PTS). Звук
    # надо ускорить на столько же, иначе дорожки разъезжаются.
    total_speed = p["speed"] * extra_speed
    tempo = round(1 / total_speed, 4)  # video ускорено → audio ускоряем так же
    # сдвиг тона: меняем sample rate и возвращаем atempo обратно
    return (
        f"asetrate={rate}*{p['pitch']},aresample={rate},"
        f"atempo={round(tempo / p['pitch'], 4)}"
    )
