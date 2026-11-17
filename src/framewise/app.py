"""Application entry point."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import APP_NAME, __version__
from .core import paths
from .core.logging_setup import get_logger, install_excepthook, setup_logging


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="framewise", description=f"{APP_NAME} video editor")
    parser.add_argument("project", nargs="?", type=Path, help="project (.fwproj) or video to open")
    parser.add_argument("--debug", action="store_true", help="verbose logging to the console")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    args = parser.parse_args(argv)

    import logging
    setup_logging(paths.logs_dir(), logging.DEBUG if args.debug else logging.INFO, console=args.debug)
    install_excepthook()
    log = get_logger()
    log.info("Starting %s %s", APP_NAME, __version__)

    from PySide6.QtWidgets import QApplication

    from .project.io import EXTENSION
    from .ui.main_window import MainWindow
    from .ui.theme import apply_theme

    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    apply_theme(app)
    win = MainWindow()
    win.show()
    if args.project:
        if args.project.suffix.lower() == EXTENSION:
            win.open_project(args.project, confirmed=True)
        else:
            win.add_files([str(args.project)])
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
