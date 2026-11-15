"""Gráficos (Plotly) y formato de cifras. Sin acceso a datos: reciben DataFrames."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go

from dashboard.data import MESES

# Paletas. Deben coincidir con las variables de dashboard/assets/style.css.
THEMES = {
    "light": dict(
        ink="#16212B", muted="#5B6874", rule="#D3D9DE",
        gasto="#1F5F74", gasto_l="#A9C6D0", ingreso="#B5761B", ingreso_l="#E6CC9B", alert="#A63A2B",
        hover_bg="#FFFFFF", hover_border="#D3D9DE",
        cadena=("#B9C3CC", "#8794A1", "#1F5F74", "#143F4E"),
        # mapa
        land="#E8ECEF", vecino="#F4F6F7", border_dep="#B4BEC7", border_pais="#6F7C88", label="#3E4A55",
        scale_low="#E3EAED", div_mid="#EEF1F3", marker_line="rgba(22,33,43,0.45)",
    ),
    "dark": dict(
        ink="#E6EBEF", muted="#B3BFC9", rule="#2E3B46",
        gasto="#6FB3C7", gasto_l="#2F6070", ingreso="#E0A755", ingreso_l="#7D5B2B", alert="#E07A6A",
        hover_bg="#1F2A35", hover_border="#3A4A57",
        cadena=("#4A5763", "#6E7D8A", "#6FB3C7", "#A9D6E3"),
        land="#1E2A34", vecino="#161F27", border_dep="#3E4F5D", border_pais="#7E909E", label="#C9D3DB",
        scale_low="#4A5C6B", div_mid="#4A5C6B", marker_line="rgba(230,235,239,0.30)",
    ),
}


def palette(theme: str | None) -> dict:
    return THEMES.get(theme or "light", THEMES["light"])


# Compatibilidad: constantes del tema claro (las usan tests y código existente).
_L = THEMES["light"]
INK, MUTED, RULE = _L["ink"], _L["muted"], _L["rule"]
GASTO, GASTO_L, INGRESO, INGRESO_L, ALERT = _L["gasto"], _L["gasto_l"], _L["ingreso"], _L["ingreso_l"], _L["alert"]
FONT = '"Public Sans", system-ui, -apple-system, "Segoe UI", Roboto, sans-serif'

GEO_FILE = Path(__file__).resolve().parent.parent / "data" / "ref" / "peru_mapa.geojson"


# ------------------------------------------------------------------ formato
def soles(x: float | None) -> str:
    """Cifra compacta en soles: 'S/ 12.34 mil M', 'S/ 5.6 M', 'S/ 800 mil'."""
    if x is None or pd.isna(x):
        return "—"
    a = abs(x)
    if a >= 1e9:
        return f"S/ {x / 1e9:,.2f} mil M"
    if a >= 1e6:
        return f"S/ {x / 1e6:,.1f} M"
    if a >= 1e3:
        return f"S/ {x / 1e3:,.0f} mil"
    return f"S/ {x:,.0f}"


def soles_parts(x: float | None) -> tuple[str, str]:
    """(número, unidad) para mostrar el número grande y la unidad pequeña."""
    if x is None or pd.isna(x):
        return "—", ""
    a = abs(x)
    if a >= 1e9:
        return f"{x / 1e9:,.2f}", "mil millones de soles"
    if a >= 1e6:
        return f"{x / 1e6:,.1f}", "millones de soles"
    if a >= 1e3:
        return f"{x / 1e3:,.0f}", "miles de soles"
    return f"{x:,.0f}", "soles"


def pct(num: float, den: float) -> float | None:
    return num / den if den else None


def pct_txt(p: float | None, digits: int = 1) -> str:
    return "—" if p is None else f"{p * 100:,.{digits}f}%"


def short(text: str, n: int = 34) -> str:
    text = str(text).strip().title() if str(text).isupper() else str(text).strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


# ------------------------------------------------------------------ base
def _layout(height: int = 300, **kw) -> dict:
    base = dict(
        height=height,
        margin=dict(l=4, r=12, t=6, b=4),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family=FONT, size=12, color=INK),
        hoverlabel=dict(font=dict(family=FONT, size=12), bgcolor="white", bordercolor=RULE),
        separators=".,",  # decimales con punto, miles con coma
        showlegend=False,
    )
    base.update(kw)
    return base


def _themed(fig: go.Figure, theme: str | None) -> go.Figure:
    """Aplica la paleta del tema a todo el texto, ejes, leyenda y tooltips de la figura."""
    t = palette(theme)
    fig.update_layout(
        font_color=t["ink"],
        legend_font_color=t["ink"],
        hoverlabel=dict(bgcolor=t["hover_bg"], bordercolor=t["hover_border"], font=dict(family=FONT, size=12, color=t["ink"])),
    )
    axis = dict(tickfont_color=t["ink"], title_font_color=t["muted"], gridcolor=t["rule"], zerolinecolor=t["rule"], linecolor=t["rule"])
    fig.update_xaxes(**axis)
    fig.update_yaxes(**axis)
    for a in fig.layout.annotations or ():
        a.font.color = t["muted"]
    return fig


def empty_fig(msg: str = "Sin datos para este filtro", height: int = 300, theme: str = "light") -> go.Figure:
    fig = go.Figure()
    fig.update_layout(
        **_layout(height),
        xaxis=dict(visible=False),
        yaxis=dict(visible=False),
        annotations=[dict(text=msg, showarrow=False, font=dict(size=14, color=MUTED))],
    )
    return _themed(fig, theme)


# ------------------------------------------------------------------ gráficos
def fig_cadena(k: dict, theme: str = "light") -> go.Figure:
    """Del presupuesto al pago: PIA -> PIM -> devengado -> girado (las 4 medidas del modelo)."""
    pim = k.get("pim", 0)
    if not pim:
        return empty_fig(height=250, theme=theme)
    c = palette(theme)["cadena"]
    steps = [
        ("Apertura (PIA)", k["pia"], c[0]),
        ("Vigente (PIM)", pim, c[1]),
        ("Devengado", k["devengado"], c[2]),
        ("Girado", k["girado"], c[3]),
    ]
    fig = go.Figure(
        go.Bar(
            y=[s[0] for s in steps],
            x=[s[1] for s in steps],
            orientation="h",
            marker_color=[s[2] for s in steps],
            text=[f"{soles(s[1])}  ·  {pct_txt(pct(s[1], pim), 0)} del PIM" for s in steps],
            textposition="outside",
            cliponaxis=False,
            hovertemplate="%{y}: %{text}<extra></extra>",
        )
    )
    fig.update_layout(
        **_layout(250, margin=dict(l=4, r=200, t=10, b=4)),
        xaxis=dict(visible=False, range=[0, max(pim, k["pia"], 1) * 1.02]),
        yaxis=dict(autorange="reversed", tickfont=dict(size=13), showgrid=False),
        bargap=0.3,
    )
    return _themed(fig, theme)


def fig_mensual(df: pd.DataFrame, col: str, pim: float, color: str, light: str, nombre: str, theme: str = "light") -> go.Figure:
    """Barras = flujo del mes; línea = avance acumulado como % del PIM vigente."""
    if df.empty or not df[col].any():
        return empty_fig(theme=theme)
    cum = df[col].cumsum()
    x = [MESES[m - 1] for m in df["mes"]]
    fig = go.Figure()
    fig.add_bar(x=x, y=df[col] / 1e6, name=f"{nombre} del mes", marker_color=light,
                hovertemplate="%{x}: S/ %{y:,.1f} M<extra></extra>")  # fmt: skip
    if pim:
        fig.add_scatter(x=x, y=cum / pim * 100, name="Avance acumulado", yaxis="y2", mode="lines+markers",
                        line=dict(color=color, width=2.5), marker=dict(size=6),
                        hovertemplate="Acumulado: %{y:.1f}% del PIM<extra></extra>")  # fmt: skip
    fig.update_layout(
        **_layout(300, showlegend=True, legend=dict(orientation="h", y=1.12, x=0, font=dict(size=12))),
        yaxis=dict(title=dict(text="S/ millones", font=dict(size=11)), zeroline=False),
        yaxis2=dict(overlaying="y", side="right", ticksuffix="%", showgrid=False, rangemode="tozero",
                    tickmode="auto", nticks=6,  # Plotly 7 sincroniza ticks de ejes superpuestos por defecto
                    title=dict(text="% del PIM", font=dict(size=11))),
        xaxis=dict(showgrid=False), bargap=0.25,
    )  # fmt: skip
    return _themed(fig, theme)


def fig_comparativo(ingreso: pd.DataFrame, gasto: pd.DataFrame, theme: str = "light") -> go.Figure:
    """Barras agrupadas: flujo mensual recaudado frente a flujo mensual devengado."""
    if ingreso.empty or gasto.empty:
        return empty_fig("Sin datos para comparar ingresos y gastos", height=360, theme=theme)

    df = ingreso[["mes", "recaudado"]].merge(
        gasto[["mes", "devengado"]],
        on="mes",
        how="outer",
    ).fillna(0).sort_values("mes")

    if not df["recaudado"].any() and not df["devengado"].any():
        return empty_fig("Sin datos para comparar ingresos y gastos", height=360, theme=theme)

    t = palette(theme)
    x = [MESES[int(m) - 1] for m in df["mes"]]
    fig = go.Figure()
    fig.add_bar(
        x=x,
        y=df["recaudado"] / 1e6,
        name="Ingresos recaudados",
        marker_color=t["ingreso"],
        hovertemplate="%{x}<br>Ingresos recaudados: S/ %{y:,.1f} M<extra></extra>",
    )
    fig.add_bar(
        x=x,
        y=df["devengado"] / 1e6,
        name="Gastos devengados",
        marker_color=t["gasto"],
        hovertemplate="%{x}<br>Gastos devengados: S/ %{y:,.1f} M<extra></extra>",
    )
    fig.update_layout(
        **_layout(360, showlegend=True, legend=dict(orientation="h", y=1.12, x=0, font=dict(size=12))),
        barmode="group",
        yaxis=dict(title=dict(text="S/ millones", font=dict(size=11)), rangemode="tozero", zeroline=False),
        xaxis=dict(showgrid=False),
        bargap=0.25,
    )
    return _themed(fig, theme)


def fig_ranking(df: pd.DataFrame, label: str, value: str, pim: str | None, color: str, light: str,
                height: int = 340, theme: str = "light") -> go.Figure:
    """Barras horizontales (mayor arriba) con valor y, si hay PIM, % de avance."""
    if df.empty or not df[value].any():
        return empty_fig(height=height, theme=theme)
    d = df.sort_values(value, ascending=True)
    texts = [
        f"{soles(v)}" + (f"  ·  {pct_txt(pct(v, p), 0)}" if pim and p else "")
        for v, p in zip(d[value], d[pim] if pim else [None] * len(d))
    ]
    fig = go.Figure(go.Bar(y=d[label], x=d[value], orientation="h", marker_color=color, text=texts,
                           textposition="outside", cliponaxis=False,
                           hovertemplate="%{y}<br>%{text}<extra></extra>"))  # fmt: skip
    fig.update_layout(**_layout(height, margin=dict(l=4, r=130, t=6, b=4)), xaxis=dict(visible=False),
                      yaxis=dict(tickfont=dict(size=12), showgrid=False), bargap=0.3)  # fmt: skip
    return _themed(fig, theme)


def fig_avance_depto(df: pd.DataFrame, label: str, selected: tuple[str, ...] = (), codes: str = "departamento",
                     theme: str = "light") -> go.Figure:
    """% de avance (devengado/PIM) por departamento, ordenado."""
    if df.empty:
        return empty_fig(height=520, theme=theme)
    d = df[df["pim"] > 0].copy()
    if d.empty:
        return empty_fig(height=520, theme=theme)
    t = palette(theme)
    d["avance"] = d["devengado"] / d["pim"]
    d = d.sort_values("avance", ascending=True)
    sel = set(selected)
    colors = [t["gasto"] if (not sel or c in sel) else t["gasto_l"] for c in d[codes]]
    fig = go.Figure(go.Bar(
        y=d[label], x=d["avance"] * 100, orientation="h", marker_color=colors,
        text=[f"{a * 100:.1f}%" for a in d["avance"]], textposition="outside", cliponaxis=False,
        customdata=list(zip(d["devengado"].map(soles), d["pim"].map(soles))),
        hovertemplate="%{y}<br>Avance: %{x:.1f}%<br>Devengado: %{customdata[0]}<br>PIM: %{customdata[1]}<extra></extra>",
    ))  # fmt: skip
    fig.update_layout(**_layout(520, margin=dict(l=4, r=48, t=6, b=4)), xaxis=dict(visible=False, rangemode="tozero"),
                      yaxis=dict(tickfont=dict(size=12), showgrid=False), bargap=0.25)  # fmt: skip
    return _themed(fig, theme)


def fig_pim_vs_ejec(df: pd.DataFrame, label: str, ejec: str, ejec_name: str, color: str, light: str,
                    height: int = 330, theme: str = "light") -> go.Figure:
    """PIM vs. ejecución (devengado/recaudado) por categoría, barras agrupadas."""
    if df.empty or not (df["pim"].any() or df[ejec].any()):
        return empty_fig(height=height, theme=theme)
    d = df.sort_values("pim", ascending=True)
    fig = go.Figure()
    fig.add_bar(y=d[label], x=d["pim"], orientation="h", name="PIM", marker_color=light,
                hovertemplate="%{y}<br>PIM: S/ %{x:,.0f}<extra></extra>")  # fmt: skip
    fig.add_bar(y=d[label], x=d[ejec], orientation="h", name=ejec_name, marker_color=color,
                hovertemplate="%{y}<br>" + ejec_name + ": S/ %{x:,.0f}<extra></extra>")  # fmt: skip
    fig.update_layout(**_layout(height, showlegend=True, legend=dict(orientation="h", y=1.08, x=0)),
                      barmode="group", xaxis=dict(showgrid=True, tickformat=".2s", zeroline=False),
                      yaxis=dict(tickfont=dict(size=12), showgrid=False), bargap=0.25)  # fmt: skip
    return _themed(fig, theme)


# ------------------------------------------------------------------ mapa
@lru_cache(maxsize=1)
def _geo() -> dict | None:
    """Límites del Perú (departamentos), contorno del país y países vecinos. None si falta el archivo."""
    try:
        return json.loads(GEO_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _paths(features: list[dict]) -> tuple[list, list]:
    """Coordenadas de todas las líneas/anillos, separadas por None (un solo trazo de Plotly)."""
    xs, ys = [], []

    def ring(coords):
        for x, y in coords:
            xs.append(x)
            ys.append(y)
        xs.append(None)
        ys.append(None)

    for f in features:
        g = f["geometry"]
        if g["type"] == "Polygon":
            for r in g["coordinates"]:
                ring(r)
        elif g["type"] == "MultiPolygon":
            for poly in g["coordinates"]:
                for r in poly:
                    ring(r)
        elif g["type"] == "LineString":
            ring(g["coordinates"])
        elif g["type"] == "MultiLineString":
            for line in g["coordinates"]:
                ring(line)
    return xs, ys


def _basemap(fig: go.Figure, t: dict) -> list[dict]:
    """Dibuja países vecinos, departamentos (relleno + límites) y contorno del Perú.
    Devuelve los puntos de etiqueta de cada departamento (se dibujan al final, encima de los puntos)."""
    geo = _geo()
    if not geo:
        return []
    feats = geo["features"]
    by = lambda tipo: [f for f in feats if f["properties"]["tipo"] == tipo]  # noqa: E731
    skip = dict(hoverinfo="skip", showlegend=False)
    x, y = _paths(by("vecino"))
    fig.add_scatter(x=x, y=y, mode="lines", fill="toself", fillcolor=t["vecino"],
                    line=dict(color=t["border_dep"], width=0.6), **skip)  # fmt: skip
    deps = by("departamento")
    x, y = _paths(deps)
    fig.add_scatter(x=x, y=y, mode="lines", fill="toself", fillcolor=t["land"],
                    line=dict(color=t["border_dep"], width=0.8), **skip)  # fmt: skip
    x, y = _paths(by("pais"))
    fig.add_scatter(x=x, y=y, mode="lines", line=dict(color=t["border_pais"], width=1.4), **skip)
    return [f["properties"] for f in deps if f["properties"]["nombre"] != "Callao"]


def fig_mapa(df: pd.DataFrame, metrica: str, height: int = 640, theme: str = "light") -> go.Figure:
    """Mapa del Perú con un punto por distrito donde se ubica la entidad (longitud x, latitud y).

    El fondo (departamentos, vecinos) sale de data/ref/peru_mapa.geojson: no necesita internet.
    Tamaño = monto absoluto; color = valor de la métrica elegida.
    """
    if df.empty:
        return empty_fig("Sin ubicaciones con coordenadas para este filtro", height, theme)
    tam = df["tamano"].abs()
    if not tam.any():
        return empty_fig("Sin montos para este filtro", height, theme)
    t = palette(theme)
    if metrica == "avance":
        hi = float(df["valor"].quantile(0.98)) or 1.0
        color, cs, cmin, cmax, cmid, bar = df["valor"], [[0, t["scale_low"]], [1, t["gasto"]]], 0, max(hi, 0.01), None, "Avance"
        fmt = lambda v: f"{v * 100:.1f}%"  # noqa: E731
    elif metrica == "brecha":
        lim = float(df["valor"].abs().quantile(0.98)) or 1.0
        color, cs, cmin, cmax, cmid, bar = df["valor"], [[0, t["gasto"]], [0.5, t["div_mid"]], [1, t["ingreso"]]], -lim, lim, 0, "Recaudado − devengado"
        fmt = soles
    else:
        base = t["ingreso"] if metrica == "recaudado" else t["gasto"]
        lim = float(df["valor"].quantile(0.98)) or 1.0
        color, cs, cmin, cmax, cmid, bar = df["valor"], [[0, t["scale_low"]], [1, base]], 0, lim, None, "S/"
        fmt = soles
    sizeref = 2.0 * float(tam.max()) / (34.0**2)

    fig = go.Figure()
    etiquetas = _basemap(fig, t)
    fig.add_scatter(
        x=df["longitud"], y=df["latitud"], mode="markers", showlegend=False,
        marker=dict(size=tam, sizemode="area", sizeref=sizeref, sizemin=3, color=color, colorscale=cs, cmin=cmin, cmax=cmax,
                    cmid=cmid, opacity=0.85, line=dict(width=0.5, color=t["marker_line"]),
                    colorbar=dict(title=dict(text=bar, font=dict(size=11, color=t["muted"])), thickness=10, len=0.55,
                                  tickfont=dict(size=11, color=t["ink"]), outlinewidth=0)),
        customdata=list(zip(df["distrito"], df["provincia"], df["departamento"], [fmt(v) for v in df["valor"]])),
        hovertemplate="<b>%{customdata[0]}</b><br>%{customdata[1]}, %{customdata[2]}<br>" + bar + ": %{customdata[3]}<extra></extra>",
    )  # fmt: skip
    if etiquetas:
        fig.add_scatter(x=[p["lon"] for p in etiquetas], y=[p["lat"] for p in etiquetas], mode="text",
                        text=[p["nombre"] for p in etiquetas], textfont=dict(size=10.5, color=t["label"]),
                        hoverinfo="skip", showlegend=False)  # fmt: skip

    # Encuadre: todo el Perú; si el filtro deja pocos departamentos, se acerca a sus puntos.
    lon, lat = df["longitud"], df["latitud"]
    x0, x1, y0, y1 = float(lon.min()), float(lon.max()), float(lat.min()), float(lat.max())
    if (x1 - x0) > 9 or (y1 - y0) > 12:
        x0, x1, y0, y1 = -81.6, -68.4, -18.6, 0.2
    else:
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        half = max(x1 - x0, y1 - y0, 3.0) / 2 + 0.8
        x0, x1, y0, y1 = cx - half, cx + half, cy - half, cy + half
    fig.update_layout(
        **_layout(height, margin=dict(l=0, r=0, t=6, b=0)),
        xaxis=dict(visible=False, range=[x0, x1], scaleanchor="y", scaleratio=1, constrain="domain"),
        yaxis=dict(visible=False, range=[y0, y1]),
        dragmode="pan",
    )
    return _themed(fig, theme)
