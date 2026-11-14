"""Dashboard de Presupuesto Público (Dash).

    python -m dashboard.app                      # desarrollo  -> http://127.0.0.1:8050
    gunicorn dashboard.app:server -w 2 --threads 4 -b 0.0.0.0:8050   # producción

Solo consulta los cubos del warehouse (ver dashboard/data.py), por eso responde en ms.
Se recarga sola cuando el ETL publica datos nuevos (cambia manifest.json).
`create_app(warehouse)` permite apuntar a otro warehouse (útil en tests).
"""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

from dash import Dash, Input, Output, dcc, html

from config import get_settings
from dashboard import figures as F
from dashboard.data import MESES, METRICAS_MAPA, Filters, Store

log = logging.getLogger("dashboard")

FONTS = (
    "https://fonts.googleapis.com/css2?family=Public+Sans:wght@400;500;600&"
    "family=Source+Serif+4:opsz,wght@8..60,500;8..60,600&display=swap"
)
GRAPH_CFG = {"displayModeBar": False, "responsive": True}


# ===================================================================== helpers
def _field(label: str, control) -> html.Div:
    return html.Div(className="field", children=[html.Label(label), control])


def _panel(title: str, graph_id: str, note: str = "", height: int = 300, extra=None) -> html.Section:
    kids = [html.H3(title)]
    if note:
        kids.append(html.P(note, className="panel-note"))
    if extra is not None:
        kids.append(extra)
    # Altura EXPLÍCITA en px (= la de la figura). Sin ella, un dcc.Graph "responsive" dentro de
    # un contenedor de altura automática colapsa a ~0 px (verificado con captura real).
    kids.append(dcc.Graph(id=graph_id, config=GRAPH_CFG, style={"height": f"{height}px"}))
    return html.Section(className="panel", children=kids)


def _filters(anio, mes, niveles, deptos, fuentes) -> Filters | None:
    if anio is None or mes is None:
        return None
    # sorted(): mismo conjunto de filtros => misma clave de caché, sin importar el orden de selección
    return Filters(int(anio), int(mes), tuple(sorted(niveles or ())), tuple(sorted(deptos or ())), tuple(sorted(fuentes or ())))


def kpi(label: str, num: str, unit: str = "", sub: str = "", tone: str = "") -> html.Div:
    return html.Div(className=f"kpi {tone}".strip(), children=[
        html.Div(label, className="kpi-label"),
        html.Div([html.Span(num, className="kpi-num"), html.Span(unit, className="kpi-unit")], className="kpi-value"),
        html.Div(sub, className="kpi-sub"),
    ])  # fmt: skip


def _delta(curr: float, prev: float, anio: int) -> str:
    if not prev:
        return f"sin dato de {anio - 1}"
    d = curr / prev - 1
    return f"{'▲' if d >= 0 else '▼'} {abs(d) * 100:.1f}% vs. mismo corte de {anio - 1}"


def _labels(series, n: int = 34) -> list[str]:
    return [F.short(v, n) for v in series]


# ======================================================================= fábrica
def create_app(warehouse: Path | str | None = None) -> Dash:
    wh = Path(warehouse) if warehouse else get_settings().warehouse
    store = Store(wh)
    app = Dash(__name__, title="Presupuesto público · Perú", update_title=None, external_stylesheets=[FONTS])
    app.store = store  # acceso para tests/depuración

    def built_at() -> str:
        try:
            return json.loads((wh / "manifest.json").read_text(encoding="utf-8")).get("built_at", "")[:16].replace("T", " ")
        except Exception:  # noqa: BLE001
            return ""

    # ------------------------------------------------------------------ layout
    def serve_layout():
        store.refresh()
        if not store.ready:
            return html.Div(className="empty", children=[
                html.H2("Todavía no hay datos"),
                html.P("Construye el datamart y recarga la página:"),
                html.Pre("python -m etl.profile   # revisa tus parquet\npython -m etl.run       # construye el warehouse"),
            ])  # fmt: skip
        o = store.options()
        anio = o["anios"][0]
        mes = o["meses"][anio][-1]
        return html.Div(className="shell", children=[
            html.Aside(className="rail", children=[
                html.Div(className="brand", children=[
                    html.H1("Presupuesto público"),
                    html.P("Perú · gasto e ingreso del Estado"),
                ]),
                _field("Año", dcc.Dropdown(id="f-anio", options=o["anios"], value=anio, clearable=False)),
                _field("Mes de corte", dcc.Dropdown(id="f-mes", options=[{"label": MESES[m - 1], "value": m} for m in o["meses"][anio]], value=mes, clearable=False)),
                _field("Nivel de gobierno", dcc.Dropdown(id="f-nivel", options=o["niveles"], multi=True, placeholder="Todos")),
                _field("Departamento", dcc.Dropdown(id="f-depto", options=o["deptos"], multi=True, placeholder="Todos")),
                _field("Fuente de financiamiento", dcc.Dropdown(id="f-fuente", options=o["fuentes"], multi=True, placeholder="Todas")),
                html.P("El departamento es el de la entidad ejecutora, igual en gasto e ingreso: así se pueden "
                       "comparar. El gasto de entidades nacionales se ubica donde está su sede (casi siempre Lima).",
                       className="rail-note"),  # fmt: skip
                html.P(f"Datos procesados: {built_at()}", className="rail-note"),
            ]),
            html.Main(className="main", children=[
                html.Header(className="section-head", children=[html.H2("Gasto"), html.P(id="sub-gasto")]),
                html.Div(id="kpi-gasto", className="ledger"),
                html.Div(className="grid g2", children=[
                    _panel("Del presupuesto al pago", "g-cadena", "Apertura (PIA), presupuesto vigente (PIM), devengado y girado.", 250),
                    _panel("Avance mensual", "g-mensual-gasto", "Devengado de cada mes y avance acumulado.", 300),
                ]),
                html.Div(className="grid g2", children=[
                    _panel("Por función", "g-funcion", "Las 10 funciones con más devengado.", 340),
                    _panel("Por producto o proyecto", "g-proyecto", "Los 10 con más devengado.", 340),
                ]),
                html.Div(className="grid g2", children=[
                    _panel("Entidades con más devengado", "g-entidad", "Las 12 principales unidades ejecutoras. A la derecha, el avance sobre su PIM.", 380),
                    _panel("Avance por departamento", "g-depto", "Devengado / PIM según la ubicación de la entidad ejecutora.", 520),
                ]),
                _panel("Por genérica de gasto", "g-generica-gasto", "Presupuesto vigente (PIM) frente a lo devengado.", 330),

                html.Header(className="section-head ingreso", children=[html.H2("Ingreso"), html.P(id="sub-ingreso")]),
                html.Div(id="kpi-ingreso", className="ledger ingreso"),
                html.Div(className="grid g2", children=[
                    _panel("Recaudación mensual", "g-mensual-ingreso", "Lo recaudado en cada mes y avance acumulado.", 300),
                    _panel("Por genérica de ingreso", "g-generica-ingreso", "Presupuesto vigente (PIM) frente a lo recaudado.", 330),
                ]),
                html.P(id="nota-cuadre", className="note"),

                html.Header(className="section-head mapa", children=[html.H2("Mapa"), html.P("Dónde se ubican las entidades que gastan y recaudan")]),
                _panel(
                    "Distritos", "g-mapa",
                    "Cada punto es un distrito; el tamaño es el monto y el color, la métrica elegida.", 640,
                    extra=dcc.RadioItems(id="map-metric", className="radio", inline=True, value="devengado",
                                         options=[{"label": v, "value": k} for k, v in METRICAS_MAPA.items()]),
                ),
                html.P(id="nota-mapa", className="note"),
                html.Footer(className="foot", children=[
                    html.P("Fuente: Ministerio de Economía y Finanzas, Consulta Amigable (SIAF). Montos en soles. Coordenadas: tabla de ubigeos."),
                    html.P("PIA: presupuesto institucional de apertura. PIM: presupuesto institucional modificado (vigente). "
                           "Devengado: obligación reconocida (el “gasto ejecutado”). Girado: orden de pago emitida."),
                ]),
            ]),
        ])  # fmt: skip

    app.layout = serve_layout  # función: cada carga de página relee las opciones

    # --------------------------------------------------------------- callbacks
    common_inputs = (Input("f-anio", "value"), Input("f-mes", "value"), Input("f-nivel", "value"),
                     Input("f-depto", "value"), Input("f-fuente", "value"))  # fmt: skip

    @app.callback(Output("f-mes", "options"), Output("f-mes", "value"), Input("f-anio", "value"))
    def sync_meses(anio):
        meses = store.options()["meses"].get(int(anio), []) if anio else []
        return [{"label": MESES[m - 1], "value": m} for m in meses], (meses[-1] if meses else None)

    @app.callback(
        Output("sub-gasto", "children"), Output("kpi-gasto", "children"),
        Output("g-cadena", "figure"), Output("g-mensual-gasto", "figure"),
        Output("g-funcion", "figure"), Output("g-proyecto", "figure"),
        Output("g-entidad", "figure"), Output("g-depto", "figure"), Output("g-generica-gasto", "figure"),
        *common_inputs,
    )  # fmt: skip
    def update_gasto(anio, mes, niveles, deptos, fuentes):
        f = _filters(anio, mes, niveles, deptos, fuentes)
        if f is None or not store.ready:
            e = F.empty_fig()
            return "", [], e, e, e, e, e, e, e
        store.refresh()
        return _gasto(store.version, f)

    @lru_cache(maxsize=256)
    def _gasto(_version, f: Filters):
        """Todo el resultado del callback, memoizado por (versión de datos, filtros).
        La versión cambia sola cuando el ETL publica, así que el caché nunca queda viejo."""
        k = store.kpi_gasto(f)
        pim_n, pim_u = F.soles_parts(k["pim"])
        dev_n, dev_u = F.soles_parts(k["devengado"])
        gir_n, gir_u = F.soles_parts(k["girado"])
        kpis = [
            kpi("Presupuesto vigente (PIM)", pim_n, pim_u, f"PIA inicial: {F.soles(k['pia'])}"),
            kpi("Devengado", dev_n, dev_u, _delta(k["devengado"], k["devengado_prev"], f.anio), "hero"),
            kpi("Avance de ejecución", F.pct_txt(F.pct(k["devengado"], k["pim"])).rstrip("%"), "% del PIM", "Devengado ÷ PIM"),
            kpi("Girado", gir_n, gir_u, f"{F.pct_txt(F.pct(k['girado'], k['devengado']), 0)} de lo devengado"),
        ]
        serie = store.serie("cube_gasto_base", f, ("devengado",))
        func = store.desglose_gasto("cube_gasto_funcion", "funcion", f, limit=10)
        if len(func):
            func["etiqueta"] = _labels(func["funcion"])
        proy = store.desglose_gasto("cube_gasto_proyecto", "proyecto", f, limit=10)
        if len(proy):
            proy["etiqueta"] = _labels(proy["proyecto"], 40)
        ent = store.entidades(f, 12)
        if len(ent):
            ent["etiqueta"] = _labels(ent["etiqueta"], 38)
        dep = store.desglose_gasto("cube_gasto_base", "departamento", f, exclude=("depto",))
        if len(dep):
            dep["etiqueta"] = _labels(dep["departamento"])
        gen = store.desglose_gasto("cube_gasto_generica", "generica", f)
        if len(gen):
            gen["etiqueta"] = _labels(gen["generica"])
        return (
            f"Ejecución del presupuesto hasta {MESES[f.mes - 1].lower()} de {f.anio}",
            kpis,
            F.fig_cadena(k),
            F.fig_mensual(serie, "devengado", k["pim"], F.GASTO, F.GASTO_L, "Devengado"),
            F.fig_ranking(func, "etiqueta", "devengado", "pim", F.GASTO, F.GASTO_L),
            F.fig_ranking(proy, "etiqueta", "devengado", "pim", F.GASTO, F.GASTO_L),
            F.fig_ranking(ent, "etiqueta", "devengado", "pim", F.GASTO, F.GASTO_L, height=380),
            F.fig_avance_depto(dep, "etiqueta", f.deptos, codes="departamento"),
            F.fig_pim_vs_ejec(gen, "etiqueta", "devengado", "Devengado", F.GASTO, F.GASTO_L),
        )

    @app.callback(
        Output("sub-ingreso", "children"), Output("kpi-ingreso", "children"),
        Output("g-mensual-ingreso", "figure"), Output("g-generica-ingreso", "figure"), Output("nota-cuadre", "children"),
        *common_inputs,
    )  # fmt: skip
    def update_ingreso(anio, mes, niveles, deptos, fuentes):
        f = _filters(anio, mes, niveles, deptos, fuentes)
        if f is None or not store.ready:
            e = F.empty_fig()
            return "", [], e, e, ""
        store.refresh()
        return _ingreso(store.version, f)

    @lru_cache(maxsize=256)
    def _ingreso(_version, f: Filters):
        k = store.kpi_ingreso(f)
        if not k:
            e = F.empty_fig()
            return "", [], e, e, ""
        pim_n, pim_u = F.soles_parts(k["pim"])
        rec_n, rec_u = F.soles_parts(k["recaudado"])
        kpis = [
            kpi("Presupuesto de ingresos (PIM)", pim_n, pim_u, f"PIA inicial: {F.soles(k['pia'])}"),
            kpi("Recaudado", rec_n, rec_u, _delta(k["recaudado"], k["recaudado_prev"], f.anio), "hero"),
            kpi("Avance de recaudación", F.pct_txt(F.pct(k["recaudado"], k["pim"])).rstrip("%"), "% del PIM", "Recaudado ÷ PIM"),
        ]
        serie = store.serie("cube_ingreso_base", f, ("recaudado",))
        gen = store.desglose_ingreso("generica", f)
        if len(gen):
            gen["etiqueta"] = _labels(gen["generica"])
        pg, pi = store.pim_total("cube_gasto_base", f), store.pim_total("cube_ingreso_base", f)
        nota = ""
        if pg and pi:
            nota = (f"Control de cuadre: el presupuesto debe estar equilibrado. PIM de ingresos {F.soles(pi)} frente a "
                    f"PIM de gastos {F.soles(pg)} (diferencia {F.pct_txt(abs(pi - pg) / pg)}).")  # fmt: skip
        return (
            f"Recaudación hasta {MESES[f.mes - 1].lower()} de {f.anio}",
            kpis,
            F.fig_mensual(serie, "recaudado", k["pim"], F.INGRESO, F.INGRESO_L, "Recaudado"),
            F.fig_pim_vs_ejec(gen, "etiqueta", "recaudado", "Recaudado", F.INGRESO, F.INGRESO_L),
            nota,
        )

    @app.callback(Output("g-mapa", "figure"), Output("nota-mapa", "children"), *common_inputs, Input("map-metric", "value"))
    def update_mapa(anio, mes, niveles, deptos, fuentes, metrica):
        f = _filters(anio, mes, niveles, deptos, fuentes)
        if f is None or not store.ready or metrica not in METRICAS_MAPA:
            return F.empty_fig("Sin datos", 640), ""
        store.refresh()
        return _mapa(store.version, f, metrica)

    @lru_cache(maxsize=128)
    def _mapa(_version, f: Filters, metrica: str):
        df, meta = store.mapa(f, metrica)
        nota = ""
        if meta and meta["sin_coordenadas"]:
            share = meta["monto_sin_coordenadas"] / meta["total"] if meta["total"] else 0
            n = meta["sin_coordenadas"]
            quien = "1 ubicación no tiene coordenadas" if n == 1 else f"{n} ubicaciones no tienen coordenadas"
            nota = (f"{quien} en la tabla de ubigeos y no se {'dibuja' if n == 1 else 'dibujan'} "
                    f"({share * 100:.1f}% del monto). Sus montos sí están en todos los demás gráficos.")  # fmt: skip
        return F.fig_mapa(df, metrica), nota

    return app


# Instancia por defecto (warehouse de la configuración): `gunicorn dashboard.app:server`
app = create_app()
server = app.server

if __name__ == "__main__":
    import os

    logging.basicConfig(level=logging.INFO)
    app.run(
        debug=os.environ.get("DASH_DEBUG", "0") == "1",
        host=os.environ.get("DASH_HOST", "127.0.0.1"),
        port=int(os.environ.get("DASH_PORT", "8050")),
    )
