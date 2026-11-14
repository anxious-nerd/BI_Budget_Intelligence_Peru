"""Modelo dimensional DECLARATIVO, alineado con el modelo del equipo (docs/modelo_objetivo.md).

Es la única fuente de verdad: de aquí salen el SQL del ETL, las dimensiones, las hechos y los
cubos del dashboard. Las tablas PUBLICADAS contienen exactamente las columnas del modelo
(lo verifica `python -m scripts.check_spec --strict`).

    FACT_INGRESO ──┐                           ┌── DIM_CLASIF_INGRESO (solo ingreso)
                   ├── DIM_TIEMPO              │
                   ├── DIM_ENTIDAD             ├── DIM_CLASIF_GASTO   (solo gasto)
    FACT_GASTO ────┤   DIM_UBIGEO              └── DIM_FUNCIONAL      (solo gasto, a grano de meta)
                   └── DIM_FINANCIAMIENTO
            (dimensiones conformadas)

Lo que el dashboard necesita y el modelo NO contempla (p. ej. nivel de gobierno) vive aparte, en
tablas de apoyo `ext_*`, para no contaminar el modelo.

Decisiones de ingeniería que se apartan (a propósito) del texto del modelo — ver README:
  * claves sustitutas: hash determinista de 64 bits (no INT secuencial): permite cargas
    incrementales por año sin tabla de mapeo;
  * montos: DECIMAL(18,2) exacto (no FLOAT); latitud/longitud: DOUBLE (no VARCHAR);
  * sk_funcional incluye el AÑO en la clave (SEC_FUNC es un secuencial y no se sabe si se reasigna
    cada año): ver FUNCIONAL.
IMPORTANTE: GENERICA..ESPECIFICA_DET existen en ambos datasets pero son clasificadores
DISTINTOS (de ingreso vs. de gasto): dos dimensiones separadas.
"""
from __future__ import annotations

from dataclasses import dataclass


# --------------------------------------------------------------------------------------
# Normalizadores (se aplican igual en la pasada de dimensiones y en la de hechos)
# --------------------------------------------------------------------------------------
_INT = {"TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "UHUGEINT"}


def code(col: str, dtype: str | None = None) -> str:
    """Código normalizado a VARCHAR no nulo ('-' si viene NULL/vacío).

    Es sensible al TIPO de la columna en el parquet (la normalización por regex es ~3x más cara y
    se aplica fila por fila, así que solo se paga donde hace falta):
      - texto            -> trim()                      (los ceros a la izquierda se conservan)
      - enteros          -> cast a texto
      - decimal/flotante -> cast a BIGINT y luego a texto ('5.0' -> '5'; típico cuando
                            pandas convirtió una columna con vacíos a float)
    """
    d = (dtype or "VARCHAR").upper()
    if d in _INT:
        return f"coalesce(cast({col} as varchar), '-')"
    if d == "VARCHAR":
        return f"coalesce(nullif(trim({col}), ''), '-')"
    return f"coalesce(cast(try_cast({col} as bigint) as varchar), '-')"


def text(col: str) -> str:
    """Descripción normalizada a VARCHAR no nulo."""
    return f"coalesce(nullif(trim(cast({col} as varchar)), ''), 'No definido')"


def money(col: str) -> str:
    """Monto a DECIMAL(18,2) exacto; lo no numérico queda en 0 (profile lo reporta)."""
    return f"coalesce(try_cast({col} as decimal(18,2)), 0)"


def _p2(c: str) -> str:
    """Parte de ubigeo de 2 dígitos a partir de un código ya normalizado con code()."""
    return f"lpad(right(CASE WHEN {c} = '-' THEN '00' ELSE {c} END, 2), 2, '0')"


def ubigeo_expr(types: dict[str, str]) -> str:
    """UBIGEO de 6 dígitos (2 depto + 2 provincia + 2 distrito) a partir de los 3 códigos del MEF.

    El MEF puede entregar los códigos de tres maneras y las tres se aceptan por fila:
      * 2-2-2 : depto '15', provincia '01', distrito '01'
      * 4-6   : provincia '1501' (depto+prov) y distrito '150101' (ubigeo completo)
      * mixtas, o con vacíos ('-' => '00').
    `python -m etl.profile` mide cuántos ubigeos resultantes existen en la tabla de ubigeos.
    """
    dep, prov, dist = (code(c, types.get(c)) for c in ("departamento_ejecutora", "provincia_ejecutora", "distrito_ejecutora"))
    return (
        f"CASE WHEN length({dist}) >= 6 THEN right({dist}, 6) "
        f"WHEN length({prov}) >= 4 THEN right({prov}, 4) || {_p2(dist)} "
        f"ELSE {_p2(dep)} || {_p2(prov)} || {_p2(dist)} END"
    )


# --------------------------------------------------------------------------------------
# Dimensiones
# --------------------------------------------------------------------------------------
# Claves "virtuales": no son una columna cruda sino una expresión derivada.
VIRTUAL_KEYS = {"anio", "codigo_ubigeo"}


@dataclass(frozen=True)
class Dim:
    name: str
    keys: tuple[str, ...]  # clave natural (columnas crudas o virtuales); NO se publica salvo `publish_keys`
    attrs: tuple[tuple[str, str], ...]  # (columna PUBLICADA, columna cruda)  -> se normalizan con text()
    publish_keys: tuple[str, ...] = ()  # claves que el modelo SÍ publica (p. ej. codigo_ubigeo)
    prefix: str = "dim"  # "dim" = parte del modelo | "ext" = apoyo del dashboard, fuera del modelo
    by_year: bool = False  # la clave incluye `anio`: se guarda particionada por año
    extra_published: tuple[str, ...] = ()  # columnas añadidas tras el ETL (latitud, longitud)

    @property
    def sk(self) -> str:
        return f"sk_{self.name}"

    @property
    def table(self) -> str:
        return f"{self.prefix}_{self.name}"

    @property
    def attr_cols(self) -> tuple[str, ...]:
        return tuple(a for a, _ in self.attrs)

    @property
    def published(self) -> tuple[str, ...]:
        """Columnas publicadas, en el orden del modelo."""
        return (self.sk, *self.publish_keys, *self.attr_cols, *self.extra_published)

    def raw_refs(self, ds: "Dataset") -> tuple[str, ...]:
        """Columnas crudas que esta dimensión necesita leer."""
        cols: list[str] = []
        for k in self.keys:
            if k == "anio":
                cols.append(ds.year_col)
            elif k == "codigo_ubigeo":
                cols += ["departamento_ejecutora", "provincia_ejecutora", "distrito_ejecutora"]
            else:
                cols.append(k)
        cols += [raw for _, raw in self.attrs]
        return tuple(dict.fromkeys(cols))

    def key_part(self, k: str, ds: "Dataset", types: dict[str, str]) -> str:
        """Una parte de la clave natural como VARCHAR no nulo (para hashear y para publicar)."""
        if k == "anio":
            return f"cast(try_cast({ds.year_col} as integer) as varchar)"
        if k == "codigo_ubigeo":
            return ubigeo_expr(types)
        # Sin ceros a la izquierda: '000123', '123' y 123 son la MISMA entidad aunque los parquet de
        # gasto e ingreso hayan guardado el tipo distinto (si no, la dimensión dejaría de ser conformada).
        c = code(k, types.get(k))
        return f"coalesce(nullif(ltrim({c}, '0'), ''), '0')"

    def sk_expr(self, ds: "Dataset", types: dict[str, str]) -> str:
        """Clave sustituta determinística: hash 64 bits de la clave natural normalizada.

        Todas las partes son NOT NULL, así que `||` es seguro (concat_ws ignoraría los NULL y podría
        colisionar).
        """
        return "hash(" + " || '|' || ".join(self.key_part(k, ds, types) for k in self.keys) + ")"


def _pairs(*cols: str) -> tuple[tuple[str, str], ...]:
    return tuple((c.removesuffix("_nombre"), c) for c in cols)


ENTIDAD = Dim(
    "entidad",
    keys=("sec_ejec",),
    attrs=(("pliego", "pliego_nombre"), ("ejecutora", "ejecutora_nombre")),
)
# Apoyo (fuera del modelo): jerarquía y códigos de la entidad. Misma clave => mismo sk_entidad.
ENTIDAD_EXT = Dim(
    "entidad",
    prefix="ext",
    keys=("sec_ejec",),
    publish_keys=("sec_ejec",),
    attrs=(
        ("nivel_gobierno_codigo", "nivel_gobierno"), ("nivel_gobierno", "nivel_gobierno_nombre"),
        ("sector_codigo", "sector"), ("sector", "sector_nombre"),
        ("pliego_codigo", "pliego"), ("ejecutora_codigo", "ejecutora"),
    ),
)  # fmt: skip
UBIGEO = Dim(
    "ubigeo",
    keys=("codigo_ubigeo",),
    publish_keys=("codigo_ubigeo",),
    attrs=(
        ("departamento", "departamento_ejecutora_nombre"),
        ("provincia", "provincia_ejecutora_nombre"),
        ("distrito", "distrito_ejecutora_nombre"),
    ),
    extra_published=("latitud", "longitud"),  # vienen de la tabla de ubigeos, no del MEF
)
FINANCIAMIENTO = Dim(
    "financiamiento",
    keys=("fuente_financiamiento", "rubro", "tipo_recurso"),
    attrs=(
        ("fuente", "fuente_financiamiento_nombre"),
        ("rubro", "rubro_nombre"),
        ("tipo_recurso", "tipo_recurso_nombre"),
    ),
)

_CLASIF_KEYS = ("generica", "subgenerica", "subgenerica_det", "especifica", "especifica_det")
_CLASIF_ATTRS = tuple((k, f"{k}_nombre") for k in _CLASIF_KEYS)
CLASIF_INGRESO = Dim("clasif_ingreso", keys=_CLASIF_KEYS, attrs=_CLASIF_ATTRS)
CLASIF_GASTO = Dim("clasif_gasto", keys=_CLASIF_KEYS, attrs=_CLASIF_ATTRS)

# Grano = META. El modelo la identifica por SEC_EJEC + SEC_FUNC. Aquí la clave incluye además el AÑO:
# el diccionario del MEF describe META como "secuencial desde '00001'", o sea un contador, y no se
# sabe si SEC_FUNC se reasigna cada año fiscal. Si se reasigna, una clave sin año mezclaría metas
# distintas y atribuiría mal las funciones de los años viejos; con año nunca puede equivocarse (a lo
# sumo duplica metas estables). `etl.validate` mide cuántas metas cambian de atributos entre años.
# Ojo: en el diccionario, META_NOMBRE describe la FINALIDAD de la meta.
FUNCIONAL = Dim(
    "funcional",
    keys=("anio", "sec_ejec", "sec_func"),
    by_year=True,
    attrs=(
        ("funcion", "funcion_nombre"),
        ("division_funcional", "division_funcional_nombre"),
        ("grupo_funcional", "grupo_funcional_nombre"),
        ("proyecto", "producto_proyecto_nombre"),
        ("inversion", "actividad_accion_obra_nombre"),
        ("meta", "meta_nombre"),
    ),
)

MODEL_DIMS = (ENTIDAD, UBIGEO, FINANCIAMIENTO, CLASIF_INGRESO, CLASIF_GASTO, FUNCIONAL)
EXT_DIMS = (ENTIDAD_EXT,)
ALL_DIMS = (*MODEL_DIMS, *EXT_DIMS)
DIMS_BY_TABLE = {d.table: d for d in ALL_DIMS}


# --------------------------------------------------------------------------------------
# Datasets / tablas de hechos
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Dataset:
    name: str  # 'gasto' | 'ingreso'
    year_col: str  # ANO_EJE (gasto) / ANO_DOC (ingreso)
    month_col: str
    fks: tuple[tuple[str, Dim], ...]  # (columna FK en la fact, dimensión), en el orden del modelo
    stock: tuple[str, ...]  # saldos presupuestales: PIA, PIM
    flow: tuple[str, ...]  # fases de ejecución (flujo mensual tras normalizar)
    # Columnas crudas que definen el grano MÁS FINO (lo que el MEF publica): la conversión
    # acumulado->flujo se hace ahí (no al grano reducido del modelo, donde las series dispersas
    # generarían saltos artificiales) y recién después se agrega al grano del modelo.
    native_cols: tuple[str, ...]

    @property
    def fact(self) -> str:
        return f"fact_{self.name}"

    @property
    def measures(self) -> tuple[str, ...]:
        return tuple(f"monto_{m}" for m in self.stock + self.flow)

    @property
    def flow_measures(self) -> tuple[str, ...]:
        return tuple(f"monto_{m}" for m in self.flow)

    @property
    def fk_cols(self) -> tuple[str, ...]:
        return tuple(fk for fk, _ in self.fks)

    @property
    def dims(self) -> tuple[Dim, ...]:
        return tuple(d for _, d in self.fks)

    def fk_for(self, dim: Dim) -> str:
        """Columna de la fact que referencia a `dim` (las tablas ext_* comparten la FK de su dimensión)."""
        for fk, d in self.fks:
            if d.name == dim.name:
                return fk
        raise KeyError(dim.table)

    @property
    def required_columns(self) -> tuple[str, ...]:
        cols = [self.year_col, self.month_col, *self.measures]
        for d in (*self.dims, *(e for e in EXT_DIMS if any(e.name == x.name for x in self.dims))):
            cols += list(d.raw_refs(self))
        return tuple(dict.fromkeys(cols))


_COMMON_NATIVE = (
    "nivel_gobierno", "sector", "pliego", "sec_ejec", "ejecutora",
    "departamento_ejecutora", "provincia_ejecutora", "distrito_ejecutora",
    "fuente_financiamiento", "rubro", "tipo_recurso", *_CLASIF_KEYS,
)  # fmt: skip

GASTO = Dataset(
    "gasto", "ano_eje", "mes_eje",
    fks=(
        ("sk_entidad", ENTIDAD), ("sk_ubigeo_ejec", UBIGEO), ("sk_financiamiento", FINANCIAMIENTO),
        ("sk_clasif_gasto", CLASIF_GASTO), ("sk_funcional", FUNCIONAL),
    ),
    stock=("pia", "pim"),
    flow=("devengado", "girado"),
    native_cols=(
        *_COMMON_NATIVE, "sec_func", "programa_ppto", "tipo_act_proy", "producto_proyecto", "actividad_accion_obra",
        "funcion", "division_funcional", "grupo_funcional", "meta", "finalidad", "departamento_meta", "categoria_gasto",
    ),
)  # fmt: skip
INGRESO = Dataset(
    "ingreso", "ano_doc", "mes_doc",
    fks=(
        ("sk_entidad", ENTIDAD), ("sk_ubigeo", UBIGEO), ("sk_financiamiento", FINANCIAMIENTO),
        ("sk_clasif_ingreso", CLASIF_INGRESO),
    ),
    stock=("pia", "pim"),
    flow=("recaudado",),
    native_cols=_COMMON_NATIVE,
)  # fmt: skip
DATASETS = {"gasto": GASTO, "ingreso": INGRESO}


# --------------------------------------------------------------------------------------
# Cubos agregados para el dashboard (no son parte del modelo: son agregados de rendimiento)
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Cube:
    name: str
    dataset: str
    # (tabla, columna, alias): "ext_entidad"/"dim_*" = columna de esa dimensión; "fact" = columna de la fact
    by: tuple[tuple[str, str, str], ...]

    @property
    def table(self) -> str:
        return f"cube_{self.name}"


_BASE = (
    ("ext_entidad", "nivel_gobierno", "nivel_gobierno"),
    ("dim_ubigeo", "departamento", "departamento"),
    ("dim_financiamiento", "fuente", "fuente"),
)
CUBES = (
    Cube("gasto_base", "gasto", _BASE),
    Cube("gasto_funcion", "gasto", _BASE + (("dim_funcional", "funcion", "funcion"),)),
    Cube("gasto_proyecto", "gasto", _BASE + (("dim_funcional", "proyecto", "proyecto"),)),
    Cube("gasto_generica", "gasto", _BASE + (("dim_clasif_gasto", "generica", "generica"),)),
    Cube("gasto_entidad", "gasto", _BASE + (("fact", "sk_entidad", "sk_entidad"),)),
    Cube("gasto_ubigeo", "gasto", _BASE + (("fact", "sk_ubigeo_ejec", "sk_ubigeo"),)),
    Cube("ingreso_base", "ingreso", _BASE),
    Cube("ingreso_generica", "ingreso", _BASE + (("dim_clasif_ingreso", "generica", "generica"),)),
    Cube("ingreso_ubigeo", "ingreso", _BASE + (("fact", "sk_ubigeo", "sk_ubigeo"),)),
)  # fmt: skip
CUBES_BY_NAME = {c.name: c for c in CUBES}
