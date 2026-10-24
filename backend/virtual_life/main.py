"""Run with: uv run uvicorn virtual_life.main:app --host 127.0.0.1 --port 8000."""

from .api import create_app
from .log import configure_logging
from .settings import Settings

settings = Settings.load()
configure_logging(settings)
app = create_app(settings)
