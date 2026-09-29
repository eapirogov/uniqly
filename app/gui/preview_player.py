"""Мини-плеер отрендеренного клипа/ролика — используется как для
превью-клипа с эффектами (app/gui/uniq_editor.py, клик по превью-кадру), так
и потенциально для готового ролика после «Применить».

Пробовали live-имитацию без ffmpeg-рендера (QMediaPlayer играет оригинальный
исходник напрямую, эффекты накладываются в реальном времени поверх
QGraphicsVideoItem через setScale/setRotation/composition-mode оверлеи) —
теоретически мгновенный отклик, как в CapCut. На offscreen-рендере (headless
тест) работало корректно, но на реальном экране macOS видео-слой
QGraphicsVideoItem рендерится ОТДЕЛЬНЫМ GPU-composited native layer поверх
Qt-сцены и игнорирует scene-клиппинг/позицию — обрезка визуально не
применялась, хотя расчёт геометрии был верным. Это известное ограничение
Qt Multimedia на macOS (video item композитится вне обычного Qt paint-пути).

Поэтому вернулись к честному ffmpeg-рендеру (pipeline.preview_clip_with_params)
— дольше (рендер первых 5с при клике), зато результат идентичен тому, что
получится в финальном ролике, без сюрпризов платформы.
"""
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QSlider,
    QVBoxLayout, QWidget,
)

from app.gui import theme


class _RatioBox(QWidget):
    """Держит вложенный виджет строго в заданной пропорции.

    Нужен, потому что QVideoWidget сам вписывает картинку по высоте своей
    области. Область под видео получается выше 9:16 (снизу от неё отъедает
    полоска с кнопкой и таймлайном), поэтому вписанный кадр не дотягивался до
    правого края — и справа висела чёрная полоса, которой в самом файле нет
    (в ролике поля только сверху и снизу, см. pipeline.preview_clip_with_params).
    """

    def __init__(self, inner: QWidget, ratio: float, parent=None):
        super().__init__(parent)
        self._inner = inner
        self._ratio = ratio          # ширина / высота
        inner.setParent(self)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def resizeEvent(self, event):
        w, h = self.width(), self.height()
        # берём меньшее из «по ширине» и «по высоте», чтобы кадр влез целиком
        iw = min(w, round(h * self._ratio))
        ih = min(h, round(w / self._ratio))
        self._inner.setGeometry((w - iw) // 2, (h - ih) // 2, iw, ih)
        super().resizeEvent(event)


class RenderedPlayerDialog(QDialog):
    """Компактный плеер уже отрендеренного файла (ffmpeg-клип превью или
    готовый ролик) — честное воспроизведение без live-эффектов."""

    def __init__(self, clip_path: Path, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Предпросмотр с эффектами")
        self.resize(360, 640)
        self.setStyleSheet(f"background:{theme.BG};")

        root = QVBoxLayout(self)
        self.video_widget = QVideoWidget()
        # видео держим в пропорции 9:16 — как готовый ролик; иначе QVideoWidget
        # растянул бы свою область на всю оставшуюся высоту и дорисовал чёрные
        # поля по бокам
        self.video_box = _RatioBox(self.video_widget, 9 / 16)
        root.addWidget(self.video_box, 1)

        controls = QHBoxLayout()
        self.play_btn = QPushButton("⏸")
        self.play_btn.setFixedWidth(36)
        self.play_btn.clicked.connect(self._toggle_play)
        controls.addWidget(self.play_btn)
        self.position_slider = QSlider(Qt.Horizontal)
        self.position_slider.sliderMoved.connect(self._seek)
        controls.addWidget(self.position_slider, 1)
        self.time_label = QLabel("0:00")
        self.time_label.setStyleSheet(f"color:{theme.TEXT_MUTED};font-size:11px;")
        controls.addWidget(self.time_label)
        root.addLayout(controls)

        self.player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.player.setAudioOutput(self.audio_output)
        self.player.setVideoOutput(self.video_widget)
        self.player.positionChanged.connect(self._on_position)
        self.player.durationChanged.connect(self._on_duration)
        self.player.playbackStateChanged.connect(self._on_state)
        self.player.errorOccurred.connect(self._on_error)

        self.player.setSource(QUrl.fromLocalFile(str(clip_path)))
        self.player.play()

    def _toggle_play(self):
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def _on_state(self, state):
        self.play_btn.setText("⏸" if state == QMediaPlayer.PlayingState else "▶")

    def _seek(self, value: int):
        self.player.setPosition(value)

    def _on_position(self, position: int):
        self.position_slider.blockSignals(True)
        self.position_slider.setValue(position)
        self.position_slider.blockSignals(False)
        secs = position // 1000
        self.time_label.setText(f"{secs // 60}:{secs % 60:02d}")

    def _on_duration(self, duration: int):
        self.position_slider.setRange(0, duration)

    def _on_error(self, _error, error_string: str):
        self.time_label.setText(f"ошибка: {error_string}")

    def closeEvent(self, event):
        self.player.stop()
        super().closeEvent(event)
