"""Пачечная обработка без карточек на экране.

Раньше пачка создавала по ProcessPanel на каждую ссылку прямо во вкладке
«Работа»: панели качали, рендерили и сами исчезали по готовности. Со стороны
это выглядело так, будто ролики «сыплются» в рабочую область, хотя человек их
там не заказывал — весь смысл пачки в том, чтобы отдать список ссылок и
получить готовые ролики, а не редактировать каждый.

Здесь та же работа идёт фоном: скачать → пересчитать рамку под разрешение
этого ролика → отрендерить → записать в историю. Наружу только сигналы для
окна прогресса (app/gui/batch_progress.py).

Параметры (рамка, ползунки, баннер) уже настроены на эталонном ролике в
app/gui/batch_dialog.py и применяются ко всем одинаково.
"""
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Signal

from app import pipeline, storage

# Разрешение, в котором заданы координаты эталонной рамки. Все три источника
# проекта отдают вертикальные 1080×1920 (см. app/channels.py); если ролик
# другого размера, рамка пересчитывается пропорционально.
_REF_W, _REF_H = 1080, 1920


def rescale_crop(crop, frame_w: int, frame_h: int):
    """Приводит эталонную рамку к разрешению конкретного ролика.

    Без этого рамка, снятая на 1080×1920, уезжала за пределы кадра у ролика
    другого размера, и ffmpeg падал на невалидном crop.
    """
    if not crop:
        return None
    if (frame_w, frame_h) == (_REF_W, _REF_H):
        return crop
    w, h, x, y = crop
    sx, sy = frame_w / _REF_W, frame_h / _REF_H
    w, h = round(w * sx), round(h * sy)
    x, y = round(x * sx), round(y * sy)
    # не вылезаем за кадр: ffmpeg отвергает crop за границами
    w = max(2, min(w, frame_w))
    h = max(2, min(h, frame_h))
    x = max(0, min(x, frame_w - w))
    y = max(0, min(y, frame_h - h))
    return w, h, x, y


class _Signals(QObject):
    stage = Signal(str, str)       # (ссылка, что сейчас происходит)
    downloaded = Signal(str, str)  # (ссылка, путь к исходнику)
    done = Signal(str, str)        # (ссылка, путь к готовому файлу)
    failed = Signal(str, str)      # (ссылка, понятный текст ошибки)


class DownloadItem(QRunnable):
    """Скачивание одной ссылки.

    Отдельно от рендера, потому что у них разные узкие места: скачивание ждёт
    сеть, рендер занимает процессор. Все ссылки качаются одновременно (этого
    так было решено — раньше сначала качался эталон, а остальные ждали), а рендер
    идёт по 2 за раз, иначе параллельные ffmpeg забивают машину.
    """

    def __init__(self, url: str, ready_src: str | None = None):
        super().__init__()
        self.signals = _Signals()
        self.url = url
        # эталонный ролик уже скачан диалогом — качать второй раз незачем
        self.ready_src = ready_src

    def run(self):
        try:
            if self.ready_src and Path(self.ready_src).exists():
                self.signals.downloaded.emit(self.url, self.ready_src)
                return
            src = pipeline.download_step(
                self.url,
                lambda text: self.signals.stage.emit(self.url, text),
            )
            self.signals.downloaded.emit(self.url, str(src))
        except Exception as exc:  # noqa: BLE001 — любая причина уходит в окно
            # тексты ошибок уже человеческие (см. app/downloader)
            self.signals.failed.emit(self.url, str(exc))


class RenderItem(QRunnable):
    """Уникализация одного скачанного ролика с общими параметрами пачки."""

    def __init__(self, url: str, src: str, channel_id: str, crop_rect,
                 uniq_params: dict, banner_path=None):
        super().__init__()
        self.signals = _Signals()
        self.url = url
        self.src = src
        self.channel_id = channel_id
        self.crop_rect = crop_rect
        self.uniq_params = uniq_params
        self.banner_path = banner_path

    def run(self):
        from processors import cropdetect
        try:
            frame_w, frame_h = cropdetect.probe_size(self.src)
            crop = rescale_crop(self.crop_rect, frame_w, frame_h)
            if crop is None:
                # Рамку не задали («Весь кадр») — берём кадр целиком. Без
                # этого пресет подставлял свой давний запасной кроп
                # 1080×1280, и на ролике меньшего размера ffmpeg падал:
                # «Invalid too big or non positive size». В пачке некому
                # это заметить — карточек на экране нет.
                crop = (frame_w - frame_w % 2, frame_h - frame_h % 2, 0, 0)

            result = pipeline.render_with_params(
                Path(self.src), self.channel_id,
                lambda text: self.signals.stage.emit(self.url, text),
                crop_rect=crop, uniq_params=self.uniq_params,
                banner_path=self.banner_path,
            )
            storage.save(
                url=self.url, channel_id=self.channel_id,
                source_path=str(self.src), output_path=result["output"],
                hook="", description="", movie_name="", movie_id="",
                crop_rect=pipeline.encode_crop(crop),
                uniq_params=pipeline.encode_params(self.uniq_params),
                banner_path=self.banner_path or "",
            )
            self.signals.done.emit(self.url, result["output"])
        except Exception as exc:  # noqa: BLE001
            # тексты ошибок уже человеческие (см. app/ffmpeg_error)
            self.signals.failed.emit(self.url, str(exc))
