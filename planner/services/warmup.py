import logging
import time

from django.db import DatabaseError

from . import gazetteer
from .stations import get_station_index

logger = logging.getLogger(__name__)


def warm_up():
    """Pre-load in-memory data so the first API request is as fast as later ones."""
    started = time.perf_counter()
    gazetteer.find_place("Chicago", "IL")  # loads the places index
    gazetteer.find_zip("60601")
    gazetteer.is_in_usa(41.88, -87.63)
    try:
        stations = len(get_station_index())
    except DatabaseError:  # e.g. migrations not applied yet
        stations = 0
    logger.info("Warm-up done in %.0f ms (%d stations indexed)",
                (time.perf_counter() - started) * 1000, stations)
