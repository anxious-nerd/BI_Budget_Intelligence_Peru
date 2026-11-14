"""Tests del ETL y del modelo alineado. Los más importantes:
  * test_model_conformance_strict      : las tablas publicadas == las columnas del modelo del equipo.
  * test_cumulative_equals_flow        : mismo dato como acumulado y como flujo => mismo datamart.
  * test_flows_match_independent_oracle: flujo mensual == diferencia de acumulados del CRUDO.
  * test_native_grain_lag_*            : acumulado->flujo al grano nativo (sumar primero daría otro resultado).
  * test_funcional_key_includes_year   : la meta no se atribuye mal si SEC_FUNC se reasigna entre años.
"""
from __future__ import annotations

import contextlib
import csv
import io
from pathlib import Path

import duckdb
import pytest

from config import get_settings
from etl import db, pipeline, profile, validate
from etl.model import ALL_DIMS, CUBES, DATASETS, EXT_DIMS, GASTO, INGRESO, MODEL_DIMS, code, ubigeo_expr
from scripts import check_spec
from scripts.make_sample_data import generate

ROOT = Path(__file__).resolve().parents[1]
TB = ROOT / "data" / "ref" / "TB_UBIGEOS.csv"


def rd(wh: Path, name: str) -> str:
    return f"read_parquet('{(wh / name).as_posix()}/*/*.parquet', hive_partitioning=true)"


def dim(wh: Path, d) -> str:
    return pipeline.dim_source(wh, d)


def raw_file(settings, name: str, year: int = 2024) -> Path:
    pat = settings.raw_gasto if name == "gasto" else settings.raw_ingreso
    return Path(pat.replace("*.parquet", f"{year}-{'Gasto' if name == 'gasto' else 'Ingreso'}.parquet"))


def diff_count(con, a: str, b: str) -> int:
    return con.execute(
        f"SELECT (SELECT count(*) FROM (SELECT * FROM {a} EXCEPT SELECT * FROM {b})) + "
        f"(SELECT count(*) FROM (SELECT * FROM {b} EXCEPT SELECT * FROM {a}))"
    ).fetchone()[0]


def tmp_settings(base, tmp_path, **kw):
    return get_settings(**{**base.__dict__, "warehouse": tmp_path / "wh", "temp_dir": tmp_path / "t", **kw})


# ============================================================ modelo vs diccionario y vs modelo del equipo
@pytest.mark.parametrize("ds,file", [(GASTO, "Gastos_Diccionario.csv"), (INGRESO, "Ingresos_Diccionario.csv")])
def test_model_columns_exist_in_mef_dictionary(ds, file):
    with open(ROOT / "docs" / "diccionarios" / file, encoding="utf-8-sig") as f:
        in_dict = {r["VARIABLE"].strip().lower() for r in csv.DictReader(f)}
    assert set(ds.required_columns) <= in_dict, set(ds.required_columns) - in_dict
    assert set(ds.native_cols) <= in_dict, set(ds.native_cols) - in_dict


def test_classifier_dims_are_separate():
    """GENERICA..ESPECIFICA_DET significan cosas distintas en ingreso y gasto."""
    assert {d.name for d in GASTO.dims} >= {"clasif_gasto"} and "clasif_ingreso" not in {d.name for d in GASTO.dims}
    assert {d.name for d in INGRESO.dims} >= {"clasif_ingreso"} and "clasif_gasto" not in {d.name for d in INGRESO.dims}


@pytest.mark.parametrize("fixture", ["wh_cumulative", "wh_flow", "wh_spec"])
def test_model_conformance_strict(fixture, request, capsys):
    """54 de 54 columnas, sin sobrantes, sin desbordes de VARCHAR(n), mismo orden."""
    s = request.getfixturevalue(fixture)
    assert check_spec.main(["--warehouse", str(s.warehouse), "--strict"]) == 0, capsys.readouterr().out


def test_fact_fks_match_the_model():
    assert GASTO.fk_cols == ("sk_entidad", "sk_ubigeo_ejec", "sk_financiamiento", "sk_clasif_gasto", "sk_funcional")
    assert INGRESO.fk_cols == ("sk_entidad", "sk_ubigeo", "sk_financiamiento", "sk_clasif_ingreso")
    assert GASTO.measures == ("monto_pia", "monto_pim", "monto_devengado", "monto_girado")
    assert INGRESO.measures == ("monto_pia", "monto_pim", "monto_recaudado")


# ============================================================ normalizadores
@pytest.mark.parametrize(
    "value,dtype,expected",
    [(5.0, "DOUBLE", "5"), (None, "DOUBLE", "-"), (float("nan"), "DOUBLE", "-"), (7, "BIGINT", "7"), (None, "BIGINT", "-"),
     (" 01 ", "VARCHAR", "01"), ("", "VARCHAR", "-"), (3, "DECIMAL(10,2)", "3")],
)  # fmt: skip
def test_code_normalization(value, dtype, expected):
    con = duckdb.connect()
    con.execute("CREATE TABLE t(x " + dtype + ")")
    con.execute("INSERT INTO t VALUES (?)", [value])
    assert con.execute(f"SELECT {code('x', dtype)} FROM t").fetchone()[0] == expected


@pytest.mark.parametrize(
    "dep,prov,dist,expected",
    [
        ("15", "01", "01", "150101"),  # 2-2-2
        ("15", "1501", "150101", "150101"),  # 4-6
        ("15", "1501", "01", "150101"),  # provincia = depto+prov, distrito de 2
        ("1", "1", "1", "010101"),  # sin ceros a la izquierda
        ("15", "01", "1", "150101"),
        ("-", "-", "-", "000000"),  # sin dato
        ("15", "-", "-", "150000"),
    ],
)  # fmt: skip
def test_ubigeo_code_conventions(dep, prov, dist, expected):
    con = duckdb.connect()
    con.execute("CREATE TABLE t(departamento_ejecutora VARCHAR, provincia_ejecutora VARCHAR, distrito_ejecutora VARCHAR)")
    con.execute("INSERT INTO t VALUES (?, ?, ?)", [None if dep == "-" else dep, None if prov == "-" else prov, None if dist == "-" else dist])
    types = {c: "VARCHAR" for c in ("departamento_ejecutora", "provincia_ejecutora", "distrito_ejecutora")}
    assert con.execute(f"SELECT {ubigeo_expr(types)} FROM t").fetchone()[0] == expected


def test_ubigeo_code_with_numeric_columns():
    """Si el parquet guardó los códigos como números, se pierden los ceros: igual se reconstruye."""
    con = duckdb.connect()
    con.execute("CREATE TABLE t(departamento_ejecutora BIGINT, provincia_ejecutora BIGINT, distrito_ejecutora BIGINT)")
    con.execute("INSERT INTO t VALUES (1, 1, 1), (15, 1, 5)")
    types = {c: "BIGINT" for c in ("departamento_ejecutora", "provincia_ejecutora", "distrito_ejecutora")}
    assert [r[0] for r in con.execute(f"SELECT {ubigeo_expr(types)} FROM t").fetchall()] == ["010101", "150105"]


# ============================================================ estructura del warehouse
def test_files_and_manifest(wh_spec):
    wh = wh_spec.warehouse
    for ds in DATASETS.values():
        assert sorted(p.name for p in (wh / ds.fact).iterdir()) == ["anio=2023", "anio=2024"]
    for d in ALL_DIMS:
        assert (wh / d.table).is_dir() if d.by_year else (wh / f"{d.table}.parquet").exists()
    assert (wh / "dim_tiempo.parquet").exists() and (wh / "mef.duckdb").exists()
    m = pipeline.read_manifest(wh_spec)
    assert m["exec_mode"] == "cumulative" and set(m["years"]["gasto"]) == {"2023", "2024"} and m["schema_version"] == 2
    assert m["budget_mode"] == {"gasto": "snapshot", "ingreso": "sum"} and m["ubigeo"]["members"] > 0


def test_published_dims_have_exactly_the_model_columns(wh_spec):
    con = duckdb.connect()
    expected = {
        "dim_entidad": ["sk_entidad", "pliego", "ejecutora"],
        "dim_ubigeo": ["sk_ubigeo", "codigo_ubigeo", "departamento", "provincia", "distrito", "latitud", "longitud"],
        "dim_financiamiento": ["sk_financiamiento", "fuente", "rubro", "tipo_recurso"],
        "dim_funcional": ["sk_funcional", "funcion", "division_funcional", "grupo_funcional", "proyecto", "inversion", "meta"],
        "dim_tiempo": ["sk_tiempo", "anio", "mes"],
    }
    for table, cols in expected.items():
        src = dim(wh_spec.warehouse, next(d for d in ALL_DIMS if d.table == table)) if table != "dim_tiempo" else f"read_parquet('{(wh_spec.warehouse / 'dim_tiempo.parquet').as_posix()}')"
        assert [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()] == cols, table


def test_extension_tables_live_outside_the_model(wh_spec):
    """El nivel de gobierno no está en el modelo: vive en ext_entidad, con la MISMA sk_entidad."""
    con = duckdb.connect()
    wh = wh_spec.warehouse
    e = f"read_parquet('{(wh / 'ext_entidad.parquet').as_posix()}')"
    assert con.execute(f"SELECT count(*) = count(DISTINCT sk_entidad) FROM {e}").fetchone()[0]
    assert con.execute(f"SELECT count(*) FROM {e} ANTI JOIN {dim(wh, MODEL_DIMS[0])} d USING (sk_entidad)").fetchone()[0] == 0
    assert set(r[0] for r in con.execute(f"SELECT DISTINCT nivel_gobierno FROM {e}").fetchall()) >= {"GOBIERNOS LOCALES"}


def test_diario_files_are_ignored(wh_cumulative):
    """Hay un *-Diario.parquet con copia de 2024: si no se ignorara, duplicaría las filas."""
    con = duckdb.connect()
    raw = con.execute(f"SELECT count(*) FROM read_parquet('{raw_file(wh_cumulative, 'gasto').as_posix()}')").fetchone()[0]
    fact = con.execute(f"SELECT count(*) FROM {rd(wh_cumulative.warehouse, 'fact_gasto')} WHERE anio = 2024").fetchone()[0]
    assert fact == raw


def test_validate_has_no_failures(wh_cumulative, wh_flow, wh_spec):
    for s in (wh_cumulative, wh_flow, wh_spec):
        res = validate.run_checks(s, deep=True)
        assert res and not [c for c in res if c.status == "FAIL"], [c for c in res if c.status == "FAIL"]


def test_cubes_reconcile_with_facts(wh_cumulative):
    con = duckdb.connect()
    for cube in CUBES:
        ds = DATASETS[cube.dataset]
        sums = ", ".join(f"sum({m})" for m in ds.measures)
        a = con.execute(f"SELECT {sums} FROM {rd(wh_cumulative.warehouse, ds.fact)}").fetchone()
        b = con.execute(f"SELECT {sums} FROM {rd(wh_cumulative.warehouse, cube.table)}").fetchone()
        assert a == b, cube.name


# ============================================================ semántica acumulado/flujo
def test_cumulative_equals_flow(wh_cumulative, wh_flow):
    con = duckdb.connect()
    for name in ["fact_gasto", "fact_ingreso", *(c.table for c in CUBES)]:
        assert diff_count(con, rd(wh_cumulative.warehouse, name), rd(wh_flow.warehouse, name)) == 0, name
    for d in ALL_DIMS:
        assert diff_count(con, dim(wh_cumulative.warehouse, d), dim(wh_flow.warehouse, d)) == 0, d.table


def test_flows_match_independent_oracle(wh_cumulative):
    """Oráculo: flujo(m) = SUMA(acumulado crudo en m) - SUMA(acumulado crudo en m-1). No usa el LAG del ETL."""
    con = duckdb.connect()
    raw = f"read_parquet('{raw_file(wh_cumulative, 'gasto').as_posix()}')"
    for measure in ("MONTO_DEVENGADO", "MONTO_GIRADO"):
        cum = dict(con.execute(f"SELECT MES_EJE, sum({measure}::DECIMAL(18,2)) FROM {raw} GROUP BY 1").fetchall())
        got = dict(con.execute(f"SELECT sk_tiempo % 100, sum({measure.lower()}) FROM {rd(wh_cumulative.warehouse, 'fact_gasto')} WHERE anio = 2024 GROUP BY 1").fetchall())
        for m in range(1, 13):
            assert got.get(m, 0) == cum.get(m, 0) - cum.get(m - 1, 0), (measure, m)


def test_stock_measures_are_not_accumulated(wh_cumulative):
    """PIM es saldo: el ETL NO debe tocarlo (sumarlo/restarlo entre meses sería un error grave)."""
    con = duckdb.connect()
    raw = f"read_parquet('{raw_file(wh_cumulative, 'gasto').as_posix()}')"
    for m in (1, 6, 12):
        a = con.execute(f"SELECT sum(MONTO_PIM::DECIMAL(18,2)) FROM {raw} WHERE MES_EJE = {m}").fetchone()[0]
        b = con.execute(f"SELECT sum(monto_pim) FROM {rd(wh_cumulative.warehouse, 'fact_gasto')} WHERE sk_tiempo = {202400 + m}").fetchone()[0]
        assert a == b


def _two_series_raw(tmp_path, base_raw: Path):
    """Raw mínimo con DOS series nativas que colapsan al mismo grano del modelo (difieren solo en PROGRAMA_PPTO,
    una columna que el modelo no publica) y una de ellas con un mes faltante."""
    con = duckdb.connect()
    sec_ejec, sec_func = con.execute(
        f"SELECT SEC_EJEC, SEC_FUNC FROM read_parquet('{base_raw.as_posix()}') GROUP BY 1, 2 HAVING min(MES_EJE) = 1 LIMIT 1"
    ).fetchone()
    raw_dir = tmp_path / "raw" / "gasto"
    raw_dir.mkdir(parents=True)
    out = raw_dir / "2024-Gasto.parquet"
    src = f"read_parquet('{base_raw.as_posix()}')"
    sel = f"{src} WHERE SEC_EJEC = '{sec_ejec}' AND SEC_FUNC = '{sec_func}'"
    con.execute(f"""
        COPY (
          SELECT * REPLACE (CASE MES_EJE WHEN 1 THEN 100.0 WHEN 2 THEN 150.0 WHEN 3 THEN 200.0 END AS MONTO_DEVENGADO)
          FROM {sel} AND MES_EJE IN (1, 2, 3)
          UNION ALL BY NAME
          SELECT * REPLACE ('9999' AS PROGRAMA_PPTO, CASE MES_EJE WHEN 1 THEN 10.0 WHEN 3 THEN 40.0 END AS MONTO_DEVENGADO)
          FROM {sel} AND MES_EJE IN (1, 3)
        ) TO '{out.as_posix()}' (FORMAT PARQUET)""")
    return raw_dir


def test_native_grain_lag_handles_gaps_and_collapsing_series(tmp_path, wh_cumulative):
    """Serie A: acumulado 100,150,200 (meses 1-3). Serie B: 10, [mes 2 ausente], 40.
    Al grano del modelo A y B colapsan. Convertir a flujo AL GRANO NATIVO da 110, 50, 80;
    sumar primero (110, 150, 240) y luego restar daría 110, 40, 90: incorrecto."""
    raw_dir = _two_series_raw(tmp_path, raw_file(wh_cumulative, "gasto"))
    s = get_settings(raw_gasto=str(raw_dir / "*.parquet"), raw_ingreso="x", warehouse=tmp_path / "wh", temp_dir=tmp_path / "t",
                     exec_mode="cumulative", ubigeo_table=TB)  # fmt: skip
    pipeline.run(s, datasets=["gasto"], steps=["dims", "facts"])
    rows = duckdb.connect().execute(f"SELECT sk_tiempo % 100, sum(monto_devengado) FROM {rd(s.warehouse, 'fact_gasto')} GROUP BY 1 ORDER BY 1").fetchall()
    assert [(m, float(v)) for m, v in rows] == [(1, 110.0), (2, 50.0), (3, 80.0)]


def test_sparse_cumulative_series(tmp_path, wh_cumulative):
    """Serie acumulada 100, 150, [mes 3 ausente], 200  =>  flujos 100, 50, 50 (meses 1, 2, 4)."""
    con = duckdb.connect()
    base = raw_file(wh_cumulative, "gasto")
    sec_ejec, sec_func = con.execute(
        f"SELECT SEC_EJEC, SEC_FUNC FROM read_parquet('{base.as_posix()}') GROUP BY 1, 2 HAVING min(MES_EJE) = 1 LIMIT 1"
    ).fetchone()
    raw_dir = tmp_path / "raw" / "gasto"
    raw_dir.mkdir(parents=True)
    out = raw_dir / "2024-Gasto.parquet"
    con.execute(f"""
        COPY (SELECT * REPLACE (CASE MES_EJE WHEN 1 THEN 100.0 WHEN 2 THEN 150.0 WHEN 4 THEN 200.0 END AS MONTO_DEVENGADO)
              FROM read_parquet('{base.as_posix()}') WHERE SEC_EJEC = '{sec_ejec}' AND SEC_FUNC = '{sec_func}' AND MES_EJE IN (1, 2, 4))
        TO '{out.as_posix()}' (FORMAT PARQUET)""")
    s = get_settings(raw_gasto=str(raw_dir / "*.parquet"), raw_ingreso="x", warehouse=tmp_path / "wh", temp_dir=tmp_path / "t",
                     exec_mode="cumulative", ubigeo_table=TB)  # fmt: skip
    pipeline.run(s, datasets=["gasto"], steps=["dims", "facts"])
    rows = con.execute(f"SELECT sk_tiempo % 100, monto_devengado FROM {rd(s.warehouse, 'fact_gasto')} ORDER BY 1").fetchall()
    assert [(m, float(v)) for m, v in rows] == [(1, 100.0), (2, 50.0), (4, 50.0)]


# ============================================================ incrementalidad y seguridad
def test_incremental_equals_full(wh_cumulative, tmp_path):
    """Construir 2023, luego 2024 por separado == construir ambos de una vez."""
    s = tmp_settings(wh_cumulative, tmp_path, warehouse=tmp_path / "inc")
    pipeline.run(s, years=[2023])
    pipeline.run(s, years=[2024])
    pipeline.run(s, years=[2024])  # idempotente: repetir no cambia nada
    con = duckdb.connect()
    for name in ["fact_gasto", "fact_ingreso", "cube_gasto_entidad", "cube_gasto_funcion", "cube_ingreso_ubigeo"]:
        assert diff_count(con, rd(s.warehouse, name), rd(wh_cumulative.warehouse, name)) == 0, name
    for d in ALL_DIMS:
        assert diff_count(con, dim(s.warehouse, d), dim(wh_cumulative.warehouse, d)) == 0, d.table


def test_exec_mode_mismatch_is_blocked(wh_cumulative):
    s = get_settings(**{**wh_cumulative.__dict__, "exec_mode": "flow"})
    with pytest.raises(pipeline.PreflightError, match="--full"):
        pipeline.run(s, years=[2024])


def test_old_warehouse_layout_is_blocked(tmp_path, wh_cumulative):
    s = tmp_settings(wh_cumulative, tmp_path)
    s.warehouse.mkdir(parents=True)
    (s.warehouse / "manifest.json").write_text('{"schema_version": 1, "exec_mode": "cumulative", "years": {}, "cubes": {}}')
    with pytest.raises(pipeline.PreflightError, match="--full"):
        pipeline.run(s, years=[2024])


def test_missing_columns_fail_fast(tmp_path):
    con = duckdb.connect()
    d = tmp_path / "g"
    d.mkdir()
    con.execute(f"COPY (SELECT 2024 AS ANO_EJE, 1 AS MES_EJE) TO '{(d / 'x.parquet').as_posix()}' (FORMAT PARQUET)")
    s = get_settings(raw_gasto=str(d / "*.parquet"), raw_ingreso=str(d / "*.parquet"), warehouse=tmp_path / "wh", temp_dir=tmp_path / "t", ubigeo_table=TB)
    with pytest.raises(pipeline.PreflightError, match="faltan columnas"):
        pipeline.run(s, datasets=["gasto"])


def test_year_filter_is_pushed_down_to_parquet(wh_cumulative):
    """El predicado de año debe quedar en el scan (si no, cada año leería TODOS los años)
    y el archivo *-Diario debe descartarse por filtro de archivo."""
    import re

    con = duckdb.connect()
    src = pipeline.Source(con, wh_cumulative, GASTO)
    plan = con.execute(f"EXPLAIN ANALYZE SELECT count(*) FROM {src.sql} WHERE {src.where(2024)}").fetchall()[0][1]
    m = re.search(r"Filters:(.*?)File Filters:(.*?)Scanning Files", plan, re.S)
    assert m, plan
    assert "ano_eje = 2024" in m.group(1).lower(), m.group(1)
    assert "diario" in m.group(2).lower(), m.group(2)


def test_invalid_config_is_rejected():
    with pytest.raises(ValueError, match="MEF_EXEC_MODE"):
        get_settings(exec_mode="acumulado")
    with pytest.raises(ValueError, match="BUDGET_MODE"):
        get_settings(budget_mode_ingreso="acumulado")
    with pytest.raises(ValueError, match="UBIGEO_CODE"):
        get_settings(ubigeo_code="sunat")


# ============================================================ mes 0 (apertura) y saldos presupuestales
def test_month_zero_rows_are_kept(wh_spec):
    """Regresión: el ETL descartaba MES_EJE=0 y con ello TODO el PIA/PIM del gasto."""
    con = duckdb.connect()
    raw = f"read_parquet('{raw_file(wh_spec, 'gasto').as_posix()}')"
    raw0 = con.execute(f"SELECT count(*) FROM {raw} WHERE MES_EJE = 0").fetchone()[0]
    fact0 = con.execute(f"SELECT count(*) FROM {rd(wh_spec.warehouse, 'fact_gasto')} WHERE sk_tiempo = 202400").fetchone()[0]
    assert raw0 > 0 and fact0 == raw0
    assert con.execute(f"SELECT count(*) FROM {rd(wh_spec.warehouse, 'fact_gasto')} WHERE anio = 2024").fetchone()[0] == con.execute(f"SELECT count(*) FROM {raw}").fetchone()[0]


def test_stock_totals_preserved_in_spec_warehouse(wh_spec):
    con = duckdb.connect()
    for name, ds in (("gasto", GASTO), ("ingreso", INGRESO)):
        for m in ("monto_pia", "monto_pim"):
            raw = con.execute(f"SELECT sum({m}::DECIMAL(18,2)) FROM read_parquet('{raw_file(wh_spec, name).as_posix()}')").fetchone()[0]
            fact = con.execute(f"SELECT sum({m}) FROM {rd(wh_spec.warehouse, ds.fact)} WHERE anio = 2024").fetchone()[0]
            assert raw == fact, (name, m)


def test_dim_tiempo_has_apertura(wh_spec):
    con = duckdb.connect()
    t = f"read_parquet('{(wh_spec.warehouse / 'dim_tiempo.parquet').as_posix()}')"
    assert con.execute(f"SELECT sk_tiempo, anio, mes FROM {t} WHERE sk_tiempo = 202400").fetchone() == (202400, 2024, 0)
    assert con.execute(f"SELECT count(*) FROM {t} WHERE anio = 2024").fetchone()[0] == 13  # apertura + 12 meses


def test_rows_with_invalid_month_are_reported_not_hidden(tmp_path, wh_spec):
    """Un mes fuera de 0-12 sigue descartándose, pero `validate` lo avisa (no es silencioso)."""
    con = duckdb.connect()
    raw = tmp_path / "raw"
    for name, f in (("gasto", "2024-Gasto.parquet"), ("ingreso", "2024-Ingreso.parquet")):
        (raw / name).mkdir(parents=True)
        src = f"read_parquet('{raw_file(wh_spec, name).as_posix()}')"
        mcol = "MES_EJE" if name == "gasto" else "MES_DOC"
        con.execute(f"COPY (SELECT * FROM {src} UNION ALL BY NAME (SELECT * REPLACE (13 AS {mcol}) FROM {src} LIMIT 5)) TO '{(raw / name / f).as_posix()}' (FORMAT PARQUET)")
    s = tmp_settings(wh_spec, tmp_path, raw_gasto=str(raw / "gasto" / "*.parquet"), raw_ingreso=str(raw / "ingreso" / "*.parquet"))
    pipeline.run(s, years=[2024])
    warns = [c for c in validate.run_checks(s) if "mes inválido" in c.name]
    assert warns and warns[0].status == "WARN"


def test_profile_shows_month_zero_budget_and_ubigeo_diagnosis(wh_spec):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        res_g = profile.profile_dataset(db.connect(wh_spec), wh_spec, GASTO, 2024)
        res_i = profile.profile_dataset(db.connect(wh_spec), wh_spec, INGRESO, 2024)
    out = buf.getvalue()
    assert "Mes 0 (apertura): PRESENTE" in out
    assert res_g["suggested_budget_mode"] == "snapshot" and res_i["suggested_budget_mode"] == "sum"
    assert res_g["suggested_ubigeo_code"] == "inei" and "convención: 2-2-2" in out


@pytest.mark.parametrize(
    "by_month,expected",
    [
        ({0: 100.0, 1: 0, 2: 0, 3: 0}, "snapshot"),  # solo apertura
        ({m: 100.0 + m for m in range(1, 13)}, "snapshot"),  # repetido cada mes
        ({1: 500.0, 2: 20.0, 3: 5.0, 4: -3.0, 5: 8.0, 6: 2.0, 7: 1.0, 8: 4.0}, "sum"),  # modificaciones
        ({m: 0.0 for m in range(0, 13)}, "?"),
    ],
)
def test_budget_verdict_heuristic(by_month, expected):
    assert profile.pim_verdict(by_month)[0] == expected


# ============================================================ ubigeo (latitud / longitud)
def test_ubigeo_coordinates_come_from_the_table(wh_spec):
    """Las coordenadas de dim_ubigeo == las de la tabla (que usa COMA decimal), cruzadas por código."""
    con = duckdb.connect()
    con.execute(f"""CREATE TABLE tb AS SELECT trim(ubigeo_inei) cod, try_cast(replace(trim(latitud), ',', '.') AS DOUBLE) lat,
                    try_cast(replace(trim(longitud), ',', '.') AS DOUBLE) lon
                    FROM read_csv('{TB.as_posix()}', delim=';', header=true, all_varchar=true, encoding='utf-8')""")
    d = f"read_parquet('{(wh_spec.warehouse / 'dim_ubigeo.parquet').as_posix()}')"
    # todo código del dim existe en la tabla de ubigeos
    assert con.execute(f"SELECT count(*) FROM {d} u ANTI JOIN tb ON tb.cod = u.codigo_ubigeo").fetchone()[0] == 0
    bad = con.execute(f"SELECT count(*) FROM {d} u JOIN tb ON tb.cod = u.codigo_ubigeo WHERE u.latitud IS DISTINCT FROM tb.lat OR u.longitud IS DISTINCT FROM tb.lon").fetchone()[0]
    assert bad == 0
    n, lo_lat, hi_lat, lo_lon, hi_lon = con.execute(f"SELECT count(latitud), min(latitud), max(latitud), min(longitud), max(longitud) FROM {d}").fetchone()
    assert n > 10 and -18.5 < lo_lat < hi_lat < 0.5 and -81.5 < lo_lon < hi_lon < -68.5  # dentro del Perú
    assert con.execute(f"SELECT typeof(latitud) FROM {d} LIMIT 1").fetchone()[0] == "DOUBLE"


def test_ubigeo_convention_does_not_change_the_dimension(tmp_path, wh_spec):
    """El MEF puede entregar los códigos como 2-2-2 o como 4-6: debe resultar el MISMO dim_ubigeo."""
    out = {}
    for style in ("2-2-2", "4-6"):
        raw = tmp_path / style / "raw"
        generate(raw, years=(2024,), entidades=60, claves=2, ubigeo_style=style)
        s = tmp_settings(wh_spec, tmp_path / style, raw_gasto=str(raw / "gasto" / "*.parquet"), raw_ingreso=str(raw / "ingreso" / "*.parquet"))
        pipeline.run(s, steps=["dims"])
        out[style] = duckdb.connect().execute(
            f"SELECT sk_ubigeo, codigo_ubigeo, departamento, provincia, distrito, latitud, longitud FROM read_parquet('{(s.warehouse / 'dim_ubigeo.parquet').as_posix()}') ORDER BY 1"
        ).fetchall()
    assert out["2-2-2"] == out["4-6"] and len(out["2-2-2"]) >= 10


def test_ubigeo_inei_vs_reniec_changes_coverage(tmp_path, wh_spec):
    """Con datos en código INEI, cruzar por RENIEC deja muchas ubicaciones sin coordenadas (y se informa)."""
    cov = {}
    for code_ in ("inei", "reniec"):
        s = tmp_settings(wh_spec, tmp_path / code_, ubigeo_code=code_)
        m = pipeline.run(s, steps=["dims"])
        cov[code_] = m["ubigeo"]["with_coordinates"] / m["ubigeo"]["members"]
    assert cov["inei"] > 0.95 and cov["reniec"] < cov["inei"] - 0.2


def test_missing_ubigeo_table_still_builds_without_coordinates(tmp_path, wh_spec):
    s = tmp_settings(wh_spec, tmp_path, ubigeo_table=tmp_path / "no_existe.csv")
    pipeline.run(s, steps=["dims"])
    n, ok = duckdb.connect().execute(f"SELECT count(*), count(latitud) FROM read_parquet('{(s.warehouse / 'dim_ubigeo.parquet').as_posix()}')").fetchone()
    assert n > 0 and ok == 0


def test_bad_ubigeo_table_fails_fast(tmp_path, wh_spec):
    bad = tmp_path / "bad.csv"
    bad.write_text("a;b;c\n1;2;3\n", encoding="utf-8")
    s = tmp_settings(wh_spec, tmp_path, ubigeo_table=bad)
    with pytest.raises(pipeline.PreflightError, match="ubigeo"):
        pipeline.run(s, steps=["dims"])


# ============================================================ decisiones de diseño del modelo
def test_funcional_key_includes_year(tmp_path, wh_spec):
    """Si SEC_FUNC se reasigna entre años, una clave sin año atribuiría mal las funciones de los años viejos."""
    con = duckdb.connect()
    for name in ("gasto", "ingreso"):
        (tmp_path / "raw" / name).mkdir(parents=True)
    src23 = raw_file(wh_spec, "gasto", 2023)
    con.execute(f"""COPY (SELECT * REPLACE (CASE WHEN SEC_EJEC = '000001' AND SEC_FUNC = '00001' THEN 'FUNCION DISTINTA 2023' ELSE FUNCION_NOMBRE END AS FUNCION_NOMBRE)
                    FROM read_parquet('{src23.as_posix()}')) TO '{(tmp_path / 'raw/gasto/2023-Gasto.parquet').as_posix()}' (FORMAT PARQUET)""")
    for f, name in ((raw_file(wh_spec, "gasto", 2024), "gasto"), (raw_file(wh_spec, "ingreso", 2023), "ingreso"), (raw_file(wh_spec, "ingreso", 2024), "ingreso")):
        con.execute(f"COPY (SELECT * FROM read_parquet('{f.as_posix()}')) TO '{(tmp_path / 'raw' / name / f.name).as_posix()}' (FORMAT PARQUET)")
    s = tmp_settings(wh_spec, tmp_path, raw_gasto=str(tmp_path / "raw/gasto/*.parquet"), raw_ingreso=str(tmp_path / "raw/ingreso/*.parquet"))
    pipeline.run(s)
    wh = s.warehouse
    rows = con.execute(f"""
        SELECT f.anio, d.funcion FROM {rd(wh, 'fact_gasto')} f JOIN {dim(wh, GASTO.fks[-1][1])} d USING (sk_funcional)
        JOIN {dim(wh, MODEL_DIMS[0])} e USING (sk_entidad)
        WHERE e.ejecutora = 'UNIDAD EJECUTORA 1' AND f.sk_tiempo % 100 = 1 AND f.monto_devengado >= 0 AND f.sk_funcional IN
          (SELECT sk_funcional FROM {dim(wh, GASTO.fks[-1][1])} WHERE funcion = 'FUNCION DISTINTA 2023')
        GROUP BY ALL ORDER BY 1""").fetchall()
    assert [r[0] for r in rows] == [2023]  # el nombre distinto solo se asigna a los hechos de 2023
    # y el mismo SEC_EJEC+SEC_FUNC de 2024 conserva su función original
    assert con.execute(f"SELECT count(*) FROM {dim(wh, GASTO.fks[-1][1])} WHERE funcion = 'FUNCION DISTINTA 2023'").fetchone()[0] == 1
    chg = [c for c in validate.run_checks(s, with_raw=False) if "metas que cambian" in c.name][0]
    assert chg.detail.startswith("1 de ")


def test_entidad_is_conformed_even_if_sec_ejec_types_differ(tmp_path, wh_spec):
    """Gasto guarda SEC_EJEC como texto '000001' e ingreso como número 1: debe ser la MISMA sk_entidad."""
    con = duckdb.connect()
    for name in ("gasto", "ingreso"):
        (tmp_path / "raw" / name).mkdir(parents=True)
    g, i = raw_file(wh_spec, "gasto"), raw_file(wh_spec, "ingreso")
    con.execute(f"COPY (SELECT * FROM read_parquet('{g.as_posix()}')) TO '{(tmp_path / 'raw/gasto/2024-Gasto.parquet').as_posix()}' (FORMAT PARQUET)")
    con.execute(f"COPY (SELECT * REPLACE (CAST(SEC_EJEC AS BIGINT) AS SEC_EJEC) FROM read_parquet('{i.as_posix()}')) TO '{(tmp_path / 'raw/ingreso/2024-Ingreso.parquet').as_posix()}' (FORMAT PARQUET)")
    s = tmp_settings(wh_spec, tmp_path, raw_gasto=str(tmp_path / "raw/gasto/*.parquet"), raw_ingreso=str(tmp_path / "raw/ingreso/*.parquet"))
    pipeline.run(s)
    wh = s.warehouse
    sg = {r[0] for r in con.execute(f"SELECT DISTINCT sk_entidad FROM {rd(wh, 'fact_gasto')}").fetchall()}
    si = {r[0] for r in con.execute(f"SELECT DISTINCT sk_entidad FROM {rd(wh, 'fact_ingreso')}").fetchall()}
    assert si and si <= sg
    assert con.execute(f"SELECT count(*) FROM {dim(wh, MODEL_DIMS[0])}").fetchone()[0] == len(sg)  # sin entidades duplicadas


def test_entity_dimension_has_one_row_per_sec_ejec(wh_spec):
    con = duckdb.connect()
    n_dim = con.execute(f"SELECT count(*) FROM {dim(wh_spec.warehouse, MODEL_DIMS[0])}").fetchone()[0]
    n_raw = con.execute(f"SELECT count(DISTINCT SEC_EJEC) FROM read_parquet('{raw_file(wh_spec, 'gasto').as_posix()}')").fetchone()[0]
    assert n_dim == n_raw


def test_extension_dims_are_not_part_of_the_published_model():
    assert {d.table for d in EXT_DIMS} == {"ext_entidad"} and all(d.prefix == "ext" for d in EXT_DIMS)
    assert all(d.prefix == "dim" for d in MODEL_DIMS)
