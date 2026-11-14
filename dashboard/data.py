"""Capa de datos del dashboard.

Diseño para que sea RÁPIDO:
  * Solo lee los CUBOS (miles/millones de filas), nunca las facts (decenas de millones).
  * Los cubos se cargan UNA vez a una base DuckDB en memoria (columnar, sin disco). Ya traen los
    nombres (departamento, fuente, función…), así que las consultas no hacen joins.
  * Cada consulta usa su propio cursor (thread-safe con el servidor de Dash/gunicorn).
  * Cache LRU de resultados; se invalida solo cuando el ETL publica datos nuevos
    (cambia el mtime de manifest.json) -> sin reiniciar la app.

Semántica de los montos (ver README): todas las medidas de EJECUCIÓN ya llegan aquí como
FLUJO mensual (el ETL normalizó acumulado->flujo), así que "acumulado al mes M" es
SUM(mes <= M). Los SALDOS PRESUPUESTALES (PIA y PIM) se leen según `budget_mode` del manifest:
  * snapshot: saldo repetido cada mes o solo en el mes 0 de apertura -> valor del último mes <= M
    con saldo (el mes 0 cuenta);
  * sum: registrados una vez (PIA) o como modificaciones del periodo (PIM) -> SUM(mes <= M).
"""
from __future__ import annotations

import json
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import duckdb
import pandas as pd

from etl.model import CUBES, DATASETS

MESES = ["Ene", "Feb", "Mar", "Abr", "May", "Jun", "Jul", "Ago", "Set", "Oct", "Nov", "Dic"]
G_FLOW = ("devengado", "girado")
G_STOCK = ("pia", "pim")

METRICAS_MAPA = {
    "devengado": "Devengado (gasto)",
    "avance": "Avance de ejecución (devengado ÷ PIM)",
    "recaudado": "Recaudado (ingreso)",
    "brecha": "Recaudado menos devengado",
}


@dataclass(frozen=True)
class Filters:
    anio: int
    mes: int  # mes de corte
    niveles: tuple[str, ...] = ()
    deptos: tuple[str, ...] = ()
    fuentes: tuple[str, ...] = ()

    def with_anio(self, anio: int) -> "Filters":
        return Filters(anio, self.mes, self.niveles, self.deptos, self.fuentes)


def _where(f: Filters, exclude: tuple[str, ...] = ()) -> tuple[str, list]:
    parts, params = ["anio = ?"], [f.anio]
    for col, vals, key in (
        ("nivel_gobierno", f.niveles, "nivel"),
        ("departamento", f.deptos, "depto"),
        ("fuente", f.fuentes, "fuente"),
    ):
        if vals and key not in exclude:
            parts.append(f"{col} IN ({', '.join('?' * len(vals))})")
            params += list(vals)
    return " AND ".join(parts), params


class Store:
    def __init__(self, warehouse: Path, cache_size: int = 512):
        self.wh = Path(warehouse)
        self.version: int | None = None
        self.budget_mode: dict[str, str] = {"gasto": "snapshot", "ingreso": "sum"}
        self._con: duckdb.DuckDBPyConnection | None = None
        self._tables: set[str] = set()
        self._lock = threading.Lock()
        self._cache: OrderedDict = OrderedDict()
        self._cache_size = cache_size
        self.refresh()

    # ------------------------------------------------------------------ carga
    def _mtime(self) -> int | None:
        p = self.wh / "manifest.json"
        return p.stat().st_mtime_ns if p.exists() else None

    @property
    def ready(self) -> bool:
        return self._con is not None and "cube_gasto_base" in self._tables

    def _read_manifest(self) -> None:
        try:
            m = json.loads((self.wh / "manifest.json").read_text(encoding="utf-8"))
            self.budget_mode = {**self.budget_mode, **m.get("budget_mode", {})}
        except (OSError, ValueError):
            pass

    def refresh(self) -> bool:
        """Recarga si el ETL publicó algo nuevo. Cuesta un stat() por llamada."""
        v = self._mtime()
        if v == self.version:
            return False
        with self._lock:
            if v == self.version:
                return False
            con = duckdb.connect(":memory:")
            tables = set()
            for c in CUBES:
                d = self.wh / c.table
                if not d.exists() or not any(d.glob("anio=*/*.parquet")):
                    continue
                repl = ", ".join(f"cast({m} as double) AS {m}" for m in DATASETS[c.dataset].measures)
                con.execute(
                    f"CREATE TABLE {c.table} AS SELECT * REPLACE ({repl}) "
                    f"FROM read_parquet('{d.as_posix()}/*/*.parquet', hive_partitioning=true)"
                )
                tables.add(c.table)
            # Dimensiones pequeñas para poner nombre/coordenadas a los resultados ya agregados
            for name, f in (("lk_entidad", "dim_entidad.parquet"), ("lk_ubigeo", "dim_ubigeo.parquet")):
                if (self.wh / f).exists():
                    con.execute(f"CREATE TABLE {name} AS SELECT * FROM read_parquet('{(self.wh / f).as_posix()}')")
                    tables.add(name)
            self._read_manifest()
            self._con, self._tables = con, tables
            self.version = v
            self._cache.clear()
        return True

    # --------------------------------------------------------------- consultas
    def query(self, sql: str, params: list | tuple = ()) -> pd.DataFrame:
        self.refresh()
        if not self.ready:
            return pd.DataFrame()
        key = (self.version, sql, tuple(params))
        hit = self._cache.get(key)
        if hit is not None:
            self._cache.move_to_end(key)
            return hit.copy()
        df = self._con.cursor().execute(sql, list(params)).df()  # cursor propio por llamada
        self._cache[key] = df
        if len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return df.copy()

    # -- opciones de filtros ------------------------------------------------------
    def options(self) -> dict:
        if not self.ready:
            return {"anios": [], "meses": {}, "niveles": [], "deptos": [], "fuentes": []}
        # el mes 0 (apertura) trae saldos, pero no es un mes de corte seleccionable
        t = self.query("SELECT DISTINCT anio, mes FROM cube_gasto_base WHERE mes > 0 ORDER BY 1, 2")
        if "cube_ingreso_base" in self._tables:
            t = pd.concat([t, self.query("SELECT DISTINCT anio, mes FROM cube_ingreso_base WHERE mes > 0")]).drop_duplicates()
        meses = {int(a): sorted(int(m) for m in g["mes"]) for a, g in t.groupby("anio")}

        def vals(col: str) -> list[str]:
            return sorted(self.query(f"SELECT DISTINCT {col} AS v FROM cube_gasto_base")["v"].tolist())

        return {"anios": sorted(meses, reverse=True), "meses": meses, "niveles": vals("nivel_gobierno"),
                "deptos": vals("departamento"), "fuentes": vals("fuente")}  # fmt: skip

    # -- helpers SQL de "saldo vigente al mes de corte" ------------------------------
    @staticmethod
    def _stock_month(cube: str, where: str) -> str:
        """Último mes <= corte con saldo (PIA/PIM) dentro del filtro (subconsulta escalar)."""
        return f"(SELECT max(mes) FROM {cube} WHERE {where} AND mes <= ? AND (monto_pim <> 0 OR monto_pia <> 0))"

    def _agg(self, cube: str, f: Filters, group: str | None, flows: tuple[str, ...], stocks: tuple[str, ...],
             exclude: tuple[str, ...] = (), order: str | None = None, limit: int | None = None) -> pd.DataFrame:  # fmt: skip
        """SELECT [group,] sum(flujos <= corte), saldos al corte FROM cube WHERE filtros."""
        where, wp = _where(f, exclude)
        sm = self._stock_month(cube, where)
        mode = self.budget_mode["ingreso" if cube.startswith("cube_ingreso") else "gasto"]
        sel: list[str] = []
        params: list = []  # se arma en el MISMO orden en que aparecen los '?' en el SELECT
        for m in flows:
            sel.append(f"sum(monto_{m}) FILTER (WHERE mes <= ?) AS {m}")
            params.append(f.mes)
        for m in stocks:
            if mode == "sum":  # registrados una vez / como modificaciones del periodo: suma acumulada
                sel.append(f"sum(monto_{m}) FILTER (WHERE mes <= ?) AS {m}")
                params.append(f.mes)
            else:  # saldo: valor del último mes <= corte que tenga saldo (el mes 0 de apertura cuenta)
                sel.append(f"sum(monto_{m}) FILTER (WHERE mes = {sm}) AS {m}")
                params += [*wp, f.mes]
        params += wp
        sql = f"SELECT {group + ', ' if group else ''}{', '.join(sel)} FROM {cube} WHERE {where}"
        if group:
            sql += f" GROUP BY {group}"
        if order:
            sql += f" ORDER BY {order}"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return self.query(sql, params)

    # -- KPIs ------------------------------------------------------------------------------
    def kpi_gasto(self, f: Filters) -> dict:
        row = self._agg("cube_gasto_base", f, None, G_FLOW, G_STOCK).iloc[0].fillna(0)
        out = {k: float(v) for k, v in row.items()}
        prev = self._agg("cube_gasto_base", f.with_anio(f.anio - 1), None, ("devengado",), ()).iloc[0].fillna(0)
        out["devengado_prev"] = float(prev["devengado"])
        return out

    def kpi_ingreso(self, f: Filters) -> dict:
        if "cube_ingreso_base" not in self._tables:
            return {}
        row = self._agg("cube_ingreso_base", f, None, ("recaudado",), G_STOCK).iloc[0].fillna(0)
        out = {k: float(v) for k, v in row.items()}
        prev = self._agg("cube_ingreso_base", f.with_anio(f.anio - 1), None, ("recaudado",), ()).iloc[0].fillna(0)
        out["recaudado_prev"] = float(prev["recaudado"])
        return out

    # -- series mensuales (flujos) -----------------------------------------------------------
    def serie(self, cube: str, f: Filters, flows: tuple[str, ...]) -> pd.DataFrame:
        where, wp = _where(f)
        sel = ", ".join(f"sum(monto_{m}) AS {m}" for m in flows)
        df = self.query(f"SELECT mes, {sel} FROM {cube} WHERE {where} AND mes <= ? GROUP BY mes ORDER BY mes", [*wp, f.mes])
        return df.set_index("mes").reindex(range(1, f.mes + 1)).fillna(0.0).reset_index()

    # -- desgloses ------------------------------------------------------------------------------
    def desglose_gasto(self, cube: str, col: str, f: Filters, exclude: tuple[str, ...] = (), order: str = "devengado DESC", limit: int | None = None) -> pd.DataFrame:  # fmt: skip
        return self._agg(cube, f, col, G_FLOW, ("pim",), exclude, order, limit)

    def desglose_ingreso(self, col: str, f: Filters) -> pd.DataFrame:
        if "cube_ingreso_generica" not in self._tables:
            return pd.DataFrame()
        return self._agg("cube_ingreso_generica", f, col, ("recaudado",), ("pim",), order="recaudado DESC")

    def entidades(self, f: Filters, limit: int = 12) -> pd.DataFrame:
        """Entidades (unidades ejecutoras) con más devengado; la etiqueta sale de DIM_ENTIDAD."""
        df = self.desglose_gasto("cube_gasto_entidad", "sk_entidad", f, limit=limit)
        if df.empty or "lk_entidad" not in self._tables:
            return df
        lk = self.query("SELECT sk_entidad, pliego, ejecutora FROM lk_entidad")
        df = df.merge(lk, on="sk_entidad", how="left")
        # el pliego identifica mejor a la entidad; si coincide con la ejecutora no se repite
        df["etiqueta"] = [e if (p == e or not isinstance(p, str)) else f"{e} · {p}" for p, e in zip(df["pliego"], df["ejecutora"])]
        return df

    def pim_total(self, cube: str, f: Filters) -> float:
        r = self._agg(cube, f, None, (), ("pim",), exclude=("depto",))
        return float(r["pim"].iloc[0] or 0) if len(r) else 0.0

    # -- mapa -----------------------------------------------------------------------------------
    def mapa(self, f: Filters, metrica: str) -> tuple[pd.DataFrame, dict]:
        """Valor por ubicación (distrito) de la entidad, con latitud/longitud de DIM_UBIGEO.

        Devuelve (df, meta): meta["sin_coordenadas"] = ubicaciones con dato pero sin coordenadas.
        """
        if "lk_ubigeo" not in self._tables:
            return pd.DataFrame(), {}
        g = i = None
        if metrica in ("devengado", "avance", "brecha"):
            g = self._agg("cube_gasto_ubigeo", f, "sk_ubigeo", ("devengado",), ("pim",)).rename(columns={"pim": "pim_g"})
        if metrica in ("recaudado", "brecha") and "cube_ingreso_ubigeo" in self._tables:
            i = self._agg("cube_ingreso_ubigeo", f, "sk_ubigeo", ("recaudado",), ("pim",)).rename(columns={"pim": "pim_i"})
        if g is None and i is None:
            return pd.DataFrame(), {}
        df = g if i is None else (i if g is None else g.merge(i, on="sk_ubigeo", how="outer"))
        for c in ("devengado", "recaudado", "pim_g"):
            if c not in df:
                df[c] = 0.0
        df = df.fillna(0.0)
        if metrica == "devengado":
            df["valor"], df["tamano"] = df["devengado"], df["devengado"]
        elif metrica == "avance":
            df = df[df["pim_g"] > 0].copy()
            df["valor"], df["tamano"] = df["devengado"] / df["pim_g"], df["devengado"]
        elif metrica == "recaudado":
            df["valor"], df["tamano"] = df["recaudado"], df["recaudado"]
        else:  # brecha
            df["valor"], df["tamano"] = df["recaudado"] - df["devengado"], (df["recaudado"] - df["devengado"]).abs()
        lk = self.query("SELECT sk_ubigeo, codigo_ubigeo, departamento, provincia, distrito, latitud, longitud FROM lk_ubigeo")
        df = df.merge(lk, on="sk_ubigeo", how="left")
        sin = df[df["latitud"].isna() | df["longitud"].isna()]
        meta = {"sin_coordenadas": int(len(sin)), "monto_sin_coordenadas": float(sin["tamano"].abs().sum()), "total": float(df["tamano"].abs().sum())}
        return df.dropna(subset=["latitud", "longitud"]), meta
