"""Fixtures: datos sintéticos pequeños + warehouses construidos (una vez por sesión)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import get_settings  # noqa: E402
from etl import pipeline  # noqa: E402
from scripts.make_sample_data import generate  # noqa: E402

YEARS = (2023, 2024)


def _build(tmp_path_factory, mode: str, name: str, style: str = "snapshot", budget=("snapshot", "snapshot")):
    base = tmp_path_factory.mktemp(name)
    raw = base / "raw"
    generate(raw, years=YEARS, entidades=60, claves=6, mode=mode, diario=True, stock_style=style)
    settings = get_settings(
        raw_gasto=str(raw / "gasto" / "*.parquet"),
        raw_ingreso=str(raw / "ingreso" / "*.parquet"),
        warehouse=base / "wh",
        temp_dir=base / "tmp",
        exec_mode=mode,
        budget_mode_gasto=budget[0],
        budget_mode_ingreso=budget[1],
        memory_limit="1GB",
        ubigeo_table=ROOT / "data" / "ref" / "TB_UBIGEOS.csv",
        ubigeo_code="inei",
    )
    pipeline.run(settings)
    return settings


@pytest.fixture(scope="session")
def wh_cumulative(tmp_path_factory):
    return _build(tmp_path_factory, "cumulative", "cum")


@pytest.fixture(scope="session")
def wh_flow(tmp_path_factory):
    return _build(tmp_path_factory, "flow", "flow")


@pytest.fixture(scope="session")
def wh_spec(tmp_path_factory):
    """Datos como los describe el README del equipo: gasto con PIA/PIM en el mes 0 de apertura;
    ingreso con PIA registrado una vez y PIM como modificaciones del periodo."""
    return _build(tmp_path_factory, "cumulative", "spec", style="spec", budget=("snapshot", "sum"))
