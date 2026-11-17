"""Create the staging application and observability PostgreSQL roles safely."""

from __future__ import annotations

import os

import psycopg
from psycopg import sql


def _required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(f"Missing required database bootstrap setting: {name}")
    return value


def main() -> None:
    host = _required("DATABASE_HOST")
    port = int(os.environ.get("DATABASE_PORT", "5432"))
    master_user = _required("RDS_MASTER_USERNAME")
    master_password = _required("RDS_MASTER_PASSWORD")
    app_password = _required("APP_DB_PASSWORD")
    langfuse_password = _required("LANGFUSE_DB_PASSWORD")

    with psycopg.connect(
        host=host,
        port=port,
        dbname="postgres",
        user=master_user,
        password=master_password,
        sslmode="require",
        autocommit=True,
    ) as admin:
        with admin.cursor() as cursor:
            _ensure_role(cursor, "research_app", app_password)
            _ensure_role(cursor, "langfuse_app", langfuse_password)
            _ensure_database(cursor, "research", "research_app")
            _ensure_database(cursor, "langfuse", "langfuse_app")

    with psycopg.connect(
        host=host,
        port=port,
        dbname="research",
        user=master_user,
        password=master_password,
        sslmode="require",
        autocommit=True,
    ) as admin:
        with admin.cursor() as cursor:
            cursor.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cursor.execute("GRANT USAGE, CREATE ON SCHEMA public TO research_app")

    print("Staging database roles and vector extension are ready.")


def _ensure_role(cursor: psycopg.Cursor, name: str, password: str) -> None:
    cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,))
    exists = cursor.fetchone() is not None
    statement = sql.SQL(
        "ALTER ROLE {} WITH LOGIN PASSWORD {}" if exists else "CREATE ROLE {} LOGIN PASSWORD {}"
    ).format(sql.Identifier(name), sql.Literal(password))
    cursor.execute(statement)


def _ensure_database(cursor: psycopg.Cursor, name: str, owner: str) -> None:
    cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
    exists = cursor.fetchone() is not None
    if exists:
        cursor.execute(
            sql.SQL("ALTER DATABASE {} OWNER TO {}").format(
                sql.Identifier(name), sql.Identifier(owner)
            )
        )
    else:
        cursor.execute(
            sql.SQL("CREATE DATABASE {} OWNER {}").format(
                sql.Identifier(name), sql.Identifier(owner)
            )
        )


if __name__ == "__main__":
    main()
