from __future__ import annotations
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

def setup_logging(log_dir: str|Path) -> logging.Logger:
    log_dir=Path(log_dir); log_dir.mkdir(parents=True,exist_ok=True)
    logger=logging.getLogger("cinecalendar")
    if logger.handlers: return logger
    logger.setLevel(logging.INFO)
    h=RotatingFileHandler(log_dir/"CineCalendar.log",maxBytes=2_000_000,backupCount=5,encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
    logger.addHandler(h)
    return logger
