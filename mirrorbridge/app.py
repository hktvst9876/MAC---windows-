"""Application entry point."""

import argparse
import logging
import logging.handlers
import os
import sys

from .config import APP_NAME, APP_VERSION, AppConfig, config_dir


def setup_logging(debug=False):
    folder = config_dir()
    folder.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if debug else logging.INFO
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    file_handler = logging.handlers.RotatingFileHandler(folder / "mirrorbridge.log", maxBytes=2_000_000,
                                                        backupCount=2, encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    if sys.stderr is not None:
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        root.addHandler(console)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("zeroconf").setLevel(logging.WARNING)
    logging.getLogger("libav").setLevel(logging.ERROR)


def main(argv=None):
    parser = argparse.ArgumentParser(prog=APP_NAME.lower(), description="AirPlay screen-mirroring receiver")
    parser.add_argument("--name", help="receiver name shown to devices")
    parser.add_argument("--port", type=int, help="AirPlay port (default 7100)")
    parser.add_argument("--debug", action="store_true", help="verbose logging")
    parser.add_argument("--version", action="version", version="%s %s" % (APP_NAME, APP_VERSION))
    args = parser.parse_args(argv)

    setup_logging(args.debug)
    log = logging.getLogger("mirrorbridge")
    log.info("%s %s starting (%s)", APP_NAME, APP_VERSION, sys.platform)

    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("MirrorBridge.Receiver")
        except Exception:
            pass

    from PySide6.QtCore import QLockFile
    from PySide6.QtWidgets import QApplication, QMessageBox

    from .core.controller import Controller
    from .ui import icons
    from .ui.main_window import MainWindow

    app = QApplication(sys.argv if argv is None else [APP_NAME] + list(argv))
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName(APP_NAME)
    app.setWindowIcon(icons.app_icon())
    app.setQuitOnLastWindowClosed(False)

    lock = QLockFile(str(config_dir() / "instance.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        QMessageBox.information(None, APP_NAME, "%s is already running." % APP_NAME)
        return 0

    config = AppConfig.load()
    if args.name:
        config.name = args.name
    if args.port:
        config.port = args.port

    controller = Controller(config)
    window = MainWindow(controller, config)
    window.show()
    controller.start()
    app.aboutToQuit.connect(controller.shutdown)
    code = app.exec()
    lock.unlock()
    return code


if __name__ == "__main__":
    sys.exit(main())
