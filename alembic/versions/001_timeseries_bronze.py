from __future__ import annotations

from pathlib import Path
from alembic import op

revision = "001_timeseries_bronze"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = (Path(__file__).resolve().parents[2] / "migrations" / "001_timeseries_bronze.sql").read_text(encoding="utf-8")
    for statement in sql.split(";"):
        if statement.strip() and not statement.strip().startswith("PRAGMA"):
            op.get_bind().exec_driver_sql(statement)


def downgrade() -> None:
    raise RuntimeError("Timeseries migration is append-only; restore a database backup to downgrade")
