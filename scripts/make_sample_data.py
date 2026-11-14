"""Genera parquet SINTÉTICO con el mismo esquema que los CSV del MEF.

Sirve para (1) probar el pipeline sin descargar GBs, (2) correr los tests y
(3) probar el dashboard. Los datos NO son reales: los nombres de funciones, fuentes
y genéricas son ilustrativos y los montos son aleatorios (pero deterministas).

Uso:
    python -m scripts.make_sample_data --out data/raw --years 2023 2024 --entidades 300
    python -m scripts.make_sample_data --out data/raw --mode flow          # montos como flujo mensual
    python -m scripts.make_sample_data --out data/raw --style snapshot     # PIA/PIM repetidos cada mes (saldo)
    python -m scripts.make_sample_data --out data/raw --diario             # añade un *-Diario.parquet (debe ignorarse)
"""
from __future__ import annotations

import argparse
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_UBIGEOS = ROOT / "data" / "ref" / "TB_UBIGEOS.csv"

DEPARTAMENTOS = [
    "Amazonas", "Áncash", "Apurímac", "Arequipa", "Ayacucho", "Cajamarca", "Callao", "Cusco",
    "Huancavelica", "Huánuco", "Ica", "Junín", "La Libertad", "Lambayeque", "Lima", "Loreto",
    "Madre de Dios", "Moquegua", "Pasco", "Piura", "Puno", "San Martín", "Tacna", "Tumbes", "Ucayali",
]  # fmt: skip
FUENTES = [
    ("1", "Recursos Ordinarios"), ("2", "Recursos Directamente Recaudados"),
    ("3", "Recursos por Operaciones Oficiales de Crédito"), ("4", "Donaciones y Transferencias"),
    ("5", "Recursos Determinados"),
]  # fmt: skip
FUNCIONES = [
    "Legislativa", "Relaciones Exteriores", "Planeamiento, Gestión y Reserva de Contingencia",
    "Defensa y Seguridad Nacional", "Orden Público y Seguridad", "Justicia", "Trabajo", "Comercio",
    "Turismo", "Agropecuaria", "Pesca", "Energía", "Minería", "Industria", "Transporte",
    "Comunicaciones", "Medio Ambiente", "Saneamiento", "Vivienda y Desarrollo Urbano", "Salud",
    "Cultura y Deporte", "Educación", "Protección Social", "Previsión Social", "Deuda Pública",
]  # fmt: skip
G_GASTO = [
    "Personal y Obligaciones Sociales", "Pensiones y Otras Prestaciones Sociales", "Bienes y Servicios",
    "Donaciones y Transferencias", "Otros Gastos", "Adquisición de Activos No Financieros",
    "Adquisición de Activos Financieros", "Servicio de la Deuda Pública",
]  # fmt: skip
G_INGRESO = [
    "Impuestos y Contribuciones Obligatorias", "Contribuciones Sociales",
    "Venta de Bienes y Servicios y Derechos Administrativos", "Donaciones y Transferencias",
    "Otros Ingresos", "Venta de Activos No Financieros", "Venta de Activos Financieros",
    "Endeudamiento", "Saldos de Balance",
]  # fmt: skip
CATEGORIAS = ["Acciones Centrales", "Programas Presupuestales", "Asignaciones Presupuestarias que no Resultan en Productos"]
PROGRAMAS = [("0001", "Programa Articulado Nutricional"), ("0002", "Salud Materno Neonatal"),
             ("0066", "Formación Universitaria de Pregrado"), ("0090", "Logros de Aprendizaje de Estudiantes"),
             ("0138", "Reducción del Costo, Tiempo e Inseguridad en el Sistema de Transporte"),
             ("9001", "Acciones Centrales"), ("9002", "Asignaciones Presupuestarias que no Resultan en Productos")]  # fmt: skip


def _list(values) -> str:
    return "[" + ", ".join("'" + str(v).replace("'", "''") + "'" for v in values) + "]"


def _h(*parts) -> str:
    """Pseudo-aleatorio determinístico (hash) -> entero no negativo."""
    return "((hash(" + ", ".join(str(p) for p in parts) + ") >> 1)::BIGINT)"


def _entities(con, n: int, ubigeo_csv: Path, style: str) -> None:
    """Entidades sintéticas ubicadas en distritos REALES (tomados de la tabla de ubigeos).

    style = convención de los códigos de ubicación que entrega el "MEF":
        2-2-2 : departamento '15', provincia '01', distrito '01'
        4-6   : provincia '1501' (depto+prov), distrito '150101' (ubigeo completo)
    """
    assert style in ("2-2-2", "4-6"), style
    con.execute(f"""
        CREATE OR REPLACE TABLE ub AS
        SELECT row_number() OVER (ORDER BY ubigeo_inei) - 1 AS r, ubigeo_inei, departamento, provincia, distrito
        FROM read_csv('{ubigeo_csv.as_posix()}', delim=';', header=true, all_varchar=true, encoding='utf-8')
    """)
    nub = con.execute("SELECT count(*) FROM ub").fetchone()[0]
    con.execute(f"""
        CREATE OR REPLACE TABLE ent AS
        SELECT i AS id, i // 3 AS pl,
               CASE WHEN i % 100 < 82 THEN 'M' WHEN i % 100 < 90 THEN 'R' ELSE 'E' END AS nivel,
               {_h('i // 3', "'ub'")} % {nub} AS r
        FROM range({n}) t(i)
    """)
    prov = "substr(u.ubigeo_inei, 3, 2)" if style == "2-2-2" else "substr(u.ubigeo_inei, 1, 4)"
    dist = "substr(u.ubigeo_inei, 5, 2)" if style == "2-2-2" else "u.ubigeo_inei"
    con.execute(f"""
        CREATE OR REPLACE TABLE ent2 AS
        SELECT id, pl, nivel, cast(substr(u.ubigeo_inei, 1, 2) AS INTEGER) AS dep,
          nivel AS "NIVEL_GOBIERNO",
          CASE nivel WHEN 'E' THEN 'GOBIERNO NACIONAL' WHEN 'R' THEN 'GOBIERNOS REGIONALES' ELSE 'GOBIERNOS LOCALES' END AS "NIVEL_GOBIERNO_NOMBRE",
          CASE nivel WHEN 'E' THEN lpad((1 + pl % 30)::varchar, 2, '0') ELSE '99' END AS "SECTOR",
          CASE nivel WHEN 'E' THEN 'SECTOR ' || (1 + pl % 30) ELSE 'GOBIERNOS ' || CASE nivel WHEN 'R' THEN 'REGIONALES' ELSE 'LOCALES' END END AS "SECTOR_NOMBRE",
          lpad((pl + 1)::varchar, 6, '0') AS "PLIEGO",
          CASE nivel WHEN 'M' THEN 'MUNICIPALIDAD ' || (pl + 1) WHEN 'R' THEN 'GOBIERNO REGIONAL ' || (pl + 1) ELSE 'MINISTERIO ' || (pl + 1) END AS "PLIEGO_NOMBRE",
          lpad((id + 1)::varchar, 6, '0') AS "SEC_EJEC",
          lpad((1 + id % 3)::varchar, 3, '0') AS "EJECUTORA",
          'UNIDAD EJECUTORA ' || (id + 1) AS "EJECUTORA_NOMBRE",
          substr(u.ubigeo_inei, 1, 2) AS "DEPARTAMENTO_EJECUTORA", u.departamento AS "DEPARTAMENTO_EJECUTORA_NOMBRE",
          {prov} AS "PROVINCIA_EJECUTORA", u.provincia AS "PROVINCIA_EJECUTORA_NOMBRE",
          {dist} AS "DISTRITO_EJECUTORA", u.distrito AS "DISTRITO_EJECUTORA_NOMBRE"
        FROM ent e JOIN ub u ON u.r = e.r
    """)


def _write(con, sql: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con.execute(f"COPY ({sql}) TO '{path.as_posix()}' (FORMAT PARQUET)")


def _cumulate(mode: str, cols: list[str], part: str = "id, j") -> str:
    """Convierte flujos en acumulados (modo cumulative) o los deja igual (modo flow)."""
    if mode == "flow":
        return ", ".join(f'{c} AS "MONTO_{c.upper()}"' for c in cols)
    return ", ".join(
        f'round(sum({c}) OVER (PARTITION BY {part} ORDER BY m), 2) AS "MONTO_{c.upper()}"' for c in cols
    )


def generate(out: Path, years=(2023, 2024), entidades: int = 300, claves: int = 12,
             mode: str = "cumulative", diario: bool = False, stock_style: str = "spec",
             ubigeo_csv: Path | None = None, ubigeo_style: str = "2-2-2") -> None:  # fmt: skip
    """Escribe out/gasto/<año>-Gasto.parquet y out/ingreso/<año>-Ingreso.parquet.

    mode        = cumulative | flow  : cómo vienen los montos de EJECUCIÓN por mes.
    stock_style = cómo vienen PIA/PIM (los saldos):
        spec     (por defecto, como documenta el README del equipo)
                 GASTO   : PIA/PIM solo en MES_EJE = 0 (apertura); meses 1-12 sin PIA/PIM.
                 INGRESO : PIA solo en el primer mes; PIM como MODIFICACIÓN del periodo
                           (la suma acumulada es el PIM vigente).
        snapshot : PIA/PIM repetidos cada mes como saldo vigente.
    """
    assert stock_style in ("spec", "snapshot"), stock_style
    con = duckdb.connect()
    con.execute("SET threads=2")
    _entities(con, entidades, ubigeo_csv or DEFAULT_UBIGEOS, ubigeo_style)
    fnames, gnames = _list(FUNCIONES), _list(G_GASTO)
    for year in years:
        scale = 1 + (year - 2020) * 0.03
        # ---------------------------------------------------------------- GASTO
        con.execute(f"""
            CREATE OR REPLACE TABLE gk AS
            SELECT e.*, j,
              1 + {_h('e.id','j',"'f'")} % 5 AS f,
              1 + {_h('e.id','j',"'g'")} % 8 AS g,
              1 + {_h('e.id','j',"'fn'")} % 25 AS fn,
              1 + {_h('e.id','j',"'pp'")} % 7 AS pp,
              1 + {_h('e.id','j',"'cat'")} % 3 AS cat,
              1 + {_h('e.id','j',"'m'")} % 3 AS first_m,
              round((1 + {_h('e.id','j',"'pia'")} % 1000) * 1000.0 * {scale}, 2) AS pia,
              CASE WHEN e.nivel = 'E' THEN 1 + {_h('e.id','j',"'dm'")} % 25 ELSE e.dep END AS dep_meta
            FROM ent2 e, range({claves}) k(j)
        """)
        con.execute(f"""
            CREATE OR REPLACE TABLE gm AS
            SELECT gk.*, m,
              round(pia * (1 + 0.02 * m), 2) AS pim,
              round(pia, 2) AS comprometido_anual,
              CASE WHEN {_h('id','j','m',"'a'")} % 4 = 0 THEN 0 ELSE round(pia / 12 * 1.1 * (0.5 + ({_h('id','j','m',"'b'")} % 100) / 100.0), 2) END AS certificado,
              CASE WHEN {_h('id','j','m',"'a'")} % 4 = 0 THEN 0 ELSE round(pia / 12 * 1.0 * (0.5 + ({_h('id','j','m',"'c'")} % 100) / 100.0), 2) END AS comprometido,
              CASE WHEN {_h('id','j','m',"'a'")} % 4 = 0 THEN 0 ELSE round(pia / 12 * 0.9 * (0.5 + ({_h('id','j','m',"'d'")} % 100) / 100.0), 2) END AS devengado,
              CASE WHEN {_h('id','j','m',"'a'")} % 4 = 0 THEN 0 ELSE round(pia / 12 * 0.85 * (0.5 + ({_h('id','j','m',"'e'")} % 100) / 100.0), 2) END AS girado
            FROM gk, range(1, 13) mm(m) WHERE m >= first_m
        """)
        if stock_style == "spec":
            con.execute("""
                CREATE OR REPLACE TABLE gm AS
                SELECT * REPLACE (0 AS pia, 0 AS pim) FROM gm
                UNION ALL BY NAME
                SELECT gk.*, 0 AS m, round(pia * 1.24, 2) AS pim, 0 AS comprometido_anual,
                       0 AS certificado, 0 AS comprometido, 0 AS devengado, 0 AS girado
                FROM gk
            """)
        tipos = _cumulate(mode, ["certificado", "comprometido", "devengado", "girado"])
        gasto_sql = f"""
            SELECT {year} AS "ANO_EJE", m AS "MES_EJE",
              "NIVEL_GOBIERNO", "NIVEL_GOBIERNO_NOMBRE", "SECTOR", "SECTOR_NOMBRE", "PLIEGO", "PLIEGO_NOMBRE",
              "SEC_EJEC", "EJECUTORA", "EJECUTORA_NOMBRE",
              "DEPARTAMENTO_EJECUTORA", "DEPARTAMENTO_EJECUTORA_NOMBRE", "PROVINCIA_EJECUTORA",
              "PROVINCIA_EJECUTORA_NOMBRE", "DISTRITO_EJECUTORA", "DISTRITO_EJECUTORA_NOMBRE",
              lpad((j + 1)::varchar, 5, '0') AS "SEC_FUNC",
              {_list([p for p, _ in PROGRAMAS])}[pp] AS "PROGRAMA_PPTO",
              {_list([n for _, n in PROGRAMAS])}[pp] AS "PROGRAMA_PPTO_NOMBRE",
              '1' AS "TIPO_ACT_PROY", 'ACTIVIDAD' AS "TIPO_ACT_PROY_NOMBRE",
              lpad((3000000 + pp * 100 + g)::varchar, 7, '0') AS "PRODUCTO_PROYECTO",
              'PRODUCTO ' || pp || '-' || g AS "PRODUCTO_PROYECTO_NOMBRE",
              lpad((5000000 + g)::varchar, 7, '0') AS "ACTIVIDAD_ACCION_OBRA",
              'ACTIVIDAD ' || g AS "ACTIVIDAD_ACCION_OBRA_NOMBRE",
              lpad(fn::varchar, 2, '0') AS "FUNCION", {fnames}[fn] AS "FUNCION_NOMBRE",
              lpad(fn::varchar, 2, '0') || '1' AS "DIVISION_FUNCIONAL", 'DIVISION ' || fn AS "DIVISION_FUNCIONAL_NOMBRE",
              lpad(fn::varchar, 2, '0') || '11' AS "GRUPO_FUNCIONAL", 'GRUPO ' || fn AS "GRUPO_FUNCIONAL_NOMBRE",
              lpad((j + 1)::varchar, 5, '0') AS "META",
              lpad((1 + {_h('id','j',"'fin'")} % 50)::varchar, 7, '0') AS "FINALIDAD",
              'FINALIDAD ' || (1 + {_h('id','j',"'fin'")} % 50) AS "META_NOMBRE",
              lpad(dep_meta::varchar, 2, '0') AS "DEPARTAMENTO_META",
              {_list(DEPARTAMENTOS)}[dep_meta] AS "DEPARTAMENTO_META_NOMBRE",
              {_list([c for c, _ in FUENTES])}[f] AS "FUENTE_FINANCIAMIENTO",
              {_list([n for _, n in FUENTES])}[f] AS "FUENTE_FINANCIAMIENTO_NOMBRE",
              lpad((f * 4)::varchar, 2, '0') AS "RUBRO", 'RUBRO ' || (f * 4) AS "RUBRO_NOMBRE",
              '1' AS "TIPO_RECURSO", 'RECURSOS PUBLICOS' AS "TIPO_RECURSO_NOMBRE",
              cat::varchar AS "CATEGORIA_GASTO", {_list(CATEGORIAS)}[cat] AS "CATEGORIA_GASTO_NOMBRE",
              '2' AS "TIPO_TRANSACCION",
              g::varchar AS "GENERICA", {gnames}[g] AS "GENERICA_NOMBRE",
              '1' AS "SUBGENERICA", 'SUBGENERICA ' || g AS "SUBGENERICA_NOMBRE",
              '1' AS "SUBGENERICA_DET", 'SUBGEN DET ' || g AS "SUBGENERICA_DET_NOMBRE",
              '1' AS "ESPECIFICA", 'ESPECIFICA ' || g AS "ESPECIFICA_NOMBRE",
              '1' AS "ESPECIFICA_DET", 'ESP DET ' || g AS "ESPECIFICA_DET_NOMBRE",
              pia AS "MONTO_PIA", pim AS "MONTO_PIM", comprometido_anual AS "MONTO_COMPROMETIDO_ANUAL",
              {tipos}
            FROM gm
        """
        _write(con, gasto_sql, out / "gasto" / f"{year}-Gasto.parquet")

        # -------------------------------------------------------------- INGRESO
        con.execute(f"""
            CREATE OR REPLACE TABLE ik AS
            SELECT e.*, j,
              1 + {_h('e.id','j',"'if'")} % 5 AS f,
              1 + {_h('e.id','j',"'ig'")} % 9 AS g,
              1 + {_h('e.id','j',"'im'")} % 3 AS first_m,
              round((1 + {_h('e.id','j',"'ipia'")} % 1000) * 1200.0 * {scale}, 2) AS pia
            FROM ent2 e, range({max(2, claves // 2)}) k(j)
        """)
        con.execute(f"""
            CREATE OR REPLACE TABLE im AS
            SELECT ik.*, m, round(pia * (1 + 0.02 * m), 2) AS pim,
              CASE WHEN {_h('id','j','m',"'z'")} % 4 = 0 THEN 0 ELSE round(pia / 12 * (0.5 + ({_h('id','j','m',"'r'")} % 100) / 100.0), 2) END AS recaudado
            FROM ik, range(1, 13) mm(m) WHERE m >= first_m
        """)
        if stock_style == "spec":
            con.execute("""
                CREATE OR REPLACE TABLE im AS
                SELECT * REPLACE (
                    CASE WHEN m = first_m THEN pia ELSE 0 END AS pia,
                    pim - coalesce(lag(pim) OVER (PARTITION BY id, j ORDER BY m), 0) AS pim)
                FROM im
            """)
        rec = _cumulate(mode, ["recaudado"])
        ing_sql = f"""
            SELECT {year} AS "ANO_DOC", m AS "MES_DOC",
              "NIVEL_GOBIERNO", "NIVEL_GOBIERNO_NOMBRE", "SECTOR", "SECTOR_NOMBRE", "PLIEGO", "PLIEGO_NOMBRE",
              "SEC_EJEC", "EJECUTORA", "EJECUTORA_NOMBRE",
              "DEPARTAMENTO_EJECUTORA", "DEPARTAMENTO_EJECUTORA_NOMBRE", "PROVINCIA_EJECUTORA",
              "PROVINCIA_EJECUTORA_NOMBRE", "DISTRITO_EJECUTORA", "DISTRITO_EJECUTORA_NOMBRE",
              {_list([c for c, _ in FUENTES])}[f] AS "FUENTE_FINANCIAMIENTO",
              {_list([n for _, n in FUENTES])}[f] AS "FUENTE_FINANCIAMIENTO_NOMBRE",
              lpad((f * 4)::varchar, 2, '0') AS "RUBRO", 'RUBRO ' || (f * 4) AS "RUBRO_NOMBRE",
              '1' AS "TIPO_RECURSO", 'RECURSOS PUBLICOS' AS "TIPO_RECURSO_NOMBRE",
              g::varchar AS "GENERICA", {_list(G_INGRESO)}[g] AS "GENERICA_NOMBRE",
              '1' AS "SUBGENERICA", 'SUBGENERICA ' || g AS "SUBGENERICA_NOMBRE",
              '1' AS "SUBGENERICA_DET", 'SUBGEN DET ' || g AS "SUBGENERICA_DET_NOMBRE",
              '1' AS "ESPECIFICA", 'ESPECIFICA ' || g AS "ESPECIFICA_NOMBRE",
              '1' AS "ESPECIFICA_DET", 'ESP DET ' || g AS "ESPECIFICA_DET_NOMBRE",
              pia AS "MONTO_PIA", pim AS "MONTO_PIM", {rec}
            FROM im
        """
        _write(con, ing_sql, out / "ingreso" / f"{year}-Ingreso.parquet")

    if diario:  # copia con otro nombre: el ETL debe IGNORARLA (si no, duplicaría montos)
        y = max(years)
        _write(con, f"SELECT * FROM read_parquet('{(out / 'gasto' / f'{y}-Gasto.parquet').as_posix()}')",
               out / "gasto" / f"{y}-Gasto-Diario.parquet")  # fmt: skip
    con.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=Path("data/raw"))
    ap.add_argument("--years", type=int, nargs="+", default=[2023, 2024])
    ap.add_argument("--entidades", type=int, default=300)
    ap.add_argument("--claves", type=int, default=12, help="claves nativas de gasto por entidad")
    ap.add_argument("--mode", choices=["cumulative", "flow"], default="cumulative")
    ap.add_argument("--style", choices=["spec", "snapshot"], default="spec", help="cómo vienen PIA/PIM")
    ap.add_argument("--diario", action="store_true")
    ap.add_argument("--ubigeos", type=Path, default=DEFAULT_UBIGEOS, help="tabla de ubigeos (geografía real)")
    ap.add_argument("--ubigeo-style", choices=["2-2-2", "4-6"], default="2-2-2", help="convención de códigos de ubicación")
    a = ap.parse_args()
    generate(a.out, a.years, a.entidades, a.claves, a.mode, a.diario, a.style, a.ubigeos, a.ubigeo_style)
    print(f"Listo: {a.out}/gasto y {a.out}/ingreso  (ejecución: {a.mode}; PIA/PIM: {a.style})")


if __name__ == "__main__":
    main()
