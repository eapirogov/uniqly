"""Рекламный баннер: два файла (ProRes 4444 с альфой), чередуются через ролик.

Появляется со 2-й секунды через fade-in, зациклен до конца видео (баннер короче
ролика — крутится по кругу). Кладётся по центру X на границе видео/фона.
Зацикливание — через `-stream_loop -1` на входе баннера + `-shortest` в пресете.
"""
from pathlib import Path

from app import storage
from app.config import ROOT, WORKSPACE

# предустановленные баннеры, ProRes 4444 с альфа-каналом.
# Файлы в репозиторий не входят — кладутся локально в assets/banners/.
BANNERS = [
    ROOT / "assets" / "banners" / "banner_1.mov",
    ROOT / "assets" / "banners" / "banner_2.mov",
]

FADE_START = 2  # секунда появления баннера

# ГЛОБАЛЬНЫЙ ВЫКЛЮЧАТЕЛЬ баннера. False = баннер заморожен (не накладывается ни на
# один канал), True = включён. Меняется одной строкой, код баннера остаётся.
ENABLED = False


def is_enabled() -> bool:
    return ENABLED


def library_dir() -> Path:
    """Папка пользовательских баннеров (app/gui/process_panel.py), отдельно
    от предустановленных assets/banners/ — added-by-user, не в git.

    Берётся из config.WORKSPACE, а не «рядом с кодом»: в собранной программе
    ROOT указывает внутрь .app, куда macOS запрещает писать.
    """
    d = WORKSPACE / "banners"
    d.mkdir(parents=True, exist_ok=True)
    return d


def pick(channel_id: str) -> Path:
    """Выбирает следующий баннер для канала (чередование 1↔2)."""
    idx = storage.next_banner_index(channel_id, len(BANNERS))
    return BANNERS[idx]


def overlay_chain(in_label: str, out_label: str, width: int, center_y: int,
                  banner_input: int = 2) -> str:
    """Строит участок filter_complex: масштаб + fade + наложение баннера.

    banner_input — индекс входа баннера (reels: 2, crop: 1). in_label — метка
    кадра до баннера, out_label — итог. width — ширина, center_y — Y центра.
    """
    return (
        f"[{banner_input}:v]scale={width}:-1,fade=in:st={FADE_START}:d=1:alpha=1,"
        f"setpts=PTS-STARTPTS[banner];"
        f"[{in_label}][banner]overlay=(W-w)/2:{center_y}-h/2:"
        f"enable='gte(t,{FADE_START})'[{out_label}]"
    )
