import logging
import sys
from contextlib import contextmanager
from pathlib import Path
from time import time
import os

from airo_ipc.cyclone_shm.cantrips.configs import load_config

class MyPathFilter(logging.Filter):
    def __init__(self, basepath):
        super().__init__()
        self.basepath = os.path.abspath(basepath)

    def filter(self, record):
        # Only logs where the pathname starts with your code directory will pass
        return os.path.abspath(record.pathname).startswith(self.basepath)

def get_logger():
    config = load_config()

    basename = os.path.basename(sys.argv[0]).replace(".py", "")
    log_filename = f"{int(time()) - 1719520335}_{basename}_{os.getpid()}.log"

    logger = logging.getLogger(basename)

    handlers = [logging.StreamHandler()]
    if config.file_logging:
        handlers.append(logging.FileHandler(Path(config.filepath) / log_filename))

    logging.basicConfig(
        level=eval(f"logging.{config.level}"),
        format=config.format,
        handlers=handlers,
    )

    logger.setLevel(eval(f"logging.{config.level}"))

    # Adjust the base path to point to your own project's root directory
    base_path = "/path/to/your/codebase"
    path_filter = MyPathFilter(base_path)

    for handler in logger.handlers:
        handler.addFilter(path_filter)

    return logger

@contextmanager
def shht():
    logger = logging.getLogger()
    previous_level = logger.getEffectiveLevel()
    logger.setLevel(logging.WARNING)
    try:
        yield
    finally:
        logger.setLevel(previous_level)
