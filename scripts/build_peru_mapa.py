"""Construye data/ref/peru_mapa.geojson, el fondo del mapa del dashboard.

    pip install shapely            # solo para este script; el dashboard no lo necesita
    python -m scripts.build_peru_mapa

Fuente: Natural Earth (dominio público), https://www.naturalearthdata.com
  - departamentos: ne_10m_admin_1_states_provinces ("Lima Province" se une a "Lima", como en el MEF)
  - países vecinos: ne_50m_admin_0_countries, recortados alrededor del Perú
Se simplifica a ~1 km y se redondea a 3 decimales (~100 m) para que pese ~60 KB.

Rasgos ("tipo"): "departamento" (con punto de etiqueta lon/lat), "pais" (contorno exterior) y "vecino".
"""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path

from shapely.geometry import MultiLineString, box, mapping, shape
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "ref" / "peru_mapa.geojson"
BASE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/"
ADM1, ADM0 = BASE + "ne_10m_admin_1_states_provinces.geojson", BASE + "ne_50m_admin_0_countries.geojson"
TOL = 0.012                               # tolerancia de simplificación en grados (~1.3 km)
BBOX = box(-84.5, -20.5, -66.0, 2.5)      # recorte de los países vecinos
ES = {"Brazil": "Brasil"}


def fetch(url: str) -> dict:
    print("descargando", url.rsplit("/", 1)[1])
    with urllib.request.urlopen(url, timeout=120) as r:  # noqa: S310 (URL fija)
        return json.load(r)


def rnd(o):
    if isinstance(o, float):
        return round(o, 3)
    if isinstance(o, (list, tuple)):
        return [rnd(x) for x in o]
    if isinstance(o, dict):
        return {k: rnd(v) for k, v in o.items()}
    return o


def feature(tipo: str, nombre: str, geom, **props) -> dict:
    return {"type": "Feature", "properties": {"tipo": tipo, "nombre": nombre, **props}, "geometry": rnd(mapping(geom))}


def main() -> None:
    deps: dict[str, list] = {}
    for f in fetch(ADM1)["features"]:
        p = f["properties"]
        if p.get("adm0_a3") == "PER":
            nombre = "Lima" if p["name"] == "Lima Province" else p["name"]
            deps.setdefault(nombre, []).append(shape(f["geometry"]))

    feats, simplificados = [], []
    for nombre, geoms in sorted(deps.items()):
        g = unary_union(geoms).simplify(TOL, preserve_topology=True)
        simplificados.append(g)
        pt = g.representative_point()
        feats.append(feature("departamento", nombre, g, lon=round(pt.x, 3), lat=round(pt.y, 3)))

    # Contorno del país: solo anillos exteriores (cierra las rendijas que deja la simplificación).
    u = unary_union(simplificados).buffer(0.03, join_style=2).buffer(-0.03, join_style=2)
    polys = [p for p in ([u] if u.geom_type == "Polygon" else u.geoms) if p.area > 0.002]
    feats.append(feature("pais", "Perú", MultiLineString([list(p.exterior.coords) for p in polys]).simplify(0.006)))

    for f in fetch(ADM0)["features"]:
        p = f["properties"]
        g = shape(f["geometry"])
        if p["ADM0_A3"] == "PER" or not g.intersects(BBOX):
            continue
        g = g.intersection(BBOX).simplify(TOL * 2, preserve_topology=True)
        if not g.is_empty:
            feats.append(feature("vecino", ES.get(p["NAME"], p["NAME"]), g))

    meta = {"fuente": "Natural Earth 1:10m (departamentos) y 1:50m (países), dominio público; "
                      "'Lima Province' unida a 'Lima'; simplificado ~1 km. Generado por scripts/build_peru_mapa.py"}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"type": "FeatureCollection", "metadata": meta, "features": feats},
                              ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    n = {t: sum(f["properties"]["tipo"] == t for f in feats) for t in ("departamento", "pais", "vecino")}
    print(f"{OUT.relative_to(ROOT)}: {n['departamento']} departamentos, {n['vecino']} vecinos, "
          f"{OUT.stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":
    main()
