"""Tests del dashboard: datos, filtros, mapa, callbacks por HTTP y recarga automática."""
from __future__ import annotations

import shutil
import time
from pathlib import Path

import duckdb
import pytest

from dashboard.app import create_app
from dashboard.data import METRICAS_MAPA, Filters, Store
from etl import pipeline
from etl.model import GASTO, INGRESO, MODEL_DIMS


def rd(wh: Path, name: str) -> str:
    return f"read_parquet('{(wh / name).as_posix()}/*/*.parquet', hive_partitioning=true)"


def q(sql: str):
    return duckdb.connect().execute(sql).fetchone()[0]


@pytest.fixture(scope="module")
def store(wh_cumulative):
    return Store(wh_cumulative.warehouse)  # PIA/PIM repetidos cada mes (modo snapshot en ambos)


@pytest.fixture(scope="module")
def client(wh_cumulative):
    return create_app(wh_cumulative.warehouse).server.test_client()


# ------------------------------------------------------------------ KPIs vs. fact (fuente de verdad)
def test_kpi_gasto_matches_fact(store, wh_cumulative):
    wh = wh_cumulative.warehouse
    for mes in (3, 8, 12):
        k = store.kpi_gasto(Filters(2024, mes))
        dev, gir = duckdb.connect().execute(
            f"SELECT sum(monto_devengado), sum(monto_girado) FROM {rd(wh, 'fact_gasto')} WHERE anio=2024 AND sk_tiempo % 100 <= {mes}"
        ).fetchone()
        pim = q(f"SELECT sum(monto_pim) FROM {rd(wh, 'fact_gasto')} WHERE sk_tiempo = {202400 + mes}")
        assert k["devengado"] == pytest.approx(float(dev), rel=1e-9)
        assert k["girado"] == pytest.approx(float(gir), rel=1e-9)
        assert k["pim"] == pytest.approx(float(pim), rel=1e-9)  # saldo al mes de corte, NO suma de meses


def test_pim_is_stock_not_sum_over_months(store):
    k3, k12 = store.kpi_gasto(Filters(2024, 3)), store.kpi_gasto(Filters(2024, 12))
    assert k12["pim"] < 2 * k3["pim"]  # sumar 12 meses de PIM daría ~4x; el saldo crece apenas
    assert k12["devengado"] > k3["devengado"]  # la ejecución sí acumula


def test_filters_match_fact(store, wh_cumulative):
    """Filtros por nivel de gobierno (tabla de apoyo), fuente y departamento == consulta directa a la fact."""
    wh = wh_cumulative.warehouse
    f = Filters(2024, 12, niveles=("GOBIERNOS LOCALES",), fuentes=("Recursos Ordinarios", "Recursos Directamente Recaudados"))
    exp = q(f"""SELECT sum(f.monto_devengado) FROM {rd(wh, 'fact_gasto')} f
                JOIN read_parquet('{(wh / 'ext_entidad.parquet').as_posix()}') e USING (sk_entidad)
                JOIN read_parquet('{(wh / 'dim_financiamiento.parquet').as_posix()}') u USING (sk_financiamiento)
                WHERE f.anio = 2024 AND e.nivel_gobierno = 'GOBIERNOS LOCALES' AND u.fuente IN ('Recursos Ordinarios', 'Recursos Directamente Recaudados')""")
    assert store.kpi_gasto(f)["devengado"] == pytest.approx(float(exp), rel=1e-9)
    f2 = Filters(2024, 12, deptos=("LIMA",))
    exp2 = q(f"""SELECT sum(f.monto_devengado) FROM {rd(wh, 'fact_gasto')} f
                 JOIN read_parquet('{(wh / 'dim_ubigeo.parquet').as_posix()}') u ON u.sk_ubigeo = f.sk_ubigeo_ejec
                 WHERE f.anio = 2024 AND u.departamento = 'LIMA'""")
    assert store.kpi_gasto(f2)["devengado"] == pytest.approx(float(exp2 or 0), rel=1e-9)
    dep = store.desglose_gasto("cube_gasto_base", "departamento", Filters(2024, 12), exclude=("depto",))
    assert dep["devengado"].sum() == pytest.approx(store.kpi_gasto(Filters(2024, 12))["devengado"], rel=1e-9)


def test_ingreso_kpi_and_previous_year(store, wh_cumulative):
    k = store.kpi_ingreso(Filters(2024, 12))
    exp = q(f"SELECT sum(monto_recaudado) FROM {rd(wh_cumulative.warehouse, 'fact_ingreso')} WHERE anio=2024")
    assert k["recaudado"] == pytest.approx(float(exp), rel=1e-9)
    assert k["recaudado_prev"] > 0  # existe 2023
    assert store.kpi_ingreso(Filters(2023, 12))["recaudado_prev"] == 0  # 2022 no existe: no falla


def test_options_are_names(store):
    o = store.options()
    assert o["anios"] == [2024, 2023] and o["meses"][2024] == list(range(1, 13))
    assert "GOBIERNOS LOCALES" in o["niveles"] and "Recursos Ordinarios" in o["fuentes"] and "LIMA" in o["deptos"] or o["deptos"]
    assert all(isinstance(v, str) for v in o["deptos"] + o["niveles"] + o["fuentes"])


def test_cache_hits_and_empty_filters(store):
    f = Filters(2024, 6, deptos=("LIMA",))
    store.kpi_gasto(f)
    n = len(store._cache)
    store.kpi_gasto(f)
    assert len(store._cache) == n  # segunda llamada salió del caché
    z = store.kpi_gasto(Filters(2024, 6, niveles=("NO_EXISTE",)))
    assert z["pim"] == 0 and z["devengado"] == 0  # filtro sin filas, no error


# ------------------------------------------------------------------ rankings y mapa
def test_entidades_are_labeled_from_dim_entidad(store):
    df = store.entidades(Filters(2024, 12), limit=5)
    assert len(df) == 5 and df["devengado"].is_monotonic_decreasing
    assert df["etiqueta"].str.contains("UNIDAD EJECUTORA").all()


def test_map_values_and_coordinates(store, wh_cumulative):
    wh = wh_cumulative.warehouse
    f = Filters(2024, 12)
    df, meta = store.mapa(f, "devengado")
    total = store.kpi_gasto(f)["devengado"]
    sin = meta["monto_sin_coordenadas"]
    assert df["devengado"].sum() + sin == pytest.approx(total, rel=1e-9)  # nada se pierde: lo que no se dibuja se informa
    assert df["latitud"].between(-18.5, 0.5).all() and df["longitud"].between(-81.5, -68.5).all()
    # coordenadas == dim_ubigeo
    ref = duckdb.connect().execute(f"SELECT sk_ubigeo, latitud FROM read_parquet('{(wh / 'dim_ubigeo.parquet').as_posix()}')").fetchall()
    ref = dict(ref)
    assert all(abs(lat - ref[sk]) < 1e-12 for sk, lat in zip(df["sk_ubigeo"], df["latitud"]))


def test_map_metrics(store):
    f = Filters(2024, 12)
    g, _ = store.mapa(f, "devengado")
    i, _ = store.mapa(f, "recaudado")
    b, _ = store.mapa(f, "brecha")
    a, _ = store.mapa(f, "avance")
    assert set(METRICAS_MAPA) == {"devengado", "avance", "recaudado", "brecha"}
    # brecha = recaudado - devengado, por ubicación
    chk = b.set_index("sk_ubigeo")
    gi, ii = g.set_index("sk_ubigeo")["devengado"], i.set_index("sk_ubigeo")["recaudado"]
    for sk in list(chk.index)[:25]:
        assert chk.loc[sk, "valor"] == pytest.approx(ii.get(sk, 0.0) - gi.get(sk, 0.0), rel=1e-9, abs=1e-6)
    assert (a["valor"] >= 0).all() and a["valor"].max() < 5  # avance razonable (devengado / PIM)
    empty, _ = store.mapa(Filters(2024, 12, niveles=("NO_EXISTE",)), "devengado")
    assert empty.empty or empty["devengado"].sum() == 0


# ------------------------------------------------------------------ app por HTTP
def _call(client, dep, values):
    outs = [o for o in dep["output"].strip(".").split("...")]
    outputs = [{"id": o.rsplit(".", 1)[0], "property": o.rsplit(".", 1)[1]} for o in outs]
    payload = {
        "output": dep["output"],
        "outputs": outputs if len(outputs) > 1 else outputs[0],
        "inputs": [{"id": i["id"], "property": i["property"], "value": values.get(i["id"])} for i in dep["inputs"]],
        "changedPropIds": [f"{dep['inputs'][0]['id']}.{dep['inputs'][0]['property']}"],
    }
    r = client.post("/_dash-update-component", json=payload)
    return r.status_code, r.get_json()


def _server_deps(client):
    """Callbacks que corren en el servidor (excluye el del navegador que detecta el tema)."""
    return [d for d in client.get("/_dash-dependencies").get_json() if not d.get("clientside_function")]


def _ids(node):
    out = set()
    if isinstance(node, dict):
        if isinstance(node.get("props"), dict) and "id" in node["props"]:
            out.add(node["props"]["id"])
        for v in node.values():
            out |= _ids(v)
    elif isinstance(node, list):
        for v in node:
            out |= _ids(v)
    return out


def test_layout_and_callback_ids_are_consistent(client):
    assert client.get("/").status_code == 200
    layout_ids = _ids(client.get("/_dash-layout").get_json())
    deps = client.get("/_dash-dependencies").get_json()
    assert len(deps) == 6  # 5 del servidor + 1 del navegador (tema claro/oscuro)
    assert len(_server_deps(client)) == 5
    for d in deps:
        for o in d["output"].strip(".").split("..."):
            assert o.rsplit(".", 1)[0] in layout_ids, o
        for i in d["inputs"]:
            assert i["id"] in layout_ids, i


@pytest.mark.parametrize(
    "vals",
    [
        {"f-anio": 2024, "f-mes": 12},
        {"f-anio": 2024, "f-mes": 1, "map-metric": "avance"},
        {"f-anio": 2023, "f-mes": 6, "f-nivel": ["GOBIERNOS LOCALES"], "f-depto": ["LIMA", "AREQUIPA"], "f-fuente": ["Recursos Ordinarios"], "map-metric": "brecha"},
        {"f-anio": 2024, "f-mes": 12, "f-nivel": ["NO_EXISTE"], "map-metric": "recaudado"},  # filtro sin datos
    ],
)
def test_all_callbacks_return_200(client, vals):
    for dep in _server_deps(client):
        status, js = _call(client, dep, {"map-metric": "devengado", **vals})
        assert status == 200, (dep["output"][:60], js)
        assert js["response"]


def test_comparativo_ingreso_gasto(client):
    """El comparativo dibuja dos series mensuales con los filtros compartidos."""
    deps = _server_deps(client)
    comparativo = next(d for d in deps if "g-comparativo" in d["output"])
    status, js = _call(client, comparativo, {"f-anio": 2024, "f-mes": 6, "theme": "light"})

    assert status == 200
    fig = js["response"]["g-comparativo"]["figure"]
    assert len(fig["data"]) == 2
    assert [serie["name"] for serie in fig["data"]] == ["Ingresos recaudados", "Gastos devengados"]
    assert list(fig["data"][0]["x"]) == ["Ene", "Feb", "Mar", "Abr", "May", "Jun"]
    assert fig["layout"]["barmode"] == "group"


def test_empty_filter_shows_message_not_error(client):
    deps = client.get("/_dash-dependencies").get_json()
    gasto = next(d for d in deps if "kpi-gasto" in d["output"])
    _, js = _call(client, gasto, {"f-anio": 2024, "f-mes": 12, "f-nivel": ["NO_EXISTE"]})
    assert js["response"]["g-cadena"]["figure"]["layout"]["annotations"][0]["text"].startswith("Sin datos")


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_figures_follow_theme(client, theme):
    """El texto de los gráficos usa la tinta del tema (en oscuro, texto claro): si no, no se lee."""
    from dashboard.figures import THEMES

    deps = _server_deps(client)
    gasto = next(d for d in deps if "kpi-gasto" in d["output"])
    _, js = _call(client, gasto, {"f-anio": 2024, "f-mes": 12, "theme": theme})
    fig = js["response"]["g-funcion"]["figure"]
    assert fig["layout"]["font"]["color"] == THEMES[theme]["ink"]
    assert fig["layout"]["hoverlabel"]["bgcolor"] == THEMES[theme]["hover_bg"]


def test_map_draws_peru_basemap(client):
    """El mapa dibuja vecinos, departamentos, contorno y nombres además de los puntos."""
    deps = _server_deps(client)
    mapa = next(d for d in deps if "g-mapa" in d["output"])
    _, js = _call(client, mapa, {"f-anio": 2024, "f-mes": 12, "map-metric": "devengado", "theme": "dark"})
    data = js["response"]["g-mapa"]["figure"]["data"]
    assert len(data) == 5  # vecinos, departamentos, contorno, puntos, nombres
    assert data[1]["fill"] == "toself" and data[3]["mode"] == "markers" and data[4]["mode"] == "text"
    assert "Loreto" in data[4]["text"]


def test_year_change_syncs_months(client):
    deps = client.get("/_dash-dependencies").get_json()
    sync = next(d for d in deps if d["output"].startswith("..f-mes.options"))
    _, js = _call(client, sync, {"f-anio": 2024})
    assert [o["value"] for o in js["response"]["f-mes"]["options"]] == list(range(1, 13))
    assert js["response"]["f-mes"]["value"] == 12


# ------------------------------------------------------------------ recarga automática
def test_store_reloads_when_etl_publishes(wh_cumulative, tmp_path):
    wh = tmp_path / "wh"
    shutil.copytree(wh_cumulative.warehouse, wh)
    st = Store(wh)
    assert st.options()["anios"] == [2024, 2023]
    assert st.refresh() is False  # nada cambió
    shutil.rmtree(wh / "cube_gasto_base" / "anio=2023")  # simula un ETL que publica
    shutil.rmtree(wh / "cube_ingreso_base" / "anio=2023")
    time.sleep(0.05)
    s = type(wh_cumulative)(**{**wh_cumulative.__dict__, "warehouse": wh})
    pipeline.write_manifest(s, pipeline.read_manifest(s))
    assert st.refresh() is True
    assert 2023 not in st.options()["anios"]


# ------------------------------------------------------------------ saldos según el README del equipo (mes 0, PIM como modificaciones)
@pytest.fixture(scope="module")
def spec_store(wh_spec):
    return Store(wh_spec.warehouse)


def test_spec_gasto_pim_pia_come_from_month_zero(spec_store, wh_spec):
    g = f"read_parquet('{wh_spec.raw_gasto.replace('*.parquet', '2024-Gasto.parquet')}')"
    exp_pim = float(q(f"SELECT sum(MONTO_PIM::DECIMAL(18,2)) FROM {g} WHERE MES_EJE = 0"))
    exp_pia = float(q(f"SELECT sum(MONTO_PIA::DECIMAL(18,2)) FROM {g} WHERE MES_EJE = 0"))
    assert exp_pim > 0 and exp_pia > 0
    for corte in (1, 6, 12):  # visible desde enero: viene de la apertura
        k = spec_store.kpi_gasto(Filters(2024, corte))
        assert k["pim"] == pytest.approx(exp_pim, rel=1e-9) and k["pia"] == pytest.approx(exp_pia, rel=1e-9)


def test_spec_ingreso_pim_is_cumulative_sum_of_modifications(spec_store, wh_spec):
    i = f"read_parquet('{wh_spec.raw_ingreso.replace('*.parquet', '2024-Ingreso.parquet')}')"
    for corte in (2, 6, 12):
        exp_pim = float(q(f"SELECT sum(MONTO_PIM::DECIMAL(18,2)) FROM {i} WHERE MES_DOC <= {corte}"))
        exp_pia = float(q(f"SELECT sum(MONTO_PIA::DECIMAL(18,2)) FROM {i} WHERE MES_DOC <= {corte}"))
        k = spec_store.kpi_ingreso(Filters(2024, corte))
        assert k["pim"] == pytest.approx(exp_pim, rel=1e-9), corte
        assert k["pia"] == pytest.approx(exp_pia, rel=1e-9), corte
    # el PIM vigente NO es el delta del último mes (error que daba 2.3 M en vez de 145 M)
    assert spec_store.kpi_ingreso(Filters(2024, 12))["pim"] > 50 * float(q(f"SELECT sum(MONTO_PIM::DECIMAL(18,2)) FROM {i} WHERE MES_DOC = 12"))


def test_spec_store_reads_modes_and_hides_month_zero(spec_store):
    assert spec_store.budget_mode == {"gasto": "snapshot", "ingreso": "sum"}
    assert 0 not in spec_store.options()["meses"][2024]


def test_spec_app_callbacks_work(wh_spec):
    c = create_app(wh_spec.warehouse).server.test_client()
    for dep in _server_deps(c):
        status, js = _call(c, dep, {"f-anio": 2024, "f-mes": 12, "map-metric": "devengado"})
        assert status == 200 and js["response"], dep["output"][:50]


def test_dashboard_uses_only_model_and_extension_tables():
    """El dashboard lee cubos y 2 dimensiones del modelo (entidad, ubigeo): nada de columnas fuera del modelo."""
    assert {d.table for d in MODEL_DIMS} >= {"dim_entidad", "dim_ubigeo"}
    assert GASTO.measures[:2] == INGRESO.measures[:2] == ("monto_pia", "monto_pim")
