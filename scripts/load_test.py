"""Prueba de carga simple contra un dashboard en ejecución.

    python -m scripts.load_test --url http://127.0.0.1:8050 --requests 60 --workers 12

Dispara callbacks reales (/_dash-update-component) con filtros variados, de forma concurrente,
y reporta errores y latencias (p50/p95/máx). Sirve para validar gunicorn/hilos antes de publicar.
"""
from __future__ import annotations

import argparse
import itertools
import statistics
import time
from concurrent.futures import ThreadPoolExecutor

import requests

FILTROS = [
    {}, {"f-nivel": ["GOBIERNOS LOCALES"]}, {"f-nivel": ["GOBIERNOS REGIONALES", "GOBIERNO NACIONAL"]},
    {"f-depto": ["LIMA"]}, {"f-depto": ["CUSCO", "PIURA", "LIMA"]}, {"f-fuente": ["Recursos Ordinarios"]},
    {"f-fuente": ["Recursos Ordinarios", "Recursos Directamente Recaudados"], "f-nivel": ["GOBIERNOS LOCALES"]},
    {"f-nivel": ["NO_EXISTE"]},
]  # fmt: skip
METRICAS = ["devengado", "avance", "recaudado", "brecha"]


def _payload(dep: dict, values: dict) -> dict:
    outs = dep["output"].strip(".").split("...")
    outputs = [{"id": o.rsplit(".", 1)[0], "property": o.rsplit(".", 1)[1]} for o in outs]
    return {
        "output": dep["output"],
        "outputs": outputs if len(outputs) > 1 else outputs[0],
        "inputs": [{"id": i["id"], "property": i["property"], "value": values.get(i["id"])} for i in dep["inputs"]],
        "changedPropIds": [f"{dep['inputs'][0]['id']}.{dep['inputs'][0]['property']}"],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://127.0.0.1:8050")
    ap.add_argument("--requests", type=int, default=60)
    ap.add_argument("--workers", type=int, default=12)
    a = ap.parse_args()

    deps = requests.get(f"{a.url}/_dash-dependencies", timeout=10).json()
    update = [d for d in deps if "kpi-" in d["output"]]
    base = {"f-anio": None, "f-mes": 12}
    # año/mes reales desde el layout
    layout = requests.get(f"{a.url}/_dash-layout", timeout=10).json()

    def find(node, wanted):
        if isinstance(node, dict):
            if node.get("props", {}).get("id") == wanted:
                return node["props"]
            for v in node.values():
                r = find(v, wanted)
                if r:
                    return r
        elif isinstance(node, list):
            for v in node:
                r = find(v, wanted)
                if r:
                    return r

    base["f-anio"] = find(layout, "f-anio")["value"]
    base["f-mes"] = find(layout, "f-mes")["value"]

    # además de KPIs/gráficos, el callback del mapa con sus 4 métricas
    mapa = [d for d in deps if "g-mapa" in d["output"]]
    combos = [(d, {**base, **f}) for d in update for f in FILTROS]
    combos += [(d, {**base, **f, "map-metric": m}) for d in mapa for f in FILTROS for m in METRICAS]
    jobs = list(itertools.islice(itertools.cycle(combos), a.requests))

    def hit(job):
        dep, vals = job
        t = time.perf_counter()
        r = requests.post(f"{a.url}/_dash-update-component", json=_payload(dep, vals), timeout=60)
        return r.status_code, (time.perf_counter() - t) * 1000

    t0 = time.perf_counter()
    with ThreadPoolExecutor(a.workers) as ex:
        res = list(ex.map(hit, jobs))
    wall = time.perf_counter() - t0
    ms = sorted(m for _, m in res)
    errs = [s for s, _ in res if s != 200]
    print(f"{len(res)} callbacks con {a.workers} clientes en paralelo: {wall:.1f} s en total, {len(res) / wall:.1f} req/s")
    print(f"latencia ms: p50={statistics.median(ms):.0f}  p95={ms[int(len(ms) * 0.95) - 1]:.0f}  máx={ms[-1]:.0f}")
    print(f"errores: {len(errs)} {errs[:5] if errs else ''}")
    return 1 if errs else 0


if __name__ == "__main__":
    raise SystemExit(main())
