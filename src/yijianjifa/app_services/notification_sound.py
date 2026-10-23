"""Page-independent sound delivery; historical notifications are never replayed."""
import threading
from yijianjifa.features.notifications.service import NotificationService


class NotificationSoundPump:
    def __init__(self, engine, settings, play):
        self.service=NotificationService(engine)
        self.settings=settings
        self.play=play
        self._seen={n.id for n in self.service.history()}
        self._stop=threading.Event()
        self._thread=None

    def poll_once(self):
        for note in reversed(self.service.history()):
            if note.id in self._seen:continue
            self._seen.add(note.id)
            if self.settings.notification_sound_enabled:self.play()

    def start(self):
        if self._thread is not None:return
        def run():
            while not self._stop.wait(.5):
                try:self.poll_once()
                except Exception:
                    # Sound failure must not break queue execution or notification history.
                    pass
        self._thread=threading.Thread(target=run,name='p011-notification-sound',daemon=False)
        self._thread.start()

    def close(self, timeout=0):
        self._stop.set()
        if self._thread:self._thread.join(timeout)
        return self._thread is None or not self._thread.is_alive()
