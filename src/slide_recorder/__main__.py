from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from .app import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Slide Recorder")
    app.setOrganizationName("Local")

    window = MainWindow()
    window.resize(1180, 720)
    window.show()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
