/* 02 · Esquemas y tablas del datamart (modelo del README del equipo).
   Idempotente: crea solo lo que falta; nunca borra datos.

   Esquemas:
     dw   el modelo dimensional (lo que consulta el equipo). Columnas = las del modelo, en su orden.
     rpt  vistas de conveniencia (hechos ya unidos a sus dimensiones).
     etl  tablas técnicas de la carga: mapas de claves, staging y bitácora. El usuario de consulta NO la ve.

   Apartados deliberados de los tipos del modelo (los mismos que documenta el proyecto):
     montos           FLOAT       -> DECIMAL(18,2)   los FLOAT suman con error de redondeo
     latitud/longitud VARCHAR(50) -> DECIMAL(10,7)   como en el diagrama: son números
   Las claves sustitutas sí son INT, como en el modelo: el ETL usa un hash de 64 bits como clave
   de negocio y la carga lo traduce a INT con los mapas etl.key_* (una clave nunca cambia entre cargas). */

USE [$(MSSQL_DB)];
GO

IF SCHEMA_ID(N'dw')  IS NULL EXEC (N'CREATE SCHEMA dw');
IF SCHEMA_ID(N'rpt') IS NULL EXEC (N'CREATE SCHEMA rpt');
IF SCHEMA_ID(N'etl') IS NULL EXEC (N'CREATE SCHEMA etl');
GO

/* Partición por año sobre sk_tiempo (= anio*100 + mes). Permite borrar o recargar un año
   completo al instante (TRUNCATE ... WITH (PARTITIONS ...)) sin tocar los demás. */
IF NOT EXISTS (SELECT 1 FROM sys.partition_functions WHERE name = N'pf_anio')
    CREATE PARTITION FUNCTION pf_anio (INT) AS RANGE RIGHT FOR VALUES (
        201100, 201200, 201300, 201400, 201500, 201600, 201700, 201800, 201900, 202000,
        202100, 202200, 202300, 202400, 202500, 202600, 202700, 202800, 202900, 203000,
        203100, 203200, 203300, 203400, 203500);
IF NOT EXISTS (SELECT 1 FROM sys.partition_schemes WHERE name = N'ps_anio')
    CREATE PARTITION SCHEME ps_anio AS PARTITION pf_anio ALL TO ([PRIMARY]);
GO

/* ------------------------------------------------------------------------------------------
   Dimensiones
   ------------------------------------------------------------------------------------------ */
IF OBJECT_ID(N'dw.dim_tiempo') IS NULL
CREATE TABLE dw.dim_tiempo (
    sk_tiempo INT NOT NULL CONSTRAINT pk_dim_tiempo PRIMARY KEY,
    anio      INT NOT NULL,
    mes       INT NOT NULL      -- 0 = apertura del año fiscal, 1..12 = meses
);

IF OBJECT_ID(N'dw.dim_entidad') IS NULL
CREATE TABLE dw.dim_entidad (
    sk_entidad INT          NOT NULL CONSTRAINT pk_dim_entidad PRIMARY KEY,
    pliego     VARCHAR(150) NULL,
    ejecutora  VARCHAR(150) NULL
);

IF OBJECT_ID(N'dw.dim_ubigeo') IS NULL
CREATE TABLE dw.dim_ubigeo (
    sk_ubigeo     INT           NOT NULL CONSTRAINT pk_dim_ubigeo PRIMARY KEY,
    codigo_ubigeo VARCHAR(6)    NULL,
    departamento  VARCHAR(50)   NULL,
    provincia     VARCHAR(50)   NULL,
    distrito      VARCHAR(50)   NULL,
    latitud       DECIMAL(10,7) NULL,
    longitud      DECIMAL(10,7) NULL
);

IF OBJECT_ID(N'dw.dim_financiamiento') IS NULL
CREATE TABLE dw.dim_financiamiento (
    sk_financiamiento INT          NOT NULL CONSTRAINT pk_dim_financiamiento PRIMARY KEY,
    fuente            VARCHAR(100) NULL,
    rubro             VARCHAR(100) NULL,
    tipo_recurso      VARCHAR(150) NULL
);

IF OBJECT_ID(N'dw.dim_clasif_ingreso') IS NULL
CREATE TABLE dw.dim_clasif_ingreso (
    sk_clasif_ingreso INT          NOT NULL CONSTRAINT pk_dim_clasif_ingreso PRIMARY KEY,
    generica          VARCHAR(100) NULL,
    subgenerica       VARCHAR(100) NULL,
    subgenerica_det   VARCHAR(150) NULL,
    especifica        VARCHAR(150) NULL,
    especifica_det    VARCHAR(200) NULL
);

IF OBJECT_ID(N'dw.dim_clasif_gasto') IS NULL
CREATE TABLE dw.dim_clasif_gasto (
    sk_clasif_gasto INT          NOT NULL CONSTRAINT pk_dim_clasif_gasto PRIMARY KEY,
    generica        VARCHAR(100) NULL,
    subgenerica     VARCHAR(100) NULL,
    subgenerica_det VARCHAR(150) NULL,
    especifica      VARCHAR(150) NULL,
    especifica_det  VARCHAR(200) NULL
);

IF OBJECT_ID(N'dw.dim_funcional') IS NULL
CREATE TABLE dw.dim_funcional (
    sk_funcional       INT          NOT NULL CONSTRAINT pk_dim_funcional PRIMARY KEY,
    funcion            VARCHAR(100) NULL,
    division_funcional VARCHAR(150) NULL,
    grupo_funcional    VARCHAR(150) NULL,
    proyecto           VARCHAR(500) NULL,   -- el modelo decía 250: el MEF trae nombres de hasta 500 (3 % de las metas)
    inversion          VARCHAR(250) NULL,
    meta               VARCHAR(250) NULL
);
GO

-- Bases creadas antes de ampliar `proyecto`: se amplía en el sitio (no pierde datos).
IF COL_LENGTH(N'dw.dim_funcional', N'proyecto') < 500
    ALTER TABLE dw.dim_funcional ALTER COLUMN proyecto VARCHAR(500) NULL;
GO

/* ------------------------------------------------------------------------------------------
   Hechos: columnstore agrupado (compresión ~10x y agregaciones rápidas), particionados por año.
   ------------------------------------------------------------------------------------------ */
IF OBJECT_ID(N'dw.fact_ingreso') IS NULL
BEGIN
    CREATE TABLE dw.fact_ingreso (
        sk_tiempo         INT NOT NULL CONSTRAINT fk_fi_tiempo         REFERENCES dw.dim_tiempo (sk_tiempo),
        sk_entidad        INT NOT NULL CONSTRAINT fk_fi_entidad        REFERENCES dw.dim_entidad (sk_entidad),
        sk_ubigeo         INT NOT NULL CONSTRAINT fk_fi_ubigeo         REFERENCES dw.dim_ubigeo (sk_ubigeo),
        sk_financiamiento INT NOT NULL CONSTRAINT fk_fi_financiamiento REFERENCES dw.dim_financiamiento (sk_financiamiento),
        sk_clasif_ingreso INT NOT NULL CONSTRAINT fk_fi_clasif_ingreso REFERENCES dw.dim_clasif_ingreso (sk_clasif_ingreso),
        monto_pia         DECIMAL(18,2) NOT NULL,
        monto_pim         DECIMAL(18,2) NOT NULL,
        monto_recaudado   DECIMAL(18,2) NOT NULL
    ) ON ps_anio (sk_tiempo);
    CREATE CLUSTERED COLUMNSTORE INDEX cci_fact_ingreso ON dw.fact_ingreso ON ps_anio (sk_tiempo);
END

IF OBJECT_ID(N'dw.fact_gasto') IS NULL
BEGIN
    CREATE TABLE dw.fact_gasto (
        sk_tiempo         INT NOT NULL CONSTRAINT fk_fg_tiempo         REFERENCES dw.dim_tiempo (sk_tiempo),
        sk_entidad        INT NOT NULL CONSTRAINT fk_fg_entidad        REFERENCES dw.dim_entidad (sk_entidad),
        sk_ubigeo_ejec    INT NOT NULL CONSTRAINT fk_fg_ubigeo         REFERENCES dw.dim_ubigeo (sk_ubigeo),
        sk_financiamiento INT NOT NULL CONSTRAINT fk_fg_financiamiento REFERENCES dw.dim_financiamiento (sk_financiamiento),
        sk_clasif_gasto   INT NOT NULL CONSTRAINT fk_fg_clasif_gasto   REFERENCES dw.dim_clasif_gasto (sk_clasif_gasto),
        sk_funcional      INT NOT NULL CONSTRAINT fk_fg_funcional      REFERENCES dw.dim_funcional (sk_funcional),
        monto_pia         DECIMAL(18,2) NOT NULL,
        monto_pim         DECIMAL(18,2) NOT NULL,
        monto_devengado   DECIMAL(18,2) NOT NULL,
        monto_girado      DECIMAL(18,2) NOT NULL
    ) ON ps_anio (sk_tiempo);
    CREATE CLUSTERED COLUMNSTORE INDEX cci_fact_gasto ON dw.fact_gasto ON ps_anio (sk_tiempo);
END
GO

/* ------------------------------------------------------------------------------------------
   etl: mapas de claves (hash del ETL -> INT estable) y bitácora de cargas
   ------------------------------------------------------------------------------------------ */
DECLARE @d SYSNAME, @sql NVARCHAR(MAX);
DECLARE c CURSOR LOCAL FAST_FORWARD FOR
    SELECT v FROM (VALUES (N'entidad'), (N'ubigeo'), (N'financiamiento'),
                          (N'clasif_ingreso'), (N'clasif_gasto'), (N'funcional')) t(v);
OPEN c;
FETCH NEXT FROM c INTO @d;
WHILE @@FETCH_STATUS = 0
BEGIN
    IF OBJECT_ID(N'etl.key_' + @d) IS NULL
    BEGIN
        SET @sql = N'CREATE TABLE etl.key_' + @d + N' (
            nk BIGINT NOT NULL CONSTRAINT pk_key_' + @d + N' PRIMARY KEY,
            sk INT IDENTITY(1,1) NOT NULL CONSTRAINT uq_key_' + @d + N' UNIQUE)';
        EXEC (@sql);
    END
    FETCH NEXT FROM c INTO @d;
END
CLOSE c; DEALLOCATE c;
GO

IF OBJECT_ID(N'etl.load_log') IS NULL
CREATE TABLE etl.load_log (
    id          INT IDENTITY(1,1) NOT NULL CONSTRAINT pk_load_log PRIMARY KEY,
    cargado_en  DATETIME2(0)  NOT NULL CONSTRAINT df_load_log_ts DEFAULT (SYSDATETIME()),
    usuario     SYSNAME       NOT NULL CONSTRAINT df_load_log_usr DEFAULT (SUSER_SNAME()),
    accion      VARCHAR(20)   NOT NULL,     -- 'carga' | 'borrado' | 'dims'
    dataset     VARCHAR(20)   NULL,         -- 'gasto' | 'ingreso'
    anio        INT           NULL,
    filas       BIGINT        NULL,
    detalle     NVARCHAR(4000) NULL
);
GO

/* Borra un año de un hecho al instante (vacía su partición). Uso: EXEC etl.usp_borrar_anio 'gasto', 2023; */
CREATE OR ALTER PROCEDURE etl.usp_borrar_anio
    @dataset   VARCHAR(20),
    @anio      INT,
    @registrar BIT = 1          -- 0 cuando lo llama la recarga de un año (ya deja su propio registro)
AS
BEGIN
    SET NOCOUNT ON;
    IF @dataset NOT IN ('gasto', 'ingreso') THROW 50001, 'dataset debe ser gasto o ingreso', 1;
    IF @anio NOT BETWEEN 2011 AND 2035     THROW 50002, 'anio fuera del rango particionado (2011-2035)', 1;

    DECLARE @p INT = $PARTITION.pf_anio(@anio * 100);
    DECLARE @tabla SYSNAME = CASE @dataset WHEN 'gasto' THEN N'dw.fact_gasto' ELSE N'dw.fact_ingreso' END;
    DECLARE @n BIGINT;
    DECLARE @sql NVARCHAR(400) = N'SELECT @n = COUNT_BIG(*) FROM ' + @tabla
                               + N' WHERE sk_tiempo >= @a * 100 AND sk_tiempo < (@a + 1) * 100';
    EXEC sp_executesql @sql, N'@a INT, @n BIGINT OUTPUT', @a = @anio, @n = @n OUTPUT;

    SET @sql = N'TRUNCATE TABLE ' + @tabla + N' WITH (PARTITIONS (' + CAST(@p AS NVARCHAR(10)) + N'))';
    EXEC (@sql);

    IF @registrar = 1
        INSERT etl.load_log (accion, dataset, anio, filas) VALUES ('borrado', @dataset, @anio, @n);
END
GO

/* ------------------------------------------------------------------------------------------
   rpt: vistas de conveniencia para consultar sin escribir los JOIN
   ------------------------------------------------------------------------------------------ */
CREATE OR ALTER VIEW rpt.v_gasto AS
SELECT  t.anio, t.mes,
        e.pliego, e.ejecutora,
        u.codigo_ubigeo, u.departamento, u.provincia, u.distrito, u.latitud, u.longitud,
        fi.fuente, fi.rubro, fi.tipo_recurso,
        c.generica, c.subgenerica, c.subgenerica_det, c.especifica, c.especifica_det,
        fn.funcion, fn.division_funcional, fn.grupo_funcional, fn.proyecto, fn.inversion, fn.meta,
        f.monto_pia, f.monto_pim, f.monto_devengado, f.monto_girado
FROM dw.fact_gasto f
JOIN dw.dim_tiempo         t  ON t.sk_tiempo          = f.sk_tiempo
JOIN dw.dim_entidad        e  ON e.sk_entidad         = f.sk_entidad
JOIN dw.dim_ubigeo         u  ON u.sk_ubigeo          = f.sk_ubigeo_ejec
JOIN dw.dim_financiamiento fi ON fi.sk_financiamiento = f.sk_financiamiento
JOIN dw.dim_clasif_gasto   c  ON c.sk_clasif_gasto    = f.sk_clasif_gasto
JOIN dw.dim_funcional      fn ON fn.sk_funcional      = f.sk_funcional;
GO

CREATE OR ALTER VIEW rpt.v_ingreso AS
SELECT  t.anio, t.mes,
        e.pliego, e.ejecutora,
        u.codigo_ubigeo, u.departamento, u.provincia, u.distrito, u.latitud, u.longitud,
        fi.fuente, fi.rubro, fi.tipo_recurso,
        c.generica, c.subgenerica, c.subgenerica_det, c.especifica, c.especifica_det,
        f.monto_pia, f.monto_pim, f.monto_recaudado
FROM dw.fact_ingreso f
JOIN dw.dim_tiempo         t  ON t.sk_tiempo          = f.sk_tiempo
JOIN dw.dim_entidad        e  ON e.sk_entidad         = f.sk_entidad
JOIN dw.dim_ubigeo         u  ON u.sk_ubigeo          = f.sk_ubigeo
JOIN dw.dim_financiamiento fi ON fi.sk_financiamiento = f.sk_financiamiento
JOIN dw.dim_clasif_ingreso c  ON c.sk_clasif_ingreso  = f.sk_clasif_ingreso;
GO
