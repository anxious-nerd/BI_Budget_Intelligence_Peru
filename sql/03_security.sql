/* 03 · Usuarios y permisos.
   Idempotente: crea los logins si no existen y, si existen, les reaplica la contraseña del .env
   (así, para cambiar una contraseña basta con editar .env y volver a correr mssql-init).

   rol_etl       -> usuario ADMIN    : carga, transforma y borra. Es db_owner SOLO de esta base
                                       (no es sysadmin: no ve otras bases ni administra el servidor).
   rol_consulta  -> usuario LECTOR   : solo SELECT sobre los esquemas dw y rpt. No ve etl.
   `sa` queda solo para administrar el servidor: no se usa en el día a día ni se comparte. */

USE [master];
GO

IF SUSER_ID(N'$(MSSQL_ADMIN_USER)') IS NULL
    CREATE LOGIN [$(MSSQL_ADMIN_USER)] WITH PASSWORD = N'$(MSSQL_ADMIN_PASSWORD)',
        DEFAULT_DATABASE = [$(MSSQL_DB)], CHECK_POLICY = ON, CHECK_EXPIRATION = OFF;
ELSE
    ALTER LOGIN [$(MSSQL_ADMIN_USER)] WITH PASSWORD = N'$(MSSQL_ADMIN_PASSWORD)';

IF SUSER_ID(N'$(MSSQL_READER_USER)') IS NULL
    CREATE LOGIN [$(MSSQL_READER_USER)] WITH PASSWORD = N'$(MSSQL_READER_PASSWORD)',
        DEFAULT_DATABASE = [$(MSSQL_DB)], CHECK_POLICY = ON, CHECK_EXPIRATION = OFF;
ELSE
    ALTER LOGIN [$(MSSQL_READER_USER)] WITH PASSWORD = N'$(MSSQL_READER_PASSWORD)';
GO

USE [$(MSSQL_DB)];
GO

IF DATABASE_PRINCIPAL_ID(N'rol_etl')      IS NULL CREATE ROLE rol_etl;
IF DATABASE_PRINCIPAL_ID(N'rol_consulta') IS NULL CREATE ROLE rol_consulta;
GO

/* rol_etl: dueño de la base (crear/alterar tablas y vistas, cargar, truncar particiones). */
ALTER ROLE db_owner ADD MEMBER rol_etl;

/* rol_consulta: leer el modelo y las vistas, ver su definición (para DBeaver / Power BI)
   y ver planes de ejecución. Nada más. */
GRANT SELECT, VIEW DEFINITION ON SCHEMA::dw  TO rol_consulta;
GRANT SELECT, VIEW DEFINITION ON SCHEMA::rpt TO rol_consulta;
GRANT SHOWPLAN TO rol_consulta;
DENY  SELECT, INSERT, UPDATE, DELETE, EXECUTE, VIEW DEFINITION ON SCHEMA::etl TO rol_consulta;
DENY  INSERT, UPDATE, DELETE, ALTER ON SCHEMA::dw  TO rol_consulta;
DENY  INSERT, UPDATE, DELETE, ALTER ON SCHEMA::rpt TO rol_consulta;
GO

IF DATABASE_PRINCIPAL_ID(N'$(MSSQL_ADMIN_USER)') IS NULL
    CREATE USER [$(MSSQL_ADMIN_USER)] FOR LOGIN [$(MSSQL_ADMIN_USER)] WITH DEFAULT_SCHEMA = dw;
IF DATABASE_PRINCIPAL_ID(N'$(MSSQL_READER_USER)') IS NULL
    CREATE USER [$(MSSQL_READER_USER)] FOR LOGIN [$(MSSQL_READER_USER)] WITH DEFAULT_SCHEMA = dw;
GO

ALTER ROLE rol_etl      ADD MEMBER [$(MSSQL_ADMIN_USER)];
ALTER ROLE rol_consulta ADD MEMBER [$(MSSQL_READER_USER)];
GO

PRINT N'Usuarios listos: $(MSSQL_ADMIN_USER) (rol_etl) y $(MSSQL_READER_USER) (rol_consulta).';
GO
