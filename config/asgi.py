"""ASGI entry point (e.g. ``uvicorn config.asgi:application``)."""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

application = get_asgi_application()

from planner.services.warmup import warm_up  # noqa: E402

warm_up()
