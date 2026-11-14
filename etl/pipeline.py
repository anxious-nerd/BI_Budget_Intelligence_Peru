"""ETL: parquet crudo del MEF  ->  datamart en parquet (dims + facts + cubos).

Flujo de una corrida (idempotente; se puede repetir sin miedo):

  1. dims   : por cada año pedido, una pasada que lee SOLO columnas de código/nombre y guarda los
              miembros distintos en  _stage/<tabla>/anio=YYYY/<dataset>.parquet.  Luego se unen los años
              -> dim_*.parquet (Tipo 1: gana el nombre del año más reciente). DIM_FUNCIONAL lleva el año
              en su clave y se guarda particionada por año. DIM_UBIGEO se enriquece con latitud/longitud.
  2. facts  : por cada año, una pasada que lee SOLO códigos + montos. Si los montos vienen acumulados se
              convierten a flujo AL GRANO NATIVO (el más fino que publica el MEF) y recién después se
              agregan al grano del modelo. Escribe fact_<dataset>/anio=YYYY/ (reemplaza solo esa partición).
  3. cubes  : por cada año, agrega la fact a los cubos pequeños que usa el dashboard.
  4. views  : mef.duckdb con vistas sobre todo (para SQL ad-hoc en DBeaver / CLI).
  5. manifest.json : qué se construyó y cuándo (el dashboard lo vigila para recargarse).
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import duckdb

from config import Settings
from etl import db
from etl.model import (
    ALL_DIMS, CUBES, DATASETS, DIMS_BY_TABLE, EXT_DIMS, MODEL_DIMS, Cube, Dataset, Dim,
    code, money, text,
)  # fmt: skip

log = logging.getLogger("etl")

MANIFEST = "manifest.json"


class PreflightError(RuntimeError):
    pass


def re_search(pattern: str, value: str) -> bool:
    """¿`value` cumple la expresión regular? (misma sintaxis que usa DuckDB/RE2 para (?i))."""
    return re.search(pattern, value) is not None


# --------------------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------------------
class Timer:
    def __init__(self, label: str):
        self.label = label

    def __enter__(self):
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        if exc[0] is None:
            log.info("%-48s %6.1f s", self.label, time.perf_counter() - self.t0)


def _raw(settings: Settings, ds: Dataset) -> str:
    return settings.raw_gasto if ds.name == "gasto" else settings.raw_ingreso


class Source:
    """Origen de un dataset ya resuelto (SQL de FROM + columnas/tipos)."""

    def __init__(self, con, settings: Settings, ds: Dataset):
        self.ds = ds
        self.sql = db.source_sql(_raw(settings, ds))
        self.cols = db.describe(con, self.sql)
        self.exclude_regex = settings.exclude_files_regex

    def preflight(self) -> None:
        missing = [c for c in self.ds.required_columns if c not in self.cols]
        if missing:
            raise PreflightError(
                f"[{self.ds.name}] faltan columnas en el origen: {missing}.\n"
                f"  Columnas encontradas: {sorted(self.cols)}\n"
                "  Si tus parquet usan otros nombres, créales una vista con los nombres del "
                "diccionario del MEF (ver README, paso 2)."
            )

    @property
    def native(self) -> list[str]:
        """Columnas del grano nativo que existen en este origen (las que falten simplemente no se usan)."""
        return [c for c in self.ds.native_cols if c in self.cols]

    def where(self, year: int | None, valid_month: bool = True) -> str:
        """Filtro de filas a cargar. `valid_month=False` sirve para AUDITAR lo que se descarta."""
        ds, parts = self.ds, []
        if year is not None:
            ytype = self.cols[ds.year_col]
            # Comparación directa contra un literal del tipo de la columna: DuckDB la empuja al scan
            # del parquet (con CAST() no lo hace y leería todos los años).
            lit = f"'{year}'" if ytype == "VARCHAR" else str(year)
            parts.append(f"{ds.year_col} = {lit}")
        if valid_month:  # 0 = "apertura": en el origen puede traer PIA/PIM sin ejecución (se conserva)
            parts.append(f"try_cast({ds.month_col} as integer) between 0 and 12")
        if "filename" in self.cols and self.exclude_regex:
            parts.append(f"NOT regexp_matches(filename, {db.sql_str(self.exclude_regex)})")
        return " AND ".join(parts)

    def years(self, con) -> list[int]:
        w = self.where(None)
        rows = con.execute(f"SELECT DISTINCT try_cast({self.ds.year_col} as integer) FROM {self.sql} WHERE {w}").fetchall()
        return sorted(r[0] for r in rows if r[0] is not None)


def _swap_dir(new: Path, dest: Path) -> None:
    """Reemplaza `dest` por `new` (renombrados atómicos en el mismo disco)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    trash = new.parent / f"{new.name}.old"
    shutil.rmtree(trash, ignore_errors=True)
    if dest.exists():
        os.rename(dest, trash)
    os.rename(new, dest)
    shutil.rmtree(trash, ignore_errors=True)


def _write_partition(con, select_sql: str, root: Path, year: int) -> int:
    """Escribe `select_sql` (que debe traer la columna `anio`) como root/anio=YYYY/.

    Se escribe primero en una carpeta temporal y luego se cambia la partición, de modo que un lector
    concurrente (el dashboard) nunca ve una partición a medias.
    """
    tmp = root.parent / ".tmp" / f"{root.name}_{year}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    con.execute(
        f"COPY ({select_sql}) TO '{tmp.as_posix()}' "
        "(FORMAT PARQUET, PARTITION_BY (anio), COMPRESSION zstd, OVERWRITE_OR_IGNORE)"
    )
    new, dest = tmp / f"anio={year}", root / f"anio={year}"
    if not new.exists():  # el año no produjo filas
        shutil.rmtree(dest, ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)
        return 0
    _swap_dir(new, dest)
    shutil.rmtree(tmp, ignore_errors=True)
    return con.execute(f"SELECT count(*) FROM read_parquet('{dest.as_posix()}/*.parquet')").fetchone()[0]


def _write_file(con, select_sql: str, dest: Path) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp.parquet")
    con.execute(f"COPY ({select_sql}) TO '{tmp.as_posix()}' (FORMAT PARQUET, COMPRESSION zstd)")
    os.replace(tmp, dest)
    return con.execute(f"SELECT count(*) FROM read_parquet('{dest.as_posix()}')").fetchone()[0]


def dim_source(wh: Path, dim: Dim) -> str:
    """Expresión FROM que lee una dimensión PUBLICADA (con o sin partición por año)."""
    if dim.by_year:
        return f"(SELECT * EXCLUDE (anio) FROM read_parquet('{(wh / dim.table).as_posix()}/*/*.parquet', hive_partitioning=true))"
    return f"read_parquet('{(wh / dim.table).as_posix()}.parquet')"


def fact_source(wh: Path, ds: Dataset) -> str:
    return f"read_parquet('{(wh / ds.fact).as_posix()}/*/*.parquet', hive_partitioning=true)"


# --------------------------------------------------------------------------------------
# Tabla de ubigeos (latitud / longitud)
# --------------------------------------------------------------------------------------
def geo_sql(settings: Settings) -> str | None:
    """SELECT (codigo, latitud, longitud) desde la tabla de ubigeos; None si el archivo no existe.

    La tabla viene con ';' como separador y COMA decimal (-6,2294), por eso el replace.
    """
    p = settings.ubigeo_table
    if not p.exists():
        return None
    col = f"ubigeo_{settings.ubigeo_code}"
    return (
        f"SELECT trim({col}) AS codigo, "
        "max(try_cast(replace(trim(latitud), ',', '.') AS DOUBLE)) AS latitud, "
        "max(try_cast(replace(trim(longitud), ',', '.') AS DOUBLE)) AS longitud "
        f"FROM read_csv('{p.as_posix()}', delim=';', header=true, all_varchar=true, encoding='utf-8') GROUP BY 1"
    )


def check_ubigeo_table(con, settings: Settings) -> None:
    p = settings.ubigeo_table
    if not p.exists():
        log.warning("No hay tabla de ubigeos en %s: dim_ubigeo quedará SIN latitud/longitud (sin mapas).", p)
        return
    cols = {r[0].lower() for r in con.execute(
        f"DESCRIBE SELECT * FROM read_csv('{p.as_posix()}', delim=';', header=true, all_varchar=true, encoding='utf-8')").fetchall()}  # fmt: skip
    need = {f"ubigeo_{settings.ubigeo_code}", "latitud", "longitud"}
    if not need <= cols:
        raise PreflightError(
            f"La tabla de ubigeos {p} no tiene las columnas {sorted(need - cols)} (separador esperado ';'). Columnas: {sorted(cols)}"
        )


# --------------------------------------------------------------------------------------
# 1) Dimensiones
# --------------------------------------------------------------------------------------
def dims_of(ds: Dataset) -> list[Dim]:
    """Dimensiones del modelo de este dataset + las tablas de apoyo ext_* que comparten su clave."""
    out = list(ds.dims)
    out += [e for e in EXT_DIMS if any(e.name == d.name for d in ds.dims)]
    return out


def dim_stage_sql(src: Source, dim: Dim, year: int) -> str:
    """Miembros distintos de una dimensión en un año (lee solo sus columnas).

    Primero DISTINCT sobre los valores CRUDOS (barato: pocas combinaciones) y recién después se
    normaliza/hashea, en vez de normalizar los millones de filas.
    """
    ds, t = src.ds, src.cols
    distinct = f"SELECT DISTINCT {', '.join(dim.raw_refs(ds))} FROM {src.sql} WHERE {src.where(year)}"
    keys = ", ".join(f"{dim.key_part(k, ds, t)} AS k_{k}" for k in dim.keys)
    attrs = ", ".join(f"{text(raw)} AS {pub}" for pub, raw in dim.attrs)
    inner = f"SELECT {dim.sk_expr(ds, t)} AS {dim.sk}, {keys}, {attrs} FROM ({distinct})"
    # max(): si dentro de un año un mismo código aparece con dos descripciones, el resultado es
    # determinístico (y no un valor al azar).
    cols = [*(f"k_{k}" for k in dim.keys), *dim.attr_cols]
    agg = ", ".join(f"max({c}) AS {c}" for c in cols)
    return f"SELECT {dim.sk}, {agg} FROM ({inner}) GROUP BY {dim.sk}"


def build_dim_stage(con, settings: Settings, src: Source, year: int) -> None:
    wh = settings.warehouse
    for dim in dims_of(src.ds):
        dest = wh / "_stage" / dim.table / f"anio={year}" / f"{src.ds.name}.parquet"
        _write_file(con, dim_stage_sql(src, dim, year), dest)
        if dim.by_year:  # la clave lleva el año: el stage de ese año ES la dimensión publicada de ese año
            cols = ", ".join([dim.sk, *dim.attr_cols])
            _write_partition(con, f"SELECT {year} AS anio, {cols} FROM read_parquet('{dest.as_posix()}')", wh / dim.table, year)


def build_dims(con, settings: Settings) -> dict[str, int]:
    """Une los años del _stage -> dim_X.parquet (Tipo 1: gana el nombre del año más reciente)."""
    wh, counts = settings.warehouse, {}
    geo = geo_sql(settings)
    for dim in ALL_DIMS:
        if dim.by_year:
            continue
        stage = wh / "_stage" / dim.table
        if not stage.exists():
            continue
        cols = [*(f"k_{k}" for k in dim.keys), *dim.attr_cols]
        per_year = ", ".join(f"max({c}) AS {c}" for c in cols)
        latest = ", ".join(f"arg_max({c}, anio) AS {c}" for c in cols)
        base = (
            f"WITH s AS (SELECT {dim.sk}, anio, {per_year} "
            f"  FROM read_parquet('{stage.as_posix()}/*/*.parquet', hive_partitioning=true, union_by_name=true) "
            f"  GROUP BY {dim.sk}, anio), "
            f"d AS (SELECT {dim.sk}, {latest} FROM s GROUP BY {dim.sk}) "
        )
        sel = [f"d.{dim.sk}", *(f"d.k_{k} AS {k}" for k in dim.publish_keys), *(f"d.{a}" for a in dim.attr_cols)]
        if dim.extra_published:  # latitud / longitud
            if geo:
                sel += ["g.latitud", "g.longitud"]
                sql = base + f"SELECT {', '.join(sel)} FROM d LEFT JOIN ({geo}) g ON g.codigo = d.k_codigo_ubigeo ORDER BY d.{dim.sk}"
            else:
                sel += ["NULL::DOUBLE AS latitud", "NULL::DOUBLE AS longitud"]
                sql = base + f"SELECT {', '.join(sel)} FROM d ORDER BY d.{dim.sk}"
        else:
            sql = base + f"SELECT {', '.join(sel)} FROM d ORDER BY d.{dim.sk}"
        counts[dim.table] = _write_file(con, sql, wh / f"{dim.table}.parquet")
    return counts


def build_dim_tiempo(con, settings: Settings) -> int:
    wh = settings.warehouse
    parts = [f"SELECT sk_tiempo, anio FROM {fact_source(wh, ds)}" for ds in DATASETS.values() if (wh / ds.fact).exists()]
    if not parts:
        return 0
    sql = (
        f"SELECT sk_tiempo, CAST(anio AS INTEGER) AS anio, sk_tiempo % 100 AS mes FROM (SELECT DISTINCT sk_tiempo, anio FROM ({' UNION ALL '.join(parts)})) "
        "ORDER BY sk_tiempo"
    )
    return _write_file(con, sql, wh / "dim_tiempo.parquet")


# --------------------------------------------------------------------------------------
# 2) Hechos
# --------------------------------------------------------------------------------------
def fact_sql(src: Source, year: int, exec_mode: str) -> str:
    """Hecho al GRANO DEL MODELO: (mes, entidad, ubicación, financiamiento, clasificador[, meta])."""
    ds, t = src.ds, src.cols
    fk_sql = ", ".join(f"{d.sk_expr(ds, t)} AS {fk}" for fk, d in ds.fks)
    mon_sql = ", ".join(f"{money(m)} AS {m}" for m in ds.measures)
    ymd = f"try_cast({ds.year_col} as integer) AS anio, try_cast({ds.month_col} as integer) AS mes"
    fks = ", ".join(ds.fk_cols)
    stock = [f"monto_{m}" for m in ds.stock]
    sums = ", ".join(f"CAST(sum({m}) AS DECIMAL(18,2)) AS {m}" for m in ds.measures)

    if exec_mode == "flow":
        inner = f"SELECT {ymd}, {fk_sql}, {mon_sql} FROM {src.sql} WHERE {src.where(year)}"
        flows_sql = inner
    else:
        # Serie nativa = todas las columnas de código que publica el MEF. El LAG se hace AQUÍ (y no al
        # grano reducido del modelo): si una serie nativa deja de aparecer unos meses, el LAG contra su
        # propio mes anterior reparte bien lo acumulado; sumando antes se crearían saltos artificiales.
        inner = f"SELECT {ymd}, hash({', '.join(src.native)}) AS _nk, {fk_sql}, {mon_sql} FROM {src.sql} WHERE {src.where(year)}"
        g = f"SELECT anio, mes, _nk, {fks}, {sums} FROM ({inner}) GROUP BY anio, mes, _nk, {fks}"
        diff = ", ".join(f"CAST({m} - coalesce(lag({m}) OVER w, 0) AS DECIMAL(18,2)) AS {m}" for m in ds.flow_measures)
        flows_sql = (
            f"SELECT anio, mes, {fks}, {', '.join(stock)}, {diff} FROM ({g}) WINDOW w AS (PARTITION BY _nk ORDER BY mes)"
        )
    # `anio` va al final: solo sirve para particionar (queda como carpeta anio=YYYY, no como columna).
    return (
        f"SELECT anio * 100 + mes AS sk_tiempo, {fks}, {sums}, anio FROM ({flows_sql}) GROUP BY anio, mes, {fks}"
    )


def build_fact(con, settings: Settings, src: Source, year: int) -> int:
    root = settings.warehouse / src.ds.fact
    return _write_partition(con, fact_sql(src, year, settings.exec_mode), root, year)


# --------------------------------------------------------------------------------------
# 3) Cubos
# --------------------------------------------------------------------------------------
def cube_sql(settings: Settings, cube: Cube, year: int) -> str:
    wh, ds = settings.warehouse, DATASETS[cube.dataset]
    used = list(dict.fromkeys(t for t, _, _ in cube.by if t != "fact"))
    joins = []
    for t in used:
        dim = DIMS_BY_TABLE[t]
        path = f"{(wh / t).as_posix()}/anio={year}/*.parquet" if dim.by_year else f"{(wh / t).as_posix()}.parquet"
        joins.append(f"JOIN read_parquet('{path}') {t} ON f.{ds.fk_for(dim)} = {t}.{dim.sk}")
    cols = ", ".join(f"{'f' if t == 'fact' else t}.{c} AS {a}" for t, c, a in cube.by)
    sums = ", ".join(f"CAST(sum(f.{m}) AS DECIMAL(18,2)) AS {m}" for m in ds.measures)
    return (
        f"SELECT f.anio, f.sk_tiempo % 100 AS mes, {cols}, {sums} "
        f"FROM {fact_source(wh, ds)} f {' '.join(joins)} WHERE f.anio = {year} GROUP BY ALL"
    )


def build_cube(con, settings: Settings, cube: Cube, year: int) -> int:
    return _write_partition(con, cube_sql(settings, cube, year), settings.warehouse / cube.table, year)


# --------------------------------------------------------------------------------------
# 4) Vistas para consulta ad-hoc  +  5) manifest
# --------------------------------------------------------------------------------------
def write_views(settings: Settings) -> Path:
    wh = settings.warehouse
    path = wh / "mef.duckdb"
    con = duckdb.connect(str(path))
    try:
        for ds in DATASETS.values():
            if (wh / ds.fact).exists():
                con.execute(f"CREATE OR REPLACE VIEW {ds.fact} AS SELECT * FROM {fact_source(wh, ds)}")
        for c in CUBES:
            if (wh / c.table).exists():
                con.execute(
                    f"CREATE OR REPLACE VIEW {c.table} AS SELECT * FROM "
                    f"read_parquet('{(wh / c.table).as_posix()}/*/*.parquet', hive_partitioning=true)"
                )
        for d in ALL_DIMS:
            if (wh / d.table).exists() if d.by_year else (wh / f"{d.table}.parquet").exists():
                con.execute(f"CREATE OR REPLACE VIEW {d.table} AS SELECT * FROM {dim_source(wh, d)}")
        t = wh / "dim_tiempo.parquet"
        if t.exists():
            con.execute(f"CREATE OR REPLACE VIEW dim_tiempo AS SELECT * FROM read_parquet('{t.as_posix()}')")
    finally:
        con.close()
    return path


def read_manifest(settings: Settings) -> dict:
    p = settings.warehouse / MANIFEST
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"schema_version": 2, "years": {"gasto": {}, "ingreso": {}}, "cubes": {}}


def write_manifest(settings: Settings, manifest: dict) -> None:
    manifest["built_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    manifest["duckdb"] = duckdb.__version__
    manifest["exec_mode"] = settings.exec_mode
    manifest["budget_mode"] = {"gasto": settings.budget_mode_gasto, "ingreso": settings.budget_mode_ingreso}
    manifest["ubigeo_code"] = settings.ubigeo_code
    p = settings.warehouse / MANIFEST
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)  # el dashboard detecta el cambio de mtime y recarga


def ubigeo_coverage(con, settings: Settings) -> dict:
    f = settings.warehouse / "dim_ubigeo.parquet"
    if not f.exists():
        return {}
    n, ok = con.execute(f"SELECT count(*), count(latitud) FROM read_parquet('{f.as_posix()}')").fetchone()
    return {"members": n, "with_coordinates": ok}


# --------------------------------------------------------------------------------------
# Orquestación
# --------------------------------------------------------------------------------------
def run(
    settings: Settings,
    years: Iterable[int] | None = None,
    datasets: Iterable[str] = ("gasto", "ingreso"),
    steps: Iterable[str] = ("dims", "facts", "cubes", "views"),
    full: bool = False,
) -> dict:
    steps, wanted = set(steps), set(years) if years else None
    settings.warehouse.mkdir(parents=True, exist_ok=True)
    manifest = read_manifest(settings)

    prev_mode = manifest.get("exec_mode")
    if prev_mode and prev_mode != settings.exec_mode and not full:
        raise PreflightError(
            f"El warehouse se construyó con exec_mode='{prev_mode}' y ahora pides "
            f"'{settings.exec_mode}'. Mezclar modos corrompe las métricas: corre con --full."
        )
    if manifest.get("schema_version", 1) < 2 and not full:
        raise PreflightError(
            "Este warehouse se construyó con una versión anterior del modelo (otra estructura de tablas). "
            "Reconstrúyelo con --full."
        )
    if full:
        for ds in DATASETS.values():
            for p in [ds.fact, *(c.table for c in CUBES if c.dataset == ds.name)]:
                shutil.rmtree(settings.warehouse / p, ignore_errors=True)
        for d in ALL_DIMS:
            shutil.rmtree(settings.warehouse / d.table, ignore_errors=True)
            (settings.warehouse / f"{d.table}.parquet").unlink(missing_ok=True)
        shutil.rmtree(settings.warehouse / "_stage", ignore_errors=True)
        manifest = {"schema_version": 2, "years": {"gasto": {}, "ingreso": {}}, "cubes": {}}
    manifest["schema_version"] = 2

    con = db.connect(settings)
    check_ubigeo_table(con, settings)
    plan: dict[str, tuple[Source, list[int]]] = {}
    for name in datasets:
        src = Source(con, settings, DATASETS[name])
        src.preflight()
        avail = src.years(con)
        todo = [y for y in avail if wanted is None or y in wanted]
        missing = sorted((wanted or set()) - set(avail))
        if missing:
            log.warning("[%s] años pedidos que no existen en el origen: %s", name, missing)
        plan[name] = (src, todo)
        log.info("[%s] años disponibles: %s | a procesar: %s", name, avail, todo)

    if "dims" in steps:
        for name, (src, todo) in plan.items():
            for y in todo:
                with Timer(f"dims-stage {name} {y}"):
                    build_dim_stage(con, settings, src, y)
        with Timer("dims (unión de años + ubigeo)"):
            counts = build_dims(con, settings)
        log.info("dimensiones: %s", counts)
        cov = ubigeo_coverage(con, settings)
        if cov:
            manifest["ubigeo"] = cov
            if cov["with_coordinates"] < cov["members"]:
                log.warning("dim_ubigeo: %s de %s ubicaciones SIN coordenadas (código sin equivalente en la tabla de ubigeos)",
                            cov["members"] - cov["with_coordinates"], cov["members"])  # fmt: skip

    if "facts" in steps:
        for name, (src, todo) in plan.items():
            for y in todo:
                with Timer(f"fact {name} {y}"):
                    n = build_fact(con, settings, src, y)
                manifest["years"].setdefault(name, {})[str(y)] = {
                    "rows_fact": n, "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }  # fmt: skip
                log.info("  fact_%s anio=%s -> %s filas", name, y, f"{n:,}")
        with Timer("dim_tiempo"):
            build_dim_tiempo(con, settings)

    if "cubes" in steps:
        for cube in CUBES:
            if cube.dataset not in plan:
                continue
            for y in plan[cube.dataset][1]:
                with Timer(f"cubo {cube.name} {y}"):
                    n = build_cube(con, settings, cube, y)
                manifest["cubes"].setdefault(cube.name, {})[str(y)] = n

    if "views" in steps:
        write_views(settings)
    write_manifest(settings, manifest)
    con.close()
    return manifest
