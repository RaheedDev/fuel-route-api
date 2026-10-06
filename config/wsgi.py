"""WSGI entry point (also used by ``runserver``)."""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

application = get_wsgi_application()

# Load the gazetteer and station index now, not on the first request.
from planner.services.warmup import warm_up  # noqa: E402

warm_up()
