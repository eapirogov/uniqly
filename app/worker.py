"""QRunnable-воркер: гоняет функцию в пуле потоков, эмитит сигналы."""
from PySide6.QtCore import QObject, QRunnable, Signal, Slot


class WorkerSignals(QObject):
    stage = Signal(str)          # человекочитаемый статус этапа
    done = Signal(object)        # результат (обычно путь)
    failed = Signal(str)         # текст ошибки


class Worker(QRunnable):
    """Запускает fn(progress_cb) в потоке. progress_cb(str) шлёт .stage."""

    def __init__(self, fn):
        super().__init__()
        self.fn = fn
        self.signals = WorkerSignals()

    @Slot()
    def run(self):
        try:
            result = self.fn(self.signals.stage.emit)
            self.signals.done.emit(result)
        except Exception as e:  # noqa: BLE001 — GUI-воркер, ошибку показываем в UI
            self.signals.failed.emit(str(e))
