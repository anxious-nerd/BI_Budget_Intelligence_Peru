"""Controles de calidad POST-ETL. Corre después de cada carga:

    python -m etl.validate            # rápido (segundos)
    python -m etl.validate --deep     # además verifica unicidad del grano de las facts (más lento)

Cada control termina en OK / WARN / FAIL. Código de salida 1 si hay algún FAIL.
  FAIL = el datamart está mal (no lo uses).
  WARN = revisar: puede ser un supuesto sobre tus datos que no se cumple.
"""
from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from decimal import Decimal

from config import Settings, get_settings
from etl import db, pipeline
from etl.model import ALL_DIMS, CUBES, DATASETS


@dataclass
class Check:
    status: str  # OK | WARN | FAIL
    name: str
    detail: str = ""


def run_checks(settings: Settings, deep: bool = False, with_raw: bool = True) -> list[Check]:
    wh, out = settings.warehouse, []
    con = db.connect(settings)
    fact_src = lambda ds: pipeline.fact_source(wh, ds)  # noqa: E731
    dim_src = lambda d: pipeline.dim_source(wh, d)  # noqa: E731

    def exists(d) -> bool:
        return (wh / d.table).exists() if d.by_year else (wh / f"{d.table}.parquet").exists()

    # 1) Claves sustitutas únicas en las dimensiones publicadas ---------------------------
    for d in ALL_DIMS:
        if not exists(d):
            continue
        n, nsk = con.execute(f"SELECT count(*), count(DISTINCT {d.sk}) FROM {dim_src(d)}").fetchone()
        if n != nsk:
            out.append(Check("FAIL", f"{d.table}: sk única", f"{n} filas pero {nsk} sk distintas (¿colisión de hash?)"))
        else:
            out.append(Check("OK", f"{d.table}: sk única", f"{n:,} miembros"))

    # 1b) Colisiones de hash: misma sk para claves naturales distintas (usa el _stage, que sí guarda las claves)
    for d in ALL_DIMS:
        stage = wh / "_stage" / d.table
        if not stage.exists():
            continue
        keys = ", ".join(f"k_{k}" for k in d.keys)
        nsk, nk = con.execute(
            f"SELECT count(DISTINCT {d.sk}), count(DISTINCT ({keys})) "
            f"FROM read_parquet('{stage.as_posix()}/*/*.parquet', hive_partitioning=true, union_by_name=true)"
        ).fetchone()
        if nsk != nk:
            out.append(Check("FAIL", f"{d.table}: hash sin colisiones", f"{nk} claves naturales -> {nsk} sk"))

    # 2) Integridad referencial fact -> dim (incluida dim_tiempo) ---------------------------
    for ds in DATASETS.values():
        if not (wh / ds.fact).exists():
            continue
        for fk, d in ds.fks:
            orphan = con.execute(f"SELECT count(*) FROM {fact_src(ds)} f ANTI JOIN {dim_src(d)} d ON f.{fk} = d.{d.sk}").fetchone()[0]
            out.append(Check("FAIL" if orphan else "OK", f"{ds.fact}.{fk} existe en {d.table}", f"{orphan:,} huérfanas" if orphan else ""))
        t = wh / "dim_tiempo.parquet"
        if t.exists():
            orphan = con.execute(
                f"SELECT count(*) FROM {fact_src(ds)} f ANTI JOIN read_parquet('{t.as_posix()}') d ON f.sk_tiempo = d.sk_tiempo"
            ).fetchone()[0]
            out.append(Check("FAIL" if orphan else "OK", f"{ds.fact}.sk_tiempo existe en dim_tiempo", f"{orphan:,} huérfanas" if orphan else ""))

    # 3) Unicidad del grano del modelo (opcional, caro) ---------------------------------------
    if deep:
        for ds in DATASETS.values():
            if not (wh / ds.fact).exists():
                continue
            cols = ", ".join(["anio", "sk_tiempo", *ds.fk_cols])
            n, nd = con.execute(f"SELECT count(*), count(DISTINCT ({cols})) FROM {fact_src(ds)}").fetchone()
            out.append(Check("FAIL" if n != nd else "OK", f"{ds.fact}: una fila por grano del modelo", f"{n - nd:,} duplicadas" if n != nd else f"{n:,} filas"))

    # 4) Cubos == hechos (deben coincidir al centavo) -------------------------------------------
    for cube in CUBES:
        ds = DATASETS[cube.dataset]
        if not (wh / cube.table).exists() or not (wh / ds.fact).exists():
            continue
        sums = ", ".join(f"sum({m})" for m in ds.measures)
        a = con.execute(f"SELECT {sums} FROM {fact_src(ds)}").fetchone()
        b = con.execute(f"SELECT {sums} FROM read_parquet('{(wh / cube.table).as_posix()}/*/*.parquet', hive_partitioning=true)").fetchone()
        bad = [m for m, x, y in zip(ds.measures, a, b) if (x or Decimal(0)) != (y or Decimal(0))]
        out.append(Check("FAIL" if bad else "OK", f"{cube.table} == {ds.fact}", f"difieren: {bad}" if bad else ""))

    # 5) Hechos vs. crudo ------------------------------------------------------------------------
    if with_raw:
        for ds in DATASETS.values():
            if not (wh / ds.fact).exists():
                continue
            src = pipeline.Source(con, settings, ds)
            years = sorted(r[0] for r in con.execute(f"SELECT DISTINCT anio FROM {fact_src(ds)}").fetchall())
            for year in years:
                last = con.execute(f"SELECT max(sk_tiempo % 100) FROM {fact_src(ds)} WHERE anio = {year}").fetchone()[0]
                # (a) Montos de ejecución (flujo): total del año en la fact vs. el crudo.
                for m in ds.flow_measures:
                    fact_total = con.execute(f"SELECT coalesce(sum({m}),0) FROM {fact_src(ds)} WHERE anio = {year}").fetchone()[0]
                    where = f"{src.where(year)} AND try_cast({ds.month_col} as integer) = {last}" if settings.exec_mode == "cumulative" else src.where(year)
                    raw_total = con.execute(f"SELECT coalesce(sum({pipeline.money(m)}),0) FROM {src.sql} WHERE {where}").fetchone()[0]
                    diff = Decimal(fact_total) - Decimal(raw_total)
                    label = f"{ds.name} {year} {m}: fact vs crudo"
                    if diff == 0:
                        out.append(Check("OK", label, f"{fact_total:,.2f}"))
                    else:
                        out.append(Check("WARN", label,
                            f"fact={fact_total:,.2f} crudo={raw_total:,.2f} (dif {diff:,.2f}). En modo cumulative esto ocurre si hay "
                            "series que NO aparecen en el último mes (datos dispersos): revisa con `python -m etl.profile`."))  # fmt: skip
                # (b) Saldos (PIA/PIM): nada se pierde ni se duplica al llevarlos al grano del modelo.
                for m in (f"monto_{x}" for x in ds.stock):
                    a = con.execute(f"SELECT coalesce(sum({m}),0) FROM {fact_src(ds)} WHERE anio = {year}").fetchone()[0]
                    b = con.execute(f"SELECT coalesce(sum({pipeline.money(m)}),0) FROM {src.sql} WHERE {src.where(year)}").fetchone()[0]
                    out.append(Check("OK" if Decimal(a) == Decimal(b) else "FAIL", f"{ds.name} {year} {m}: saldo fact == crudo", f"{a:,.2f}" if Decimal(a) == Decimal(b) else f"fact={a:,.2f} crudo={b:,.2f}"))
                # (c) Filas con mes inválido: se descartan, pero no en silencio.
                n_bad, pim_bad = con.execute(
                    f"SELECT count(*), coalesce(sum({pipeline.money('monto_pim')}), 0) FROM {src.sql} "
                    f"WHERE {src.where(year, valid_month=False)} AND NOT coalesce(try_cast({ds.month_col} as integer) between 0 and 12, false)"
                ).fetchone()
                if n_bad:
                    out.append(Check("WARN", f"{ds.name} {year}: filas con mes inválido (descartadas)", f"{n_bad:,} filas, PIM {pim_bad:,.2f}"))

    # 6) Ubicaciones sin coordenadas (mapas) -----------------------------------------------------
    f_ub = wh / "dim_ubigeo.parquet"
    if f_ub.exists():
        n, ok = con.execute(f"SELECT count(*), count(latitud) FROM read_parquet('{f_ub.as_posix()}')").fetchone()
        if ok == n:
            out.append(Check("OK", "dim_ubigeo: todas las ubicaciones tienen coordenadas", f"{n:,}"))
        else:
            share = ""
            g = DATASETS["gasto"]
            if (wh / g.fact).exists():
                tot, sin = con.execute(
                    f"SELECT sum(f.monto_devengado), sum(f.monto_devengado) FILTER (WHERE u.latitud IS NULL) "
                    f"FROM {fact_src(g)} f JOIN read_parquet('{f_ub.as_posix()}') u ON f.sk_ubigeo_ejec = u.sk_ubigeo"
                ).fetchone()
                share = f"; esas ubicaciones concentran {float(sin or 0) / float(tot):.1%} del devengado" if tot else ""
            out.append(Check("WARN", "dim_ubigeo: ubicaciones SIN coordenadas",
                f"{n - ok:,} de {n:,}{share}. Causas: distrito creado después de la tabla de ubigeos, o el MEF usa el otro código "
                f"(MEF_UBIGEO_CODE={settings.ubigeo_code}): `python -m etl.profile` compara inei vs reniec."))  # fmt: skip

    # 7) Calidad de las claves del modelo (usa el _stage) -------------------------------------------
    st = wh / "_stage" / "ext_entidad"
    if st.exists():
        conflicts = con.execute(
            f"SELECT count(*) FROM (SELECT sk_entidad, anio FROM read_parquet('{(wh / '_stage' / 'dim_entidad').as_posix()}/*/*.parquet', "
            "hive_partitioning=true, union_by_name=true) GROUP BY ALL HAVING count(DISTINCT (pliego, ejecutora)) > 1)"
        ).fetchone()[0]
        out.append(Check("WARN" if conflicts else "OK", "SEC_EJEC identifica a una sola entidad", f"{conflicts:,} SEC_EJEC con nombres distintos en un mismo año (gasto vs ingreso, o repetidos)" if conflicts else ""))
    sf = wh / "_stage" / "dim_funcional"
    if sf.exists():
        chg, tot = con.execute(
            f"SELECT count(*) FILTER (WHERE n > 1), count(*) FROM (SELECT k_sec_ejec, k_sec_func, "
            "count(DISTINCT (funcion, division_funcional, grupo_funcional, proyecto, inversion, meta)) AS n "
            f"FROM read_parquet('{sf.as_posix()}/*/*.parquet', hive_partitioning=true, union_by_name=true) GROUP BY ALL)"
        ).fetchone()
        out.append(Check("OK", "DIM_FUNCIONAL: metas que cambian de atributos entre años",
            f"{chg:,} de {tot:,} (SEC_EJEC, SEC_FUNC). " + ("El año en la clave evita atribuirlas mal." if chg else "Hoy ninguna cambia; la clave con año sigue siendo segura.")))  # fmt: skip

    # 8) Cuadre PIM ingreso vs gasto (diagnóstico). El PIM vigente depende del modo de lectura:
    #    snapshot = último mes con PIM;  sum = suma de todos los meses del año.
    g, i = DATASETS["gasto"], DATASETS["ingreso"]
    if (wh / g.fact).exists() and (wh / i.fact).exists():
        def pim_vigente(ds, anio):
            mode = settings.budget_mode_gasto if ds.name == "gasto" else settings.budget_mode_ingreso
            if mode == "sum":
                return con.execute(f"SELECT sum(monto_pim) FROM {fact_src(ds)} WHERE anio = {anio}").fetchone()[0]
            return con.execute(
                f"SELECT sum(monto_pim) FROM {fact_src(ds)} WHERE anio = {anio} AND sk_tiempo = "
                f"(SELECT max(sk_tiempo) FROM {fact_src(ds)} WHERE anio = {anio} AND monto_pim <> 0)"
            ).fetchone()[0]

        years = sorted({r[0] for r in con.execute(f"SELECT DISTINCT anio FROM {fact_src(g)}").fetchall()}
                       & {r[0] for r in con.execute(f"SELECT DISTINCT anio FROM {fact_src(i)}").fetchall()})
        for anio in years:
            pg, pi = pim_vigente(g, anio), pim_vigente(i, anio)
            if not pg:
                continue
            rel = abs(Decimal(pi or 0) - Decimal(pg)) / Decimal(pg)
            out.append(Check("OK" if rel <= Decimal("0.01") else "WARN", f"{anio}: cuadre PIM ingreso vs gasto",
                             f"ingreso={pi or 0:,.0f} gasto={pg:,.0f} (dif {rel:.1%}; modos: gasto={settings.budget_mode_gasto}, "
                             f"ingreso={settings.budget_mode_ingreso}). El presupuesto debe estar equilibrado; una diferencia grande "
                             "puede venir de datos de prueba, de un modo de PIM mal elegido o de partidas que el extracto no trae."))  # fmt: skip
    con.close()
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--deep", action="store_true")
    ap.add_argument("--no-raw", action="store_true", help="omite la conciliación contra el crudo")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    checks = run_checks(get_settings(), deep=a.deep, with_raw=not a.no_raw)
    for c in checks:
        print(f"[{c.status:4}] {c.name}" + (f"  —  {c.detail}" if c.detail else ""))
    fails = sum(c.status == "FAIL" for c in checks)
    warns = sum(c.status == "WARN" for c in checks)
    print(f"\n{len(checks)} controles: {len(checks) - fails - warns} OK, {warns} WARN, {fails} FAIL")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
