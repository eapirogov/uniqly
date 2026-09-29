"""Готовит bundled_env.py — ключи, которые вшиваются в сборку для друзей.

    venv311/bin/python make_bundled_env.py

Берёт из .env только то, что нужно для поиска информации о фильме
(КиноПоиск, TMDB). Токен бота и доступ к базе Neon НЕ включаются — они
личные, друзьям не нужны и в чужих руках опасны.

Сам bundled_env.py в репозиторий не попадает (.gitignore).
"""
from dotenv import dotenv_values

SHARED = ["KPDEV_API_KEY", "TMDB_API_KEY"]

TEMPLATE = '''"""Ключи, вшитые в сборку. Сгенерировано make_bundled_env.py — не редактировать."""

KEYS = {keys!r}


def apply() -> None:
    """Подставляет ключи в окружение, если их там ещё нет."""
    import os
    for name, value in KEYS.items():
        if value and not os.getenv(name):
            os.environ[name] = value
'''


def main() -> None:
    vals = dotenv_values(".env")
    keys = {name: (vals.get(name) or "").strip() for name in SHARED}
    missing = [n for n, v in keys.items() if not v]
    if missing:
        print("внимание: в .env нет", ", ".join(missing))
    with open("bundled_env.py", "w", encoding="utf-8") as f:
        f.write(TEMPLATE.format(keys=keys))
    print(f"bundled_env.py готов ({sum(1 for v in keys.values() if v)} ключей)")


if __name__ == "__main__":
    main()
