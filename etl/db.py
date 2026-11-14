"""Conexión a DuckDB y resolución del origen de datos."""
from __future__ import annotations

from pathlib import Path

import duckdb

from config import Settings


def connect(settings: Settings) -> duckdb.DuckDBPyConnection:
    """Conexión en memoria con derrame a disco (`temp_dir`) para años muy pesados.

    El ETL no necesita un archivo .duckdb: lee parquet y escribe parquet.
    """
    settings.temp_dir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(":memory:")
    con.execute(f"SET memory_limit='{settings.memory_limit}'")
    con.execute(f"SET temp_directory='{settings.temp_dir.as_posix()}'")
    if settings.threads:
        con.execute(f"SET threads={int(settings.threads)}")
    # Menos memoria en COPY/GROUP BY grandes; no necesitamos orden de inserción.
    con.execute("SET preserve_insertion_order=false")
    con.execute("SET enable_progress_bar=false")  # DuckDB 2.x la imprime en logs/no-TTY
    for item in filter(None, (s.strip() for s in settings.attach.split(";"))):
        alias, _, path = item.partition("=")
        con.execute(f"ATTACH '{path.strip()}' AS {alias.strip()} (READ_ONLY)")
    return con


def source_sql(raw: str) -> str:
    """Convierte el valor de configuración en una expresión válida para FROM.

    - "data/raw/gasto/*.parquet"  -> read_parquet(...)   (glob)
    - "data/raw/gasto"            -> read_parquet('.../**/*.parquet')   (carpeta)
    - "raw.gasto"                 -> tabla/vista DuckDB tal cual (requiere MEF_ATTACH)
    """
    p = Path(raw)
    if p.is_dir():
        raw = (p / "**" / "*.parquet").as_posix()
    elif any(ch in raw for ch in "*?[/\\") or raw.lower().endswith(".parquet"):
        raw = raw.replace("\\", "/")
    else:
        return raw
    return f"read_parquet('{raw}', union_by_name=true, filename=true)"


def describe(con: duckdb.DuckDBPyConnection, source: str) -> dict[str, str]:
    """{columna_en_minúsculas: tipo} del origen (solo lee metadatos del parquet)."""
    rows = con.execute(f"DESCRIBE SELECT * FROM {source}").fetchall()
    return {r[0].lower(): str(r[1]).upper() for r in rows}


def sql_str(value: str) -> str:
    """Literal SQL seguro para una cadena."""
    return "'" + value.replace("'", "''") + "'"
