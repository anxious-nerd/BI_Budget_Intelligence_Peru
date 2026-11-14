"""Verifica el warehouse REAL del proyecto contra el README-modelo del equipo.

    python -m scripts.check_spec                                   # usa docs/modelo_objetivo.md y MEF_WAREHOUSE
    python -m scripts.check_spec --spec otro/README.md --warehouse warehouse --strict

Lee las tablas (### DIM_* / ### FACT_*) del README y compara, por cada una, contra el parquet que
produjo el ETL:
    * columnas: deben existir TODAS las del modelo (FALTA) y no sobrar ninguna (EXTRA), mismo orden;
    * VARCHAR(n): el dato real no debe exceder el largo declarado en el modelo (DESBORDE);
    * tipos: se informa dónde el proyecto se aparta a propósito (claves hash, DECIMAL, DOUBLE).
La partición física por año (carpeta anio=YYYY) no cuenta como columna.
Con --strict el código de salida es 1 si hay FALTA, EXTRA o DESBORDE.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import duckdb

from config import get_settings
from etl import pipeline
from etl.model import ALL_DIMS, DATASETS

ROOT = Path(__file__).resolve().parents[1]


def parse_spec(text: str) -> dict[str, list[dict]]:
    spec: dict[str, list[dict]] = {}
    cur = None
    for line in text.splitlines():
        m = re.match(r"^###\s+(DIM_\w+|FACT_\w+)\s*$", line.strip())
        if m:
            cur = m.group(1)
            spec[cur] = []
            continue
        if line.startswith("## "):
            cur = None
        if cur and line.strip().startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) >= 3 and cells[0] != "Nombre de columna" and not set(cells[0]) <= set("-: "):
                spec[cur].append({"col": cells[0], "type": cells[1], "desc": "|".join(cells[2:])})
    return spec


def table_source(wh: Path, name: str) -> str | None:
    """FROM de la tabla del proyecto que corresponde a un nombre del modelo (DIM_X / FACT_X)."""
    t = name.lower()
    if t.startswith("fact_"):
        ds = next((d for d in DATASETS.values() if d.fact == t), None)
        return f"(SELECT * EXCLUDE (anio) FROM {pipeline.fact_source(wh, ds)})" if ds and (wh / ds.fact).exists() else None
    dim = next((d for d in ALL_DIMS if d.table == t), None)
    if dim and ((wh / dim.table).exists() if dim.by_year else (wh / f"{dim.table}.parquet").exists()):
        return pipeline.dim_source(wh, dim)
    if t == "dim_tiempo" and (wh / "dim_tiempo.parquet").exists():
        return f"read_parquet('{(wh / 'dim_tiempo.parquet').as_posix()}')"
    return None


def type_family(spec_type: str) -> str:
    s = spec_type.upper()
    return "VARCHAR" if s.startswith("VARCHAR") else "INT" if s.startswith("INT") else "FLOAT" if s in ("FLOAT", "DOUBLE", "REAL") else s


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--spec", type=Path, default=ROOT / "docs" / "modelo_objetivo.md")
    ap.add_argument("--warehouse", type=Path, default=None)
    ap.add_argument("--strict", action="store_true", help="código de salida 1 si hay FALTA, EXTRA o DESBORDE")
    a = ap.parse_args(argv)

    wh = (a.warehouse or get_settings().warehouse).resolve()
    spec = parse_spec(a.spec.read_text(encoding="utf-8"))
    con = duckdb.connect()
    print(f"Modelo: {a.spec}  ({sum(len(v) for v in spec.values())} columnas en {len(spec)} tablas)\nWarehouse: {wh}\n")

    bad = {"FALTA": 0, "EXTRA": 0, "DESBORDE": 0, "ORDEN": 0, "SIN_TABLA": 0}
    ok = 0
    deviations: dict[str, set[str]] = {}
    for table, cols in spec.items():
        src = table_source(wh, table)
        print(f"## {table}")
        if src is None:
            print("  SIN_TABLA  no existe en el warehouse\n")
            bad["SIN_TABLA"] += 1
            continue
        proj = {r[0]: r[1] for r in con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()}
        for c in cols:
            if c["col"] not in proj:
                print(f"  FALTA     {c['col']:22s} {c['type']}")
                bad["FALTA"] += 1
                continue
            ok += 1
            actual, st = proj[c["col"]], "OK"
            m = re.match(r"VARCHAR\((\d+)\)", c["type"].upper())
            if m:
                mx = con.execute(f"SELECT coalesce(max(length(cast({c['col']} as varchar))), 0) FROM {src}").fetchone()[0]
                if mx > int(m.group(1)):
                    st = "DESBORDE"
                    bad["DESBORDE"] += 1
                    print(f"  DESBORDE  {c['col']:22s} declarado {c['type']} pero el dato real llega a {mx} caracteres")
                    continue
            fam = type_family(c["type"])
            a_up = actual.upper()
            same = (fam == "VARCHAR" and a_up == "VARCHAR") or (fam == "INT" and a_up in ("INTEGER", "INT")) or (fam == "FLOAT" and a_up in ("FLOAT", "DOUBLE"))
            if not same:
                deviations.setdefault(f"{c['type']} -> {actual}", set()).add(f"{table}.{c['col']}")
            print(f"  OK        {c['col']:22s} {c['type']:14s} [{actual}]")
        extras = [c for c in proj if c not in {x["col"] for x in cols}]
        for c in extras:
            print(f"  EXTRA     {c}   (no está en el modelo)")
            bad["EXTRA"] += 1
        order_ok = [c for c in proj if c in {x['col'] for x in cols}] == [x["col"] for x in cols if x["col"] in proj]
        if not order_ok:
            print("  ORDEN     las columnas no están en el mismo orden que el modelo")
            bad["ORDEN"] += 1
        print()

    print("## Fuera del modelo (apoyo del dashboard / rendimiento)")
    for d in ALL_DIMS:
        if d.prefix == "ext":
            print(f"  {d.table}: {', '.join(d.published)}")
    print("  cube_*: agregados para el dashboard (se pueden borrar y regenerar)\n")

    print("## Apartados deliberados de los TIPOS del modelo (decisión de ingeniería, no faltas)")
    for k, v in sorted(deviations.items()):
        print(f"  {k:28s} {len(v)} columnas: {', '.join(sorted(v))[:150]}")
    print(f"\nRESUMEN: {ok} columnas del modelo presentes · FALTA {bad['FALTA']} · EXTRA {bad['EXTRA']} · DESBORDE {bad['DESBORDE']} · "
          f"ORDEN {bad['ORDEN']} · tablas sin crear {bad['SIN_TABLA']}")
    return 1 if (a.strict and any(bad.values())) else 0


if __name__ == "__main__":
    sys.exit(main())
