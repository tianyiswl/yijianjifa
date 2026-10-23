from __future__ import annotations

import sys

from yijianjifa import APP_NAME
from yijianjifa.os_integration.paths import get_app_paths


def main() -> int:
    """Start the desktop application.

    The Qt import stays local so non-UI service tests can run in headless
    environments that do not have the desktop runtime installed.
    """
    paths = get_app_paths()
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError as exc:  # pragma: no cover - verified in UI-capable env.
        raise RuntimeError("PySide6 is required to run the desktop UI") from exc

    app = QApplication.instance() or QApplication(sys.argv)
    from yijianjifa.storage.db import create_engine_for, migrate_to_head
    from yijianjifa.app.main_window import MainWindow
    app.setApplicationName(APP_NAME)
    engine = create_engine_for(paths.data_dir / "app.db")
    migrate_to_head(engine)
    window = MainWindow(engine, paths)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
