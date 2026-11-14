"""Carga el warehouse (parquet que produce `etl.run`) al datamart en SQL Server.

    python -m etl.load_mssql                          # dimensiones + todos los años de ambos hechos
    python -m etl.load_mssql --years 2026             # solo un año (las dims siempre se actualizan)
    python -m etl.load_mssql --dataset gasto --years 2025 2026
    python -m etl.load_mssql --delete --dataset gasto --years 2023   # BORRA ese año en SQL Server
    python -m etl.load_mssql --check                  # solo compara SQL Server contra los parquet
    python -m etl.load_mssql --full                   # vacía todo el datamart y lo recarga

Cómo funciona:
  * Claves. El ETL identifica cada miembro de dimensión con un hash de 64 bits (estable entre años).
    El modelo pide claves INT, así que la primera vez que aparece un hash se le asigna un INT en
    `etl.key_<dim>` y desde ahí siempre es el mismo: recargar un año no cambia ninguna clave.
  * Dimensiones: se suben a staging y se hace MERGE (Tipo 1: gana el nombre más reciente).
    Nunca se borran miembros, porque otros años pueden usarlos.
  * Hechos: un año por vez y en UNA transacción: se vacía la partición de ese año y se inserta el
    año completo. Si algo falla, el año queda como estaba.
  * Al final cuadra filas y sumas de cada monto, año por año, contra los parquet (OK / FAIL).

Se conecta con el usuario ADMIN de .env (MSSQL_ADMIN_USER), nunca con `sa`.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import duckdb
import pymssql

from config import get_settings

BATCH = 100_000

# Hash UBIGINT del ETL -> BIGINT con signo (SQL Server no tiene enteros sin signo). Es biyectivo.
NK = "CAST(CAST({c} AS HUGEINT) - 9223372036854775808 AS BIGINT)"


@dataclass(frozen=True)
class Dim:
    name: str          # sufijo de etl.key_<name>
    table: str         # dw.<table> y nombre del parquet
    sk: str
    cols: tuple        # (columna, largo VARCHAR) ; largo None = coordenada numérica


DIMS = (
    Dim("entidad", "dim_entidad", "sk_entidad", (("pliego", 150), ("ejecutora", 150))),
    Dim("ubigeo", "dim_ubigeo", "sk_ubigeo", (("codigo_ubigeo", 6), ("departamento", 50), ("provincia", 50),
                                              ("distrito", 50), ("latitud", None), ("longitud", None))),
    Dim("financiamiento", "dim_financiamiento", "sk_financiamiento",
        (("fuente", 100), ("rubro", 100), ("tipo_recurso", 150))),
    Dim("clasif_ingreso", "dim_clasif_ingreso", "sk_clasif_ingreso",
        (("generica", 100), ("subgenerica", 100), ("subgenerica_det", 150), ("especifica", 150), ("especifica_det", 200))),
    Dim("clasif_gasto", "dim_clasif_gasto", "sk_clasif_gasto",
        (("generica", 100), ("subgenerica", 100), ("subgenerica_det", 150), ("especifica", 150), ("especifica_det", 200))),
    Dim("funcional", "dim_funcional", "sk_funcional",
        (("funcion", 100), ("division_funcional", 150), ("grupo_funcional", 150),
         ("proyecto", 500), ("inversion", 250), ("meta", 250))),
)


@dataclass(frozen=True)
class Fact:
    dataset: str
    table: str
    keys: tuple        # (columna en la fact, dimensión a la que apunta)
    measures: tuple


FACTS = {
    "ingreso": Fact("ingreso", "fact_ingreso",
                    (("sk_entidad", "entidad"), ("sk_ubigeo", "ubigeo"), ("sk_financiamiento", "financiamiento"),
                     ("sk_clasif_ingreso", "clasif_ingreso")),
                    ("monto_pia", "monto_pim", "monto_recaudado")),
    "gasto": Fact("gasto", "fact_gasto",
                  (("sk_entidad", "entidad"), ("sk_ubigeo_ejec", "ubigeo"), ("sk_financiamiento", "financiamiento"),
                   ("sk_clasif_gasto", "clasif_gasto"), ("sk_funcional", "funcional")),
                  ("monto_pia", "monto_pim", "monto_devengado", "monto_girado")),
}


class LoadError(RuntimeError):
    pass


# --------------------------------------------------------------------------------------------
# Conexiones y utilidades
# --------------------------------------------------------------------------------------------
def connect_mssql() -> pymssql.Connection:
    env = os.environ.get
    missing = [v for v in ("MSSQL_ADMIN_USER", "MSSQL_ADMIN_PASSWORD") if not env(v)]
    if missing:
        raise LoadError(f"faltan variables en .env: {', '.join(missing)}")
    host, port, db = env("MSSQL_HOST", "localhost"), int(env("MSSQL_PORT", "1433")), env("MSSQL_DB", "MEF_DW")
    try:
        return pymssql.connect(server=host, port=port, user=env("MSSQL_ADMIN_USER"),
                               password=env("MSSQL_ADMIN_PASSWORD"), database=db,
                               autocommit=False, login_timeout=15)
    except pymssql.Error as e:  # pragma: no cover - depende de la red
        raise LoadError(f"no pude conectar a {host}:{port}/{db} como {env('MSSQL_ADMIN_USER')}: {e}") from e


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')}  {msg}", flush=True)


def parquet_glob(wh: Path, table: str) -> str:
    single = wh / f"{table}.parquet"
    if single.exists():
        return single.as_posix()
    part = wh / table
    if part.is_dir():
        return f"{part.as_posix()}/*/*.parquet"
    raise LoadError(f"no encuentro {table} en {wh}: corre primero `python -m etl.run`")


def fact_years(wh: Path, fact: Fact) -> list[int]:
    d = wh / fact.table
    return sorted(int(p.name.split("=")[1]) for p in d.glob("anio=*") if any(p.glob("*.parquet"))) if d.is_dir() else []


def stream(con: duckdb.DuckDBPyConnection, sql: str):
    """Filas de DuckDB por lotes, sin cargar todo en memoria."""
    cur = con.execute(sql)
    while rows := cur.fetchmany(BATCH):
        yield from rows


def recreate_stage(ms, name: str, columns_sql: str) -> None:
    cur = ms.cursor()
    cur.execute(f"DROP TABLE IF EXISTS etl.{name}; CREATE TABLE etl.{name} ({columns_sql});")
    ms.commit()


def drop_stage(ms, name: str) -> None:
    ms.cursor().execute(f"DROP TABLE IF EXISTS etl.{name};")
    ms.commit()


def bulk(ms, table: str, rows) -> int:
    n = 0

    def counted():
        nonlocal n
        for r in rows:
            n += 1
            yield r

    ms.bulk_copy(f"etl.{table}", counted(), batch_size=BATCH, tablock=True)
    ms.commit()
    return n


def scalar(ms, sql: str, params=None):
    cur = ms.cursor()
    cur.execute(sql, params)
    return cur.fetchone()[0]


def write_log(ms, accion: str, dataset=None, anio=None, filas=None, detalle=None) -> None:
    ms.cursor().execute("INSERT etl.load_log (accion, dataset, anio, filas, detalle) VALUES (%s, %s, %s, %s, %s)",
                        (accion, dataset, anio, filas, detalle))


# --------------------------------------------------------------------------------------------
# Dimensiones
# --------------------------------------------------------------------------------------------
def check_lengths(dk: duckdb.DuckDBPyConnection, src: str, dim: Dim) -> None:
    txt = [(c, n) for c, n in dim.cols if n]
    got = dk.execute("SELECT " + ", ".join(f"max(length({c}))" for c, _ in txt) + f" FROM read_parquet('{src}')").fetchone()
    over = [f"{c} ({g} > {n})" for (c, n), g in zip(txt, got) if g and g > n]
    if over:
        raise LoadError(f"{dim.table}: textos más largos que el VARCHAR del modelo: {', '.join(over)}")


def load_dim(ms, dk, wh: Path, dim: Dim) -> tuple[int, int]:
    src = parquet_glob(wh, dim.table)
    check_lengths(dk, src, dim)
    dups = dk.execute(f"SELECT count(*) - count(DISTINCT {dim.sk}) FROM read_parquet('{src}')").fetchone()[0]
    if dups:
        raise LoadError(f"{dim.table}: {dups} claves repetidas en el parquet")

    cols = [c for c, _ in dim.cols]
    stage_cols = ", ".join(f"{c} {'FLOAT' if n is None else 'NVARCHAR(1000)'} NULL" for c, n in dim.cols)
    stg = f"stg_{dim.table}"
    recreate_stage(ms, stg, f"nk BIGINT NOT NULL, {stage_cols}")
    n = bulk(ms, stg, stream(dk, f"SELECT {NK.format(c=dim.sk)}, {', '.join(cols)} FROM read_parquet('{src}')"))

    def cast(c, length):
        return f"CAST(s.{c} AS DECIMAL(10,7))" if length is None else f"CAST(s.{c} AS VARCHAR({length}))"

    cur = ms.cursor()
    cur.execute(f"""
        INSERT etl.key_{dim.name} (nk)
        SELECT s.nk FROM etl.{stg} s WHERE NOT EXISTS (SELECT 1 FROM etl.key_{dim.name} k WHERE k.nk = s.nk);""")
    nuevos = cur.rowcount
    cur.execute(f"""
        MERGE dw.{dim.table} AS d
        USING (SELECT k.sk, {', '.join(f'{cast(c, n)} AS {c}' for c, n in dim.cols)}
               FROM etl.{stg} s JOIN etl.key_{dim.name} k ON k.nk = s.nk) AS src
           ON d.{dim.sk} = src.sk
        WHEN MATCHED AND EXISTS (SELECT {', '.join(f'd.{c}' for c in cols)}
                                 EXCEPT SELECT {', '.join(f'src.{c}' for c in cols)})
            THEN UPDATE SET {', '.join(f'{c} = src.{c}' for c in cols)}
        WHEN NOT MATCHED BY TARGET
            THEN INSERT ({dim.sk}, {', '.join(cols)}) VALUES (src.sk, {', '.join(f'src.{c}' for c in cols)});""")
    ms.commit()
    drop_stage(ms, stg)
    return n, nuevos


def load_dim_tiempo(ms, dk, wh: Path) -> int:
    src = parquet_glob(wh, "dim_tiempo")
    recreate_stage(ms, "stg_dim_tiempo", "sk_tiempo INT NOT NULL, anio INT NOT NULL, mes INT NOT NULL")
    n = bulk(ms, "stg_dim_tiempo", stream(dk, f"SELECT sk_tiempo, anio, mes FROM read_parquet('{src}')"))
    ms.cursor().execute("""
        MERGE dw.dim_tiempo AS d USING etl.stg_dim_tiempo AS s ON d.sk_tiempo = s.sk_tiempo
        WHEN MATCHED AND (d.anio <> s.anio OR d.mes <> s.mes) THEN UPDATE SET anio = s.anio, mes = s.mes
        WHEN NOT MATCHED BY TARGET THEN INSERT (sk_tiempo, anio, mes) VALUES (s.sk_tiempo, s.anio, s.mes);""")
    ms.commit()
    drop_stage(ms, "stg_dim_tiempo")
    return n


def load_dims(ms, dk, wh: Path) -> None:
    t0 = time.time()
    log(f"dim_tiempo: {load_dim_tiempo(ms, dk, wh):,} filas")
    for dim in DIMS:
        n, nuevos = load_dim(ms, dk, wh, dim)
        log(f"{dim.table}: {n:,} filas ({nuevos:,} claves nuevas)")
    write_log(ms, "dims", detalle=f"{time.time() - t0:.1f} s")
    ms.commit()


# --------------------------------------------------------------------------------------------
# Hechos
# --------------------------------------------------------------------------------------------
def load_fact_year(ms, dk, wh: Path, fact: Fact, year: int) -> int:
    t0 = time.time()
    src = f"{(wh / fact.table / f'anio={year}').as_posix()}/*.parquet"
    stg = f"stg_{fact.table}"
    recreate_stage(ms, stg, "sk_tiempo INT NOT NULL, "
                   + ", ".join(f"nk_{dname} BIGINT NOT NULL" for _, dname in fact.keys) + ", "
                   + ", ".join(f"{m} DECIMAL(18,2) NOT NULL" for m in fact.measures))
    sel = ", ".join(["sk_tiempo"] + [NK.format(c=col) for col, _ in fact.keys] + list(fact.measures))
    n = bulk(ms, stg, stream(dk, f"SELECT {sel} FROM read_parquet('{src}')"))
    t_bulk = time.time() - t0

    cur = ms.cursor()
    fuera = scalar(ms, f"SELECT COUNT_BIG(*) FROM etl.{stg} WHERE sk_tiempo < %s OR sk_tiempo > %s",
                   (year * 100, year * 100 + 12))
    if fuera:
        raise LoadError(f"{fact.table} {year}: {fuera} filas con sk_tiempo fuera de ese año")
    for _, dname in fact.keys:
        huerfanos = scalar(ms, f"""SELECT COUNT_BIG(*) FROM etl.{stg} s
                                   WHERE NOT EXISTS (SELECT 1 FROM etl.key_{dname} k WHERE k.nk = s.nk_{dname})""")
        if huerfanos:
            raise LoadError(f"{fact.table} {year}: {huerfanos} filas apuntan a un miembro de dim {dname} "
                            "que no existe: carga primero las dimensiones (sin --only facts)")

    joins = "\n".join(f"JOIN etl.key_{d} k{i} ON k{i}.nk = s.nk_{d}" for i, (_, d) in enumerate(fact.keys))
    try:
        cur.execute("EXEC etl.usp_borrar_anio %s, %s, 0", (fact.dataset, year))
        cur.execute(f"""
            INSERT INTO dw.{fact.table} WITH (TABLOCK)
                   (sk_tiempo, {', '.join(c for c, _ in fact.keys)}, {', '.join(fact.measures)})
            SELECT s.sk_tiempo, {', '.join(f'k{i}.sk' for i in range(len(fact.keys)))},
                   {', '.join(f's.{m}' for m in fact.measures)}
            FROM etl.{stg} s
            {joins}
            OPTION (MAXDOP 1);""")  # en paralelo, el INSERT al columnstore recién truncado se bloquea a sí mismo (error 1205)
        if cur.rowcount != n:
            raise LoadError(f"{fact.table} {year}: se subieron {n} filas pero se insertaron {cur.rowcount}")
        write_log(ms, "carga", fact.dataset, year, n, f"{time.time() - t0:.1f} s")
        ms.commit()
    except Exception:
        ms.rollback()
        raise
    finally:
        drop_stage(ms, stg)
    dt = time.time() - t0
    log(f"{fact.table} {year}: {n:,} filas en {dt:.1f} s (subida {n / max(t_bulk, 1e-9):,.0f} filas/s)")
    return n


def compress(ms, fact: Fact) -> None:
    """Comprime las filas que quedaron en el 'delta store' del columnstore."""
    ms.autocommit(True)
    try:
        ms.cursor().execute(f"ALTER INDEX cci_{fact.table} ON dw.{fact.table} REORGANIZE "
                            "WITH (COMPRESS_ALL_ROW_GROUPS = ON);")
    finally:
        ms.autocommit(False)


def delete_years(ms, fact: Fact, years: list[int]) -> None:
    for y in years:
        n = scalar(ms, f"SELECT COUNT_BIG(*) FROM dw.{fact.table} WHERE sk_tiempo >= %s AND sk_tiempo < %s",
                   (y * 100, (y + 1) * 100))
        ms.cursor().execute("EXEC etl.usp_borrar_anio %s, %s", (fact.dataset, y))
        ms.commit()
        log(f"{fact.table} {y}: borradas {n:,} filas")


def wipe_all(ms) -> None:
    cur = ms.cursor()
    for f in FACTS.values():
        cur.execute(f"TRUNCATE TABLE dw.{f.table};")
    for d in DIMS:
        cur.execute(f"DELETE FROM dw.{d.table}; TRUNCATE TABLE etl.key_{d.name};")
    cur.execute("DELETE FROM dw.dim_tiempo;")
    write_log(ms, "borrado", detalle="--full: datamart vaciado")
    ms.commit()
    log("datamart vaciado (hechos, dimensiones y mapas de claves)")


# --------------------------------------------------------------------------------------------
# Verificación: SQL Server == parquet
# --------------------------------------------------------------------------------------------
def verify(ms, dk, wh: Path, fact: Fact, years: list[int]) -> bool:
    ok_all = True
    aggs = ", ".join(["COUNT(*)"] + [f"SUM({m})" for m in fact.measures])
    for y in years:
        pq = dk.execute(f"SELECT {aggs} FROM read_parquet('{(wh / fact.table / f'anio={y}').as_posix()}/*.parquet')"
                        ).fetchone()
        cur = ms.cursor()
        cur.execute(f"SELECT COUNT_BIG(*), {', '.join(f'SUM({m})' for m in fact.measures)} FROM dw.{fact.table} "
                    "WHERE sk_tiempo >= %s AND sk_tiempo < %s", (y * 100, (y + 1) * 100))
        sq = cur.fetchone()
        norm = lambda v: 0 if v is None else v  # noqa: E731
        same = all(norm(a) == norm(b) for a, b in zip(pq, sq))
        ok_all &= same
        detalle = f"{sq[0]:,} filas" + "".join(f" · {m}={norm(v):,.2f}" for m, v in zip(fact.measures, sq[1:]))
        if not same:
            detalle += f"   ≠ parquet: {pq[0]:,} filas" + "".join(f" · {norm(v):,.2f}" for v in pq[1:])
        print(f"[{'OK  ' if same else 'FAIL'}] {fact.table} {y}: {detalle}")
    return ok_all


# --------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------
def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Carga el warehouse parquet al datamart en SQL Server.")
    p.add_argument("--dataset", choices=["gasto", "ingreso", "all"], default="all")
    p.add_argument("--years", type=int, nargs="+", help="años a cargar/borrar (por defecto: todos los del warehouse)")
    p.add_argument("--only", choices=["dims", "facts"], help="solo dimensiones o solo hechos")
    p.add_argument("--delete", action="store_true", help="BORRA los --years indicados del datamart (no carga)")
    p.add_argument("--full", action="store_true", help="vacía todo el datamart (incluidas las claves) y recarga")
    p.add_argument("--check", action="store_true", help="solo compara SQL Server contra los parquet")
    a = p.parse_args(argv)

    wh = get_settings().warehouse
    facts = list(FACTS.values()) if a.dataset == "all" else [FACTS[a.dataset]]
    try:
        ms = connect_mssql()
        dk = duckdb.connect()

        if a.delete:
            if not a.years:
                raise LoadError("--delete necesita --years (para no borrar todo por accidente)")
            for f in facts:
                delete_years(ms, f, a.years)
            return 0

        if not a.check:
            if a.full:
                wipe_all(ms)
            if a.only != "facts":
                load_dims(ms, dk, wh)
            if a.only != "dims":
                for f in facts:
                    years = a.years or fact_years(wh, f)
                    missing = [y for y in years if y not in fact_years(wh, f)]
                    if missing:
                        raise LoadError(f"{f.table}: no hay parquet de {missing} en {wh}: corre `python -m etl.run`")
                    for y in years:
                        load_fact_year(ms, dk, wh, f, y)
                    if years:
                        compress(ms, f)

        if a.only == "dims":
            return 0
        ok = all([verify(ms, dk, wh, f, a.years or fact_years(wh, f)) for f in facts])
        print("Cuadre SQL Server vs parquet: " + ("todo OK" if ok else "HAY DIFERENCIAS"))
        return 0 if ok else 1
    except (LoadError, pymssql.Error) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
