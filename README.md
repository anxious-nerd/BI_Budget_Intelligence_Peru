# BI_Budget_Intelligence_Peru
Large scale Budgeting Analysis Tool using open data from the Peruvian government's platform

## Integrantes:

* Abel Montes de Oca
* Rodrigo Bisetti
* María Mendoza
* Ximena Ramírez

# 1. Contexto


# 2. Descripción de la institución


# 3. Fuentes de datos


# 4. Problemática


# 5. Objetivos

## Objetivo general


## Objetivos específicos


# 6. Marco teórico


# 7. Modelamiento multidimensional

El modelo está compuesto por dos tablas de hechos: `FACT_INGRESO` y `FACT_GASTO`. Ambas comparten las dimensiones conformadas `DIM_TIEMPO`, `DIM_ENTIDAD`, `DIM_UBIGEO` y `DIM_FINANCIAMIENTO`, lo que permite comparar ingresos y gastos bajo un mismo contexto temporal, institucional, territorial y de fuente de financiamiento. Además, cada tabla de hechos se relaciona con dimensiones propias que describen la naturaleza económica, funcional y programática de los montos.

A continuación, se presenta la descripción de las tablas que conforman el modelo dimensional.

## Diccionario de datos

### DIM_TIEMPO
 
La dimensión tiempo almacena la información temporal asociada a los registros de ingreso y gasto. Su granularidad es mensual, en concordancia con la frecuencia de publicación de los datos del MEF, y permite realizar análisis de evolución y acumulados a lo largo del año fiscal.
 
| Nombre de columna | Tipo de dato | Descripción |
|---|---|---|
| sk_tiempo | INT | Identificador único de la dimensión tiempo (clave subrogada). |
| anio | INT | Año fiscal al que corresponde el registro. |
| mes | INT | Número del mes del registro (1–12). El valor 0 corresponde al registro de apertura presupuestal. |
 
---
 
### DIM_ENTIDAD
  
La dimensión entidad almacena la información de las instituciones públicas que registran ingresos y ejecutan gasto. Su nivel de detalle es la unidad ejecutora, que es la instancia que administra directamente los recursos dentro de un pliego presupuestal.
 
| Nombre de columna | Tipo de dato | Descripción |
|---|---|---|
| sk_entidad | INT | Identificador único de la dimensión entidad (clave subrogada). |
| pliego | VARCHAR(150) | Nombre del pliego presupuestal al que pertenece la unidad ejecutora (por ejemplo, un ministerio, gobierno regional o municipalidad). |
| ejecutora | VARCHAR(150) | Nombre de la unidad ejecutora responsable de la recaudación o del gasto. |
 
---
 
### DIM_UBIGEO
 
La dimensión ubigeo almacena la información geográfica según la división político-administrativa del Perú. Cumple un doble rol en el modelo: identifica la ubicación de la unidad ejecutora y, en el caso del gasto, la ubicación donde se ejecuta la meta presupuestal.
 
| Nombre de columna | Tipo de dato | Descripción |
|---|---|---|
| sk_ubigeo | INT | Identificador único de la dimensión ubigeo (clave subrogada). |
| departamento | VARCHAR(50) | Nombre del departamento. |
| provincia | VARCHAR(50) | Nombre de la provincia. |
| distrito | VARCHAR(50) | Nombre del distrito. |
 
---
 
### DIM_FINANCIAMIENTO
 
La dimensión financiamiento almacena el origen de los recursos públicos. Es una dimensión conformada que permite comparar, para una misma fuente, cuánto se recaudó y cuánto se gastó; por ejemplo, en el rubro de canon y sobrecanon.
 
| Nombre de columna | Tipo de dato | Descripción |
|---|---|---|
| sk_financiamiento | INT | Identificador único de la dimensión financiamiento (clave subrogada). |
| fuente | VARCHAR(100) | Fuente de financiamiento (Recursos Ordinarios, Recursos Directamente Recaudados, Recursos por Operaciones Oficiales de Crédito, Donaciones y Transferencias, Recursos Determinados). |
| rubro | VARCHAR(100) | Rubro de financiamiento, que desagrega la fuente (por ejemplo, Canon y sobrecanon, regalías, renta de aduanas y participaciones). |
| tipo_recurso | VARCHAR(150) | Tipo de recurso específico dentro del rubro. |
 
---
 
### DIM_CLASIF_INGRESO

La dimensión clasificador de ingreso almacena la clasificación económica de los ingresos según el Clasificador de Ingresos del MEF. Permite identificar la naturaleza de cada ingreso, como impuestos, transferencias, endeudamiento o saldos de balance.
 
| Nombre de columna | Tipo de dato | Descripción |
|---|---|---|
| sk_clasif_ingreso | INT | Identificador único de la dimensión clasificador de ingreso (clave subrogada). |
| generica | VARCHAR(100) | Genérica de ingreso, el nivel más agregado del clasificador (por ejemplo, Impuestos y contribuciones obligatorias, Saldos de balance). |
| especifica_det | VARCHAR(200) | Específica de ingreso a nivel de detalle, el nivel más desagregado del clasificador. |
 
---
 
### DIM_CLASIF_GASTO
  
La dimensión clasificador de gasto almacena la clasificación económica del gasto según el Clasificador de Gastos del MEF. Permite identificar en qué se utilizan los recursos, como planillas, bienes y servicios o inversión en activos.
 
| Nombre de columna | Tipo de dato | Descripción |
|---|---|---|
| sk_clasif_gasto | INT | Identificador único de la dimensión clasificador de gasto (clave subrogada). |
| generica | VARCHAR(100) | Genérica de gasto, el nivel más agregado del clasificador (por ejemplo, Personal y obligaciones sociales, Bienes y servicios, Adquisición de activos no financieros). |
| especifica_det | VARCHAR(200) | Específica de gasto a nivel de detalle, el nivel más desagregado del clasificador. |
 
---
 
### DIM_FUNCIONAL
  
La dimensión funcional almacena la clasificación funcional del gasto, que agrupa las acciones del Estado según las grandes áreas de intervención pública. Permite analizar la orientación del gasto hacia sectores como educación, salud o transporte.
 
| Nombre de columna | Tipo de dato | Descripción |
|---|---|---|
| sk_funcional | INT | Identificador único de la dimensión funcional (clave subrogada). |
| funcion | VARCHAR(100) | Función del Estado a la que se orienta el gasto (por ejemplo, Educación, Salud, Transporte). |
| grupo_funcional | VARCHAR(150) | Grupo funcional, el nivel más desagregado de la clasificación funcional. |
 
---
 
### DIM_PROGRAMA
 
La dimensión programa almacena la estructura programática del gasto. Permite vincular la ejecución con los programas presupuestales orientados a resultados y distinguir entre productos (gasto operativo) y proyectos (inversión).
 
| Nombre de columna | Tipo de dato | Descripción |
|---|---|---|
| sk_programa | INT | Identificador único de la dimensión programa (clave subrogada). |
| programa_ppto | VARCHAR(200) | Programa presupuestal al que se asigna el gasto, o la categoría de acciones centrales o asignaciones que no resultan en productos. |
| producto_proyecto | VARCHAR(250) | Producto o proyecto de inversión al que se destina el gasto. |
 
---
 
### DIM_META
 
 
La dimensión meta almacena las metas presupuestales, que son la unidad mínima de programación del gasto dentro de cada unidad ejecutora. Incluye además la categoría de gasto, que distingue entre gasto corriente, gasto de capital y servicio de la deuda.
 
| Nombre de columna | Tipo de dato | Descripción |
|---|---|---|
| sk_meta | INT | Identificador único de la dimensión meta (clave subrogada). |
| meta | VARCHAR(250) | Nombre de la meta presupuestal. |
| categoria_gasto | VARCHAR(50) | Categoría del gasto: Gastos corrientes, Gastos de capital o Servicio de la deuda. |
 
---
 
### FACT_INGRESO
 
 
La tabla de hechos de ingreso almacena los montos presupuestados y recaudados por las entidades públicas. Su granularidad es un registro por mes, entidad, ubicación, fuente de financiamiento y clasificador de ingreso.
 
| Nombre de columna | Tipo de dato | Descripción |
|---|---|---|
| sk_tiempo | INT | Clave foránea a `DIM_TIEMPO`. Mes en que se registra el ingreso. |
| sk_entidad | INT | Clave foránea a `DIM_ENTIDAD`. Unidad ejecutora que percibe el ingreso. |
| sk_ubigeo | INT | Clave foránea a `DIM_UBIGEO`. Ubicación de la unidad ejecutora. |
| sk_financiamiento | INT | Clave foránea a `DIM_FINANCIAMIENTO`. Fuente, rubro y tipo de recurso del ingreso. |
| sk_clasif_ingreso | INT | Clave foránea a `DIM_CLASIF_INGRESO`. Clasificación económica del ingreso. |
| monto_pia | DECIMAL(18,2) | Presupuesto Institucional de Apertura: ingreso estimado aprobado al inicio del año fiscal, en soles. |
| monto_pim | DECIMAL(18,2) | Presupuesto Institucional Modificado: ingreso estimado actualizado tras las modificaciones presupuestales del periodo, en soles. |
| monto_recaudado | DECIMAL(18,2) | Monto efectivamente recaudado o percibido por la entidad en el periodo, en soles. |
 
---
 
### FACT_GASTO
 
 
La tabla de hechos de gasto almacena los montos presupuestados y ejecutados por las entidades públicas en sus distintas fases. Su granularidad es un registro por mes, entidad, ubicación, fuente de financiamiento, clasificador de gasto, función, programa y meta.
 
| Nombre de columna | Tipo de dato | Descripción |
|---|---|---|
| sk_tiempo | INT | Clave foránea a `DIM_TIEMPO`. Mes en que se registra el gasto. |
| sk_entidad | INT | Clave foránea a `DIM_ENTIDAD`. Unidad ejecutora que realiza el gasto. |
| sk_ubigeo_ejec | INT | Clave foránea a `DIM_UBIGEO` (rol: ubicación de la ejecutora). Ubicación de la sede de la unidad ejecutora. |
| sk_ubigeo_meta | INT | Clave foránea a `DIM_UBIGEO` (rol: ubicación de la meta). Departamento donde se ejecuta la meta presupuestal. |
| sk_financiamiento | INT | Clave foránea a `DIM_FINANCIAMIENTO`. Fuente, rubro y tipo de recurso que financia el gasto. |
| sk_clasif_gasto | INT | Clave foránea a `DIM_CLASIF_GASTO`. Clasificación económica del gasto. |
| sk_funcional | INT | Clave foránea a `DIM_FUNCIONAL`. Área de intervención del Estado a la que se orienta el gasto. |
| sk_programa | INT | Clave foránea a `DIM_PROGRAMA`. Programa presupuestal y producto o proyecto asociado. |
| sk_meta | INT | Clave foránea a `DIM_META`. Meta presupuestal y categoría del gasto. |
| monto_pim | DECIMAL(18,2) | Presupuesto Institucional Modificado: presupuesto de gasto vigente tras las modificaciones realizadas durante el año, en soles. |
| monto_devengado | DECIMAL(18,2) | Monto devengado: obligación de pago reconocida tras la conformidad del bien o servicio recibido. Es la medida estándar de ejecución del gasto, en soles. |
| monto_girado | DECIMAL(18,2) | Monto girado: pago efectivamente emitido a favor del acreedor, en soles. |



## Diseño del Data Warehouse
