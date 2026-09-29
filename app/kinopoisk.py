"""КиноПоиск (kinopoisk.dev): поиск фильма по названию + сборка Reels-описания.

Формат описания повторяет reels_template из бота (main.py).
"""
import os
import re

import httpx

from app import config  # грузит .env → ключ доступен через os.getenv

KPDEV_BASE = "https://api.poiskkino.dev/v1.4"

# 20 секунд человек ждёт как зависшую программу, а при обрыве связи столько
# ждать бессмысленно — ответа уже не будет. 8 секунд хватает с запасом:
# нормальный ответ приходит за полсекунды.
TIMEOUT = 8.0
ATTEMPTS = 2      # сервис бывает нестабилен, вторая попытка часто проходит


class ServiceUnavailable(Exception):
    """Не удалось достучаться до сервиса (нет связи, таймаут, сбой сервера).

    Отдельный тип ошибки нужен, чтобы отличать «связи нет» от «фильм не
    найден» — снаружи это выглядело одинаково («Описание не найдено»), и
    человек думал, что дело в названии фильма.
    """


# Шапка описания: первая строка, если у канала включена галочка
# «Добавлять шапку» в настройках. Текст свой у каждого — меняется здесь.
DESC_HEADER = (
    "📹 Смотри фильм в хорошем качестве — ссылка в шапке профиля 🎞️"
)


def _get(path: str, params: dict | None = None) -> dict | None:
    """Запрос к API. None — ответ получен, но пустой/не 200 по вине запроса.
    ServiceUnavailable — до сервиса не достучались вовсе."""
    headers = {"X-API-KEY": os.getenv("KPDEV_API_KEY", ""), "accept": "application/json"}
    last_error = ""
    for attempt in range(1, ATTEMPTS + 1):
        try:
            r = httpx.get(f"{KPDEV_BASE}{path}", headers=headers,
                          params=params, timeout=TIMEOUT)
            if r.status_code == 200:
                return r.json()
            print(f"[kinopoisk] {path} → HTTP {r.status_code}: {r.text[:200]}")
            # 5xx — проблема на стороне сервиса, имеет смысл повторить;
            # 4xx (неверный ключ, лимит) повторять бесполезно
            if r.status_code >= 500 and attempt < ATTEMPTS:
                last_error = f"сервис отвечает ошибкой {r.status_code}"
                continue
            if r.status_code >= 500:
                raise ServiceUnavailable(f"сервис отвечает ошибкой {r.status_code}")
            if r.status_code in (401, 403):
                raise ServiceUnavailable("ключ доступа не принят")
            if r.status_code == 429:
                raise ServiceUnavailable("слишком много запросов, подожди немного")
            return None
        except httpx.HTTPError as e:
            last_error = _human_error(e)
            print(f"[kinopoisk] {path} → {type(e).__name__}: {e} (попытка {attempt})")
            if attempt >= ATTEMPTS:
                raise ServiceUnavailable(last_error) from e
    raise ServiceUnavailable(last_error or "сервис недоступен")


def _human_error(e: Exception) -> str:
    """Техническую ошибку httpx — в понятную человеку фразу."""
    name = type(e).__name__
    if "Timeout" in name:
        return "сервис не отвечает"
    if "Connect" in name or "Network" in name:
        return "нет связи с сервисом"
    return "сбой связи"


def search(query: str) -> list[dict]:
    """Поиск фильма по названию. Возвращает список вариантов [{id, name, year}]."""
    data = _get("/movie/search", {"query": query, "limit": 10, "page": 1})
    if not data or not data.get("docs"):
        return []
    out = []
    for m in data["docs"]:
        out.append({
            "id": m.get("id"),
            "name": m.get("name") or m.get("alternativeName") or "Без названия",
            "year": m.get("year") or "—",
        })
    return out


def _clean(text: str, limit: int = 800) -> str:
    if not text:
        return "Описание скоро появится... 🍿"
    text = re.sub(r"<.*?>", "", text).replace("\r", "").strip()
    if len(text) <= limit:
        return text
    trimmed = text[:limit]
    m = re.search(r"([.!?…])(?=[^.!?…]*$)", trimmed)
    return trimmed[:m.start() + 1] if m else trimmed[:trimmed.rfind(" ")] + "..."


def _ratings(kp, imdb) -> tuple:
    # округляем до 1 знака, как в боте (6.863 → 6.9)
    kp = round(float(kp or 0), 1)
    imdb = round(float(imdb or 0), 1)
    if not kp and not imdb:
        return "—", "—"
    if not imdb and kp:
        imdb = round(kp - 0.2, 1)
    return (kp if kp else "—"), (imdb if imdb else "—")


def build_reels_description(
    movie_id: int,
    header: bool = True,
    signature: str = "",
) -> str | None:
    """Собирает Reels-описание по id фильма под формат канала.

    header — добавить шапку DESC_HEADER; signature — фикс. подпись в конце.
    Хештеги не добавляются (убраны у всех каналов).
    """
    m = _get(f"/movie/{movie_id}")
    if not m:
        return None

    name = m.get("name") or m.get("alternativeName") or "Без названия"
    year = m.get("year") or "????"
    rating = m.get("rating") or {}
    kp_str, imdb_str = _ratings(rating.get("kp"), rating.get("imdb"))
    genres = [g.get("name") for g in (m.get("genres") or []) if g.get("name")]
    genre_main = genres[0].capitalize() if genres else "Не указан"
    actors = [p.get("name") for p in (m.get("persons") or [])
              if p.get("enProfession") == "actor" and p.get("name")][:4]
    actors_str = ", ".join(actors) if actors else "Не указаны"
    desc = _clean(m.get("description") or "")

    parts = []
    if header:
        parts.append(DESC_HEADER)
    parts.append(
        f"🍿Фильм: «{name}» ({year})🎬\n"
        f"⭐️Рейтинг: IMDb: {imdb_str} | Кинопоиск: {kp_str}⭐️\n"
        f"🎥Жанр: {genre_main}🎥\n"
        f"🎭В главных ролях: {actors_str}🎭"
    )
    parts.append(f"🍿Описание:\n\n{desc}")
    if signature:
        parts.append(signature)
    # блоки разделяем пустой строкой (\n\n); внутри блока переносы одинарные
    return "\n\n".join(parts)
