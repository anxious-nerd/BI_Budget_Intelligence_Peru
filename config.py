"""Configuración central del proyecto.

Todo se puede sobreescribir con variables de entorno o con un archivo `.env`
(ver `.env.example`). Se lee en cada llamada a `get_settings()`, de modo que los
tests pueden cambiar el entorno sin reimportar nada.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent

try:  # python-dotenv es opcional
    from dotenv import load_dotenv

    # Determinista: primero el .env junto al proyecto, luego el del directorio actual.
    # Las variables ya definidas en el entorno tienen prioridad sobre el archivo.
    load_dotenv(ROOT / ".env")
    load_dotenv(Path.cwd() / ".env")
except ImportError:  # pragma: no cover
    pass

EXEC_MODES = ("cumulative", "flow")
# Cómo se registran los SALDOS PRESUPUESTALES (PIA y PIM) mes a mes en el origen
# (decídelo con `python -m etl.profile`):
#   snapshot: son un SALDO repetido cada mes (o solo en el mes 0 de apertura) -> el vigente al
#             corte es el valor del último mes <= corte con saldo.
#   sum:      se registran UNA VEZ (p. ej. PIA en el primer mes) o como MODIFICACIONES del periodo
#             (PIM) -> el vigente al corte es SUM(mes <= corte).
BUDGET_MODES = ("snapshot", "sum")


@dataclass(frozen=True)
class Settings:
    # --- Origen (lo que ya tienes) -------------------------------------------------
    # Puede ser un glob de parquet ("data/raw/gasto/*.parquet") o el nombre de una
    # relación DuckDB ("raw.gasto") si usas MEF_ATTACH.
    raw_gasto: str
    raw_ingreso: str
    # "alias=ruta/a/tu.duckdb;otro=ruta2.duckdb" -> se adjuntan en modo solo lectura.
    attach: str
    # Expresión regular: los archivos cuyo nombre la cumpla se IGNORAN (p. ej. los
    # "*-Diario.csv" de 2025-2026, que duplicarían los "*-Mensual.csv").
    exclude_files_regex: str

    # --- Tabla de ubigeos (latitud/longitud) ----------------------------------------------
    # CSV de datos abiertos "Tabla de Ubigeos del Perú" (separador ';', coma decimal).
    ubigeo_table: Path
    # Con qué columna de esa tabla se cruza el código que arma el ETL (depto+provincia+distrito del MEF):
    # 'inei' o 'reniec'. Los dos sistemas NO coinciden en la mayoría de distritos: verifícalo con
    # `python -m etl.profile`, que mide cuál de los dos calza con tus datos.
    ubigeo_code: str

    # --- Destino --------------------------------------------------------------------
    warehouse: Path

    # --- Semántica de los montos por mes (ver README, paso 3) -------------------------
    #   cumulative: certificado/comprometido/devengado/girado/recaudado vienen
    #               ACUMULADOS al mes -> el ETL los convierte a flujo mensual.
    #   flow:       ya vienen como flujo del mes -> se dejan tal cual.
    exec_mode: str
    budget_mode_gasto: str
    budget_mode_ingreso: str

    # --- Recursos de DuckDB ---------------------------------------------------------
    memory_limit: str
    threads: int  # 0 = todos los núcleos
    temp_dir: Path


def get_settings(**overrides) -> Settings:
    env = os.environ.get
    wh = Path(env("MEF_WAREHOUSE", ROOT / "warehouse")).expanduser().resolve()
    s = Settings(
        raw_gasto=env("MEF_RAW_GASTO", str(ROOT / "data/raw/gasto/*.parquet")),
        raw_ingreso=env("MEF_RAW_INGRESO", str(ROOT / "data/raw/ingreso/*.parquet")),
        attach=env("MEF_ATTACH", ""),
        exclude_files_regex=env("MEF_EXCLUDE_FILES_REGEX", "(?i)diario"),
        ubigeo_table=Path(env("MEF_UBIGEO_TABLE", ROOT / "data/ref/TB_UBIGEOS.csv")).expanduser(),
        ubigeo_code=env("MEF_UBIGEO_CODE", "inei").strip().lower(),
        warehouse=wh,
        exec_mode=env("MEF_EXEC_MODE", "cumulative").strip().lower(),
        budget_mode_gasto=env("MEF_BUDGET_MODE_GASTO", "snapshot").strip().lower(),
        budget_mode_ingreso=env("MEF_BUDGET_MODE_INGRESO", "sum").strip().lower(),
        memory_limit=env("MEF_MEMORY_LIMIT", "2GB"),
        threads=int(env("MEF_THREADS", "0")),
        temp_dir=Path(env("MEF_TEMP_DIR", wh / ".duckdb_tmp")).expanduser().resolve(),
    )
    if overrides:
        s = Settings(**{**s.__dict__, **overrides})
    if s.exec_mode not in EXEC_MODES:
        raise ValueError(f"MEF_EXEC_MODE debe ser uno de {EXEC_MODES}, no '{s.exec_mode}'")
    if s.ubigeo_code not in ("inei", "reniec"):
        raise ValueError(f"MEF_UBIGEO_CODE debe ser 'inei' o 'reniec', no '{s.ubigeo_code}'")
    for name in ("budget_mode_gasto", "budget_mode_ingreso"):
        if getattr(s, name) not in BUDGET_MODES:
            raise ValueError(f"MEF_{name.upper()} debe ser uno de {BUDGET_MODES}, no '{getattr(s, name)}'")
    return s
