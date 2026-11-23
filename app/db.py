# os: read environment variables.
import os

# create_engine: builds a SQLAlchemy connection pool from a database URL.
from sqlalchemy import create_engine
# Engine: the type returned by create_engine, used for the type hint.
from sqlalchemy.engine import Engine

# Local Postgres from docker-compose: user "runner", password "runner", port 5433, database "runner", psycopg driver.
DEFAULT_DATABASE_URL = "postgresql+psycopg://runner:runner@localhost:5433/runner"


# Return a database engine; callers use it to open connections.
def get_engine() -> Engine:
    # Use DATABASE_URL from the environment if set, otherwise the local default.
    return create_engine(os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL))
