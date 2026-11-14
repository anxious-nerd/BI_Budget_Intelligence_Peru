"""Diagnóstico de tus parquet ANTES de construir el datamart.

    python -m etl.profile                 # usa el último año disponible
    python -m etl.profile --year 2023

Responde las preguntas que no se pueden saber leyendo el diccionario:
  1. ¿Están todas las columnas que el ETL necesita? ¿Con qué tipos?
  2. ¿Hay archivos "Diario" mezclados con los "Mensual"? (duplicarían los montos)
  3. ¿Hay códigos con longitudes mezcladas (se perdieron ceros a la izquierda)?
  4. ¿Los montos de ejecución mensual vienen ACUMULADOS o como FLUJO?  -> MEF_EXEC_MODE
  5. ¿PIA/PIM se repiten cada mes (saldo) o aparecen una sola vez?
  6. ¿Hay filas duplicadas en el grano nativo?
"""
from __future__ import annotations

import argparse
import logging
import sys

import pandas as pd

from config import Settings, get_settings
from etl import db, pipeline
from etl.model import DATASETS, Dataset, code, money, ubigeo_expr

# Columnas-código donde una longitud mezclada casi seguro significa ceros perdidos.
FIXED_WIDTH_HINTS = {
    "sector", "departamento_ejecutora", "provincia_ejecutora", "distrito_ejecutora",
    "departamento_meta", "funcion", "rubro", "fuente_financiamiento",
}  # fmt: skip


def _native_key(src) -> str:
    """Serie nativa: todas las columnas de código que publica el MEF (igual que usa el ETL)."""
    return "hash(" + ", ".join(src.native) + ")"


def semantics_verdict(series: int, with_drop: int) -> tuple[str, str]:
    """Devuelve (modo sugerido, explicación) a partir de series monotónicas."""
    if series < 30:
        return "?", f"solo {series} series con ≥3 meses: muestra insuficiente para decidir"
    share = with_drop / series
    if share <= 0.05:
        return "cumulative", f"{share:.1%} de las series baja alguna vez -> se comportan como ACUMULADO al mes"
    if share >= 0.25:
        return "flow", f"{share:.1%} de las series baja alguna vez -> se comportan como FLUJO del mes"
    return "?", f"{share:.1%} de las series baja alguna vez: zona ambigua, compara con el portal (ver README, paso 3)"


def pim_verdict(by_month: dict[int, float]) -> tuple[str, str]:
    """Sugiere cómo se registra el PIM mes a mes, a partir de {mes: suma de PIM}.

    snapshot = saldo (solo en el mes 0 de apertura, o repetido con nivel estable cada mes)
    sum      = modificaciones del periodo (el vigente es la suma acumulada)
    """
    nz = {m: v for m, v in by_month.items() if abs(v) > 1e-9}
    if not nz:
        return "?", "el PIM es 0 en todos los meses"
    later = {m: v for m, v in nz.items() if m > 0}
    if 0 in nz and not later:
        return "snapshot", "el PIM aparece solo en el mes 0 (apertura): es un saldo"
    vals = [abs(v) for v in later.values()]
    n_months = len([m for m in by_month if m > 0])
    stable = len(later) >= 0.8 * n_months and min(vals) >= 0.5 * max(vals)
    if stable:
        return "snapshot", "el PIM aparece en casi todos los meses con nivel parecido: se repite como saldo"
    if any(v < 0 for v in later.values()) or min(vals) < 0.3 * max(vals):
        return "sum", "el PIM varía mucho de un mes a otro (y/o hay negativos): parecen modificaciones del periodo"
    return "?", "patrón ambiguo: compara con el portal (ver README, paso 3)"


def profile_dataset(con, settings: Settings, ds: Dataset, year: int | None, sample_mod: int = 50) -> dict:
    src = pipeline.Source(con, settings, ds)
    res: dict = {"dataset": ds.name}
    print(f"\n{'=' * 78}\nDATASET: {ds.name.upper()}\n{'=' * 78}")

    # 1) columnas ----------------------------------------------------------------------
    missing = [c for c in ds.required_columns if c not in src.cols]
    extra = sorted(set(src.cols) - set(ds.required_columns) - {"filename"})
    print(f"[1] Columnas: {len(src.cols)} en el origen | faltan {len(missing)} | sobran {len(extra)}")
    if missing:
        print(f"    ✗ FALTAN: {missing}")
        res["ok"] = False
        return res
    if extra:
        print(f"    · sobran (se ignoran): {extra}")
    bad_money = [m for m in ds.measures if src.cols[m] in ("VARCHAR",)]
    print(f"    tipo año={src.cols[ds.year_col]} mes={src.cols[ds.month_col]} "
          f"montos={sorted({src.cols[m] for m in ds.measures})}")  # fmt: skip
    if bad_money:
        print(f"    ⚠ montos como texto ({bad_money}): se intentará TRY_CAST; revisa el punto [5]")

    # 2) archivos diario -----------------------------------------------------------------
    if "filename" in src.cols:
        files = [r[0] for r in con.execute(f"SELECT DISTINCT filename FROM {src.sql}").fetchall()]
        ignored = [f for f in files if pipeline.re_search(settings.exclude_files_regex, f)]
        print(f"[2] Archivos: {len(files)} en el origen | ignorados por MEF_EXCLUDE_FILES_REGEX: {len(ignored)}")
        for f in ignored:
            print(f"    · ignorado: {f}")
    years = src.years(con)
    year = year or (years[-1] if years else None)
    print(f"    Años disponibles: {years} | perfilando: {year}")
    if year is None:
        return res
    w = src.where(year)

    # 3) códigos de longitud mezclada ----------------------------------------------------
    print("[3] Códigos: longitud mezclada (ceros perdidos) y formato '5.0' en columnas de texto:")
    found = False
    for k in src.native:
        ctype = src.cols[k]
        if k in FIXED_WIDTH_HINTS:
            lens = con.execute(
                f"SELECT DISTINCT length({code(k, ctype)}) FROM {src.sql} WHERE {w} AND {code(k, ctype)} <> '-' ORDER BY 1"
            ).fetchall()
            if len(lens) > 1:
                found = True
                print(f"    ⚠ {k} ({ctype}): longitudes {[r[0] for r in lens]}  -> reexporta esa columna como texto")
        if ctype == "VARCHAR":
            n_dot = con.execute(
                f"SELECT count(*) FROM (SELECT DISTINCT {k} AS v FROM {src.sql} WHERE {w}) "
                "WHERE regexp_matches(trim(v), '^[0-9]+\\.0+$')"
            ).fetchone()[0]
            if n_dot:
                found = True
                print(f"    ⚠ {k}: {n_dot} códigos con formato '5.0' en una columna de TEXTO "
                      "(el ETL no los corrige en texto; corrige el origen)")
    if not found:
        print("    ✓ sin hallazgos")

    # 3b) ubicación: convención de códigos y cruce con la tabla de ubigeos (latitud/longitud) -------
    print("[3b] Ubicación de la entidad (para dim_ubigeo y los mapas):")
    parts = ("departamento_ejecutora", "provincia_ejecutora", "distrito_ejecutora")
    lens = {c: [r[0] for r in con.execute(f"SELECT DISTINCT length({code(c, src.cols[c])}) FROM {src.sql} WHERE {w} AND {code(c, src.cols[c])} <> '-' ORDER BY 1").fetchall()] for c in parts}  # fmt: skip
    conv = "distrito = ubigeo completo (6)" if max(lens[parts[2]] or [0]) >= 6 else (
        "provincia = depto+provincia (4)" if max(lens[parts[1]] or [0]) >= 4 else "2-2-2 (depto, provincia, distrito)")
    print(f"    longitudes de código: depto {lens[parts[0]]} | provincia {lens[parts[1]]} | distrito {lens[parts[2]]}  ->  convención: {conv}")
    p = settings.ubigeo_table
    if not p.exists():
        print(f"    ⚠ no existe la tabla de ubigeos ({p}): no habrá latitud/longitud ni mapas")
    else:
        tb = f"read_csv('{p.as_posix()}', delim=';', header=true, all_varchar=true, encoding='utf-8')"
        n_rows, n_codes = con.execute(
            f"SELECT sum(n), count(*) FROM (SELECT count(*) AS n FROM {src.sql} WHERE {w} GROUP BY {', '.join(parts)})"
        ).fetchone()
        best, best_rate = None, -1.0
        for col in ("inei", "reniec"):
            matched = con.execute(f"""
                WITH c AS (SELECT {ubigeo_expr(src.cols)} AS cod, n
                           FROM (SELECT {', '.join(parts)}, count(*) AS n FROM {src.sql} WHERE {w} GROUP BY {', '.join(parts)})),
                     t AS (SELECT DISTINCT trim(ubigeo_{col}) AS cod FROM {tb})
                SELECT coalesce(sum(n) FILTER (WHERE t.cod IS NOT NULL), 0), count(*) FILTER (WHERE t.cod IS NOT NULL), count(*)
                FROM c LEFT JOIN t USING (cod)""").fetchone()
            rate = float(matched[0]) / float(n_rows) if n_rows else 0.0
            print(f"    código {col.upper():6s}: {matched[1]:,} de {matched[2]:,} ubicaciones distintas existen en la tabla ({rate:.1%} de las filas)")
            if rate > best_rate:
                best, best_rate = col, rate
        res["suggested_ubigeo_code"] = best
        print(f"    ➜ Sugerencia: MEF_UBIGEO_CODE={best}   (configurado: {settings.ubigeo_code})")
        if best_rate < 0.95:
            print("      ⚠ menos del 95 % de las filas calza con la tabla: revisa la convención de códigos del MEF o la versión de la tabla")

    # 4) montos ------------------------------------------------------------------------
    print("[4] Montos:")
    for m in ds.measures:
        nn, nulls, neg = con.execute(
            f"SELECT count({m}), count(*) FILTER (WHERE {m} IS NOT NULL AND try_cast({m} as decimal(18,2)) IS NULL), "
            f"count(*) FILTER (WHERE try_cast({m} as decimal(18,2)) < 0) FROM {src.sql} WHERE {w}"
        ).fetchone()
        flag = "⚠ NO numéricos: " + str(nulls) if nulls else "ok"
        print(f"    {m:28s} no nulos={nn:>12,}  negativos={neg:>9,}  {flag}")

    # 5) semántica mensual: tabla por mes ---------------------------------------------------
    cols = ", ".join(f"sum({money(m)}) AS {m.replace('monto_', '')}" for m in ds.measures)
    by_month = con.execute(
        f"SELECT try_cast({ds.month_col} as integer) AS mes, count(*) AS filas, {cols} "
        f"FROM {src.sql} WHERE {w} GROUP BY 1 ORDER BY 1"
    ).df()
    print(f"[5] Suma por mes en {year} (todas las filas):")
    pd.options.display.float_format = "{:,.0f}".format
    print(by_month.to_string(index=False))
    first_flow = ds.flow_measures[-2] if ds.name == "gasto" else ds.flow_measures[0]  # devengado / recaudado
    total_all = float(by_month[first_flow.replace("monto_", "")].sum())
    last_month = float(by_month[first_flow.replace("monto_", "")].iloc[-1])
    print(f"\n    {first_flow}: suma de TODOS los meses = {total_all:,.0f} | solo el último mes = {last_month:,.0f}")
    print("    >> Compara con el total anual del mismo año en el portal Consulta Amigable (MEF):")
    print("       - si coincide con el ÚLTIMO mes  -> MEF_EXEC_MODE=cumulative")
    print("       - si coincide con la SUMA de meses -> MEF_EXEC_MODE=flow")
    # --- meses: ¿hay apertura (mes 0)? ¿se descarta algo?
    bad = con.execute(
        f"SELECT count(*), coalesce(sum({money('monto_pim')}), 0) FROM {src.sql} WHERE {src.where(year, valid_month=False)} "
        f"AND NOT coalesce(try_cast({ds.month_col} as integer) between 0 and 12, false)"
    ).fetchone()
    has0 = 0 in set(by_month["mes"].tolist())
    print(f"    Mes 0 (apertura): {'PRESENTE: trae PIA/PIM sin ejecución; el ETL lo conserva' if has0 else 'no hay filas con mes 0'}")
    print(f"    Filas con mes fuera de 0-12 o nulo (se DESCARTAN): {bad[0]:,}" + (f"  ⚠ con PIM por {float(bad[1]):,.0f}" if bad[0] else ""))

    # --- PIM: ¿saldo o modificaciones del periodo?
    pim_by = {int(m): float(v) for m, v in zip(by_month["mes"], by_month["pim"])}
    last_nz = max((m for m, v in pim_by.items() if abs(v) > 1e-9), default=None)
    print("\n    Saldos presupuestales (PIA y PIM): dos lecturas posibles del vigente del año " + str(year) + ":")
    pia_by = {int(m): float(v) for m, v in zip(by_month["mes"], by_month["pia"])}
    last_pia = max((m for m, v in pia_by.items() if abs(v) > 1e-9), default=None)
    print(f"      a) valor del último mes con saldo   PIM (mes {last_nz}) = {pim_by.get(last_nz, 0):>16,.0f} | PIA (mes {last_pia}) = {pia_by.get(last_pia, 0):>16,.0f}   -> MEF_BUDGET_MODE_{ds.name.upper()}=snapshot")
    print(f"      b) suma de TODOS los meses          PIM            = {sum(pim_by.values()):>16,.0f} | PIA            = {sum(pia_by.values()):>16,.0f}   -> MEF_BUDGET_MODE_{ds.name.upper()}=sum")
    print("      >> compara a) y b) con el PIA y el PIM del mismo año en Consulta Amigable: la lectura que coincida es tu modo.")
    pmode, pwhy = pim_verdict(pim_by)
    cfg = settings.budget_mode_gasto if ds.name == "gasto" else settings.budget_mode_ingreso
    res["suggested_budget_mode"] = pmode
    print(f"    ➜ Sugerencia: MEF_BUDGET_MODE_{ds.name.upper()}={pmode if pmode != '?' else '(decide manualmente)'}   [{pwhy}]")
    print(f"      Configurado actualmente: MEF_BUDGET_MODE_{ds.name.upper()}={cfg}")

    # monotonía (test automático)
    nk = _native_key(src)
    row = con.execute(f"""
        WITH b AS (SELECT {nk} AS nk, try_cast({ds.month_col} as integer) AS mes, sum({money(first_flow)}) AS v
                   FROM {src.sql} WHERE {w} GROUP BY 1, 2),
        s AS (SELECT * FROM b WHERE nk % {sample_mod} = 0),
        d AS (SELECT nk, v, v - lag(v) OVER (PARTITION BY nk ORDER BY mes) AS dv,
                     count(*) OVER (PARTITION BY nk) AS n FROM s)
        SELECT count(DISTINCT nk) FILTER (WHERE n >= 3), count(DISTINCT nk) FILTER (WHERE n >= 3 AND dv < 0) FROM d
    """).fetchone()
    mode, why = semantics_verdict(row[0], row[1])
    res["suggested_exec_mode"] = mode
    print(f"\n    Test automático (muestra 1/{sample_mod} de series, {first_flow}): {row[0]:,} series, {row[1]:,} con alguna baja")
    print(f"    ➜ Sugerencia: MEF_EXEC_MODE={mode if mode != '?' else '(decide manualmente)'}   [{why}]")
    print(f"      Configurado actualmente: MEF_EXEC_MODE={settings.exec_mode}")

    # 6) duplicados --------------------------------------------------------------------
    n, nd = con.execute(
        f"SELECT count(*), count(DISTINCT ({nk}, try_cast({ds.month_col} as integer))) FROM {src.sql} WHERE {w}"
    ).fetchone()
    print(f"[6] Filas: {n:,} | combinaciones (clave nativa, mes) distintas: {nd:,} | repetidas: {n - nd:,}")
    if n != nd:
        print("    ⚠ hay claves repetidas: el ETL las SUMA (consolida) antes de normalizar. Si vienen de")
        print("      mezclar archivos Mensual y Diario, ajusta MEF_EXCLUDE_FILES_REGEX.")
    res["ok"] = True
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--year", type=int)
    ap.add_argument("--dataset", choices=["gasto", "ingreso"], nargs="+", default=["gasto", "ingreso"])
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    settings = get_settings()
    con = db.connect(settings)
    ok = True
    for name in a.dataset:
        ok &= bool(profile_dataset(con, settings, DATASETS[name], a.year).get("ok"))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
