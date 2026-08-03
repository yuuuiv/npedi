from __future__ import annotations

from pathlib import Path
from alembic import op

revision = "002_silver_gold"
down_revision = "001_timeseries_bronze"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sql = (Path(__file__).resolve().parents[2] / "migrations" / "002_silver_gold.sql").read_text(encoding="utf-8")
    for statement in sql.split(";"):
        if statement.strip() and not statement.strip().startswith("PRAGMA"):
            op.get_bind().exec_driver_sql(statement)


def downgrade() -> None:
    raise RuntimeError("Timeseries migration is append-only; restore a database backup to downgrade")
