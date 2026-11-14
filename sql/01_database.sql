/* 01 · Base de datos del datamart.
   Lo ejecuta docker/mssql-init.sh como `sa`. Es idempotente: si la base ya existe no la toca.

   Intercalación Latin1_General_100_CI_AI:
     - página de códigos 1252: VARCHAR(n) guarda n caracteres, tildes y ñ incluidas
       (con una intercalación UTF-8, VARCHAR(n) cuenta BYTES y "Educación" ocuparía 10);
     - CI_AI: las búsquedas no distinguen mayúsculas ni tildes ('junin' encuentra 'JUNÍN').
   RECOVERY SIMPLE: el datamart se reconstruye desde los parquet, no necesita copias del log. */

IF DB_ID(N'$(MSSQL_DB)') IS NULL
BEGIN
    PRINT N'Creando base $(MSSQL_DB)';
    EXEC (N'CREATE DATABASE [$(MSSQL_DB)] COLLATE Latin1_General_100_CI_AI');
END
ELSE
    PRINT N'La base $(MSSQL_DB) ya existe: no se modifica.';
GO

ALTER DATABASE [$(MSSQL_DB)] SET RECOVERY SIMPLE;
GO
