"""Скачивание видео по ссылке (yt-dlp).

Площадка не задаётся нигде: yt-dlp определяет её по самой ссылке и умеет
сотни сайтов — YouTube, Instagram, TikTok, VK и прочие. Отдельная настройка
нужна только Instagram (куки из браузера, см. cookie_browser)."""
import uuid
from pathlib import Path
from typing import Callable

from app import binaries
from app.config import DOWNLOADS

# Instagram больше не отдаёт видео анонимно — нужны куки из браузера, где
# ты залогинен в IG. Раньше браузер был жёстко прописан ("chrome"); теперь это
# настройка (вкладка «Настройки» → Instagram), т.к. у другого пользователя
# может быть Firefox/Edge/Brave. Safari на macOS блокирует доступ по sandbox —
# его в списке нет.
DEFAULT_COOKIE_BROWSER = "chrome"
COOKIE_BROWSERS = ["chrome", "firefox", "edge", "brave", "opera", "vivaldi", "chromium"]


def cookie_browser() -> str:
    """Браузер, откуда yt-dlp берёт куки Instagram (настройка пользователя)."""
    from app import storage
    value = storage.kv_get("cookie_browser", DEFAULT_COOKIE_BROWSER)
    return value if value in COOKIE_BROWSERS else DEFAULT_COOKIE_BROWSER


def _format_progress_hook(progress: Callable[[str], None], grand_total: int = 0):
    """yt-dlp progress_hook → тот же progress(str), что шлёт статус карточки.

    Показываем ОБЩИЙ процент, а не тот, что даёт yt-dlp. У него процент — по
    текущему файлу, а файлов на ролик почти всегда два: сначала видео, потом
    звук (YouTube отдаёт их раздельно и склеивает в конце). Из-за этого счётчик
    доходил до 100%, падал к нулю и шёл заново — выглядело как сбой.

    Считаем по трём источникам, в порядке надёжности:

    1. grand_total — сумма размеров всех файлов, посчитанная ДО скачивания
       (см. _grand_total ниже). Самый честный вариант. Внутри хука её взять
       неоткуда: yt-dlp кладёт в info_dict только текущий файл.
    2. Номер куска (fragment_index/fragment_count) — для потоковых форматов
       (HLS/m3u8), где размер в байтах неизвестен в принципе, но количество
       кусков известно.
    3. Доля текущего файла — последний запасной вариант.

    В случаях 2 и 3 шкала не доходит до 100%: сколько всего будет файлов, там
    неизвестно, и показать 100% в середине — обмануть.
    """
    state = {"done": 0, "shown": 0.0}

    def hook(d: dict):
        status = d.get("status")
        if status == "finished":
            # файл докачан — его байты уходят в «уже готово»
            state["done"] += d.get("downloaded_bytes") or 0
            return
        if status != "downloading":
            return

        got = d.get("downloaded_bytes") or 0
        exact = False
        if grand_total:
            pct = 100.0 * (state["done"] + got) / grand_total
            exact = True
        else:
            frag_i, frag_n = d.get("fragment_index"), d.get("fragment_count")
            if frag_n:
                # потоковый формат: считаем по кускам, до 95% (файл может
                # оказаться не последним — дальше ещё звук)
                pct = 95.0 * (frag_i or 0) / frag_n
            else:
                total = (d.get("total_bytes")
                         or d.get("total_bytes_estimate") or 0)
                pct = (95.0 * (state["done"] + got) / (state["done"] + total)
                       if total else 0.0)

        if pct:
            # Защёлка: показанное число никогда не уменьшается. Нужна даже при
            # точном размере — yt-dlp при обрыве начинает кусок заново, и
            # счётчик откатывался бы назад. Потолок 95%, пока полный размер
            # неизвестен: файлов может быть ещё один (звук).
            ceiling = 100.0 if exact else 95.0
            state["shown"] = max(state["shown"], min(pct, ceiling))
            pct_str = f"{state['shown']:.0f}%"
        else:
            pct_str = ""

        speed = d.get("_speed_str", "").strip()
        eta = d.get("_eta_str", "").strip()
        # «Unknown» — внутренняя заглушка yt-dlp; пользователю она читается
        # как сбой, поэтому просто не показываем эту часть
        if "unknown" in speed.lower():
            speed = ""
        if "unknown" in eta.lower():
            eta = ""
        parts = [p for p in (pct_str, speed, f"осталось {eta}" if eta else "") if p]
        progress(f"Скачивание… {' · '.join(parts)}" if parts else "Скачивание…")

    return hook


def _grand_total(ydl, url: str) -> int:
    """Суммарный размер всех файлов ролика в байтах; 0 — если неизвестен.

    Считается ДО скачивания: yt-dlp на этом шаге отдаёт список файлов
    (`requested_formats`) с размерами, а во время скачивания в хук приходит
    только текущий файл, и общий размер там уже не собрать.

    Нужен, чтобы процент шёл ровно 0→100 по всему ролику, а не отсчитывался
    заново на каждом файле. Если размер хотя бы одного файла неизвестен
    (потоковые форматы), возвращаем 0 — хук перейдёт на счёт по кускам.
    """
    try:
        info = ydl.extract_info(url, download=False, process=True)
    except Exception:  # noqa: BLE001 — это лишь ради красивого процента
        return 0       # не смогли узнать заранее — не повод срывать загрузку
    formats = (info or {}).get("requested_formats")
    if not formats:
        # один файл без раздельного звука: размер берём прямо из него
        size = (info or {}).get("filesize") or (info or {}).get("filesize_approx")
        return int(size or 0)
    total = 0
    for f in formats:
        size = f.get("filesize") or f.get("filesize_approx") or 0
        if not size:
            return 0        # хоть один неизвестен — сумме верить нельзя
        total += size
    return total


# Сколько места требовать перед скачиванием. Ролик 1080x1920 на минуту — это
# десятки мегабайт, плюс столько же на готовый рендер и превью. 500 МБ — запас,
# при котором заведомо не упрёмся в «No space left» посреди работы.
_MIN_FREE_MB = 500


def _check_free_space() -> None:
    """Проверяет, есть ли куда сохранять. Место кончилось — понятная ошибка
    заранее, а не сырой лог ffmpeg посреди рендера."""
    import shutil
    try:
        free_mb = shutil.disk_usage(DOWNLOADS).free // (1024 * 1024)
    except OSError:
        return          # не смогли посчитать — не мешаем работать
    if free_mb < _MIN_FREE_MB:
        raise RuntimeError(
            f"На диске почти нет места — свободно {free_mb} МБ. "
            "Освободите хотя бы 1 ГБ и попробуйте снова."
        )


def download(url: str, progress: Callable[[str], None] | None = None) -> Path:
    """Скачивает видео по ссылке в workspace/downloads/. Возвращает путь к mp4.

    progress — тот же колбэк, что шлёт статус этапов конвейера (см.
    app/pipeline.py); сюда дополнительно летит % и скорость скачивания.
    """
    import yt_dlp

    _check_free_space()
    stem = uuid.uuid4().hex[:10]
    outtmpl = str(DOWNLOADS / f"{stem}.%(ext)s")
    ydl_opts = {
        # Лучшее видео (по площади кадра) + лучший звук, склеить в mp4.
        # НЕ ограничиваем height<=1080: у вертикальных Shorts 608x1080 имеет
        # height=1080 и ошибочно выигрывает у настоящего 1080x1920. Берём
        # максимум и сортируем по разрешению, чтобы взять полный 1080x1920.
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
        "format_sort": ["res", "vbr"],
        "outtmpl": outtmpl,
        "merge_output_format": "mp4",
        # Путь к НАШЕМУ ffmpeg. Без этого yt-dlp ищет ffmpeg в системе: на
        # рабочем Маке он стоит и всё работало, а на Windows его нет — видео и
        # звук качаются отдельными файлами, склеить их нечем, и загрузка
        # падает. Свой ffmpeg лежит внутри программы (см. app/binaries.py).
        "ffmpeg_location": str(Path(binaries.ffmpeg()).parent),
        "quiet": True,
        "no_warnings": True,
    }
    # для Instagram подставляем куки из браузера (анонимно IG больше не даёт)
    if "instagram.com" in url.lower():
        ydl_opts["cookiesfrombrowser"] = (cookie_browser(),)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            if progress:
                # сперва узнаём общий размер (без скачивания), потом вешаем
                # счётчик — иначе процент считать не от чего
                total = _grand_total(ydl, url)
                ydl.add_progress_hook(_format_progress_hook(progress, total))
            ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as e:
        msg = str(e).lower()
        is_instagram = "instagram.com" in url.lower()
        # IG требует авторизацию, а куки не подхватились/протухли.
        # Проверяем ПЛОЩАДКУ, а не только слова: YouTube тоже пишет про
        # cookies («use --cookies-from-browser»), и ютуб-ссылка получала
        # совет «залогиньтесь в Instagram», который сбивает с толку.
        if is_instagram and any(s in msg for s in (
            "empty media response", "login required", "requires authentication",
            "cookies", "rate-limit", "429",
        )):
            br = cookie_browser().capitalize()
            raise RuntimeError(
                f"Instagram требует вход. Открой Instagram в {br} и залогинься, "
                f"потом попробуй снова (приложение берёт куки из {br}; "
                "браузер меняется во вкладке «Настройки»)."
            )
        # закрытый/приватный ролик
        if any(s in msg for s in (
            "isn't available to everyone", "can't be seen", "private",
        )):
            raise RuntimeError("Ролик закрыт (приватный/ограничен). Возьми другой.")
        # блокировка по стране/IP — у TikTok частый случай: ссылка верная,
        # ролик существует, но с этого адреса его не отдают. Без отдельного
        # текста пользователь читает «проверь ссылку» и правит исправное.
        if any(s in msg for s in (
            "ip address is blocked", "not available in your country",
            "geo restricted", "geo-restricted", "blocked in your country",
            "unavailable in your region",
        )):
            raise RuntimeError(
                "Ролик недоступен с твоего IP — площадка блокирует доступ "
                "из этого региона. Ссылка правильная, дело не в ней: "
                "попробуй включить VPN и повторить."
            )
        # Различаем причины: раньше пустое поле, опечатка и пропавший
        # интернет давали одинаковое «проверь ссылку» — совет бесполезный,
        # когда ссылка верная, а отвалилась сеть.
        if any(s in msg for s in (
            "unable to download", "connection", "timed out", "timeout",
            "temporary failure in name resolution", "network", "getaddrinfo",
            "connection reset", "connection refused", "ssl",
        )):
            raise RuntimeError(
                "Нет связи с сайтом. Проверьте интернет и попробуйте снова — "
                "ссылка тут ни при чём."
            )
        if any(s in msg for s in ("unsupported url", "is not a valid url",
                                  "no suitable extractor")):
            raise RuntimeError(
                "Это не похоже на ссылку на ролик. Скопируйте адрес ролика "
                "из YouTube, Instagram или TikTok целиком."
            )
        if any(s in msg for s in ("video unavailable", "not available",
                                  "has been removed", "does not exist",
                                  "410", "404")):
            raise RuntimeError(
                "Ролик не найден — его удалили или ссылка устарела."
            )
        if ("sign in to confirm" in msg or ("bot" in msg and "confirm" in msg)
                or "not a bot" in msg):
            br = cookie_browser().capitalize()
            raise RuntimeError(
                "YouTube требует подтвердить, что вы не робот. "
                f"Откройте YouTube в {br} и войдите в аккаунт — приложение "
                f"возьмёт данные входа оттуда. Браузер меняется во вкладке "
                "«Настройки»."
            )
        raise RuntimeError(
            "Не удалось скачать ролик. Проверьте, что ссылка открывается "
            "в браузере."
        )

    # yt-dlp мог сохранить не в .mp4 — находим реальный файл по stem
    matches = list(DOWNLOADS.glob(f"{stem}.*"))
    if not matches:
        # техническая ситуация «скачали, но файла нет» — для пользователя это
        # просто неудачная загрузка, внутренний путь ему ничего не скажет
        raise RuntimeError(
            "Ролик скачался, но файл не сохранился. Проверьте, есть ли место "
            "на диске, и попробуйте снова."
        )
    return matches[0]
