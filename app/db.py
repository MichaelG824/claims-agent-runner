import os

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

DEFAULT_DATABASE_URL = "postgresql+psycopg://runner:runner@localhost:5433/runner"


def get_engine() -> Engine:
    return create_engine(os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL))
