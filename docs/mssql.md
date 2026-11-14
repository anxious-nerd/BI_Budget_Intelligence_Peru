# Datamart en SQL Server (Docker → VM de Azure)

```
 Drive (parquet crudos del MEF)
        │  scripts.download_data
        ▼
 data/raw/ ──► etl.run (DuckDB) ──► warehouse/ (parquet del modelo) ──► etl.load_mssql ──► SQL Server  MEF_DW
                                                                       (usuario ADMIN)          │
                                                                                                ▼
                                                             equipo: DBeaver · Power BI · Python (usuario LECTOR)
```

DuckDB hace la transformación pesada (rápida, en una máquina) y SQL Server **guarda y sirve** el
datamart. Todo corre con `docker compose`: igual en tu PC que en la VM.

| Pieza | Qué es |
|---|---|
| `docker-compose.yml` | `mssql` (SQL Server 2022), `mssql-init` (crea base, modelo y usuarios en cada `up`) y `etl` (herramienta: ETL + carga) |
| `sql/01_database.sql` | base `MEF_DW` (intercalación `Latin1_General_100_CI_AI`, recuperación simple) |
| `sql/02_schema.sql` | esquemas `dw` (modelo), `rpt` (vistas), `etl` (técnico); tablas, particiones por año, procedimiento de borrado |
| `sql/03_security.sql` | usuarios ADMIN y LECTOR con sus roles |
| `etl/load_mssql.py` | carga el `warehouse/` a SQL Server y cuadra montos |
| `scripts/download_data.py` | baja los parquet de Drive a `data/raw/` |

## Usuarios

| Usuario (`.env`) | Rol | Puede | No puede |
|---|---|---|---|
| `mef_admin` | `rol_etl` (db_owner **solo** de `MEF_DW`) | cargar, transformar (crear tablas/vistas), borrar | crear bases, administrar el servidor |
| `mef_lector` | `rol_consulta` | `SELECT` en `dw` y `rpt`, ver planes de ejecución | escribir, borrar, ver el esquema `etl` |
| `sa` | sysadmin | todo | — **no se usa en el día a día ni se comparte** |

Todo esto está probado: el lector recibe "permission denied" al intentar INSERT, DELETE, TRUNCATE, CREATE TABLE o
ejecutar el procedimiento de borrado; el admin no puede crear otra base.

**Otro lector** (uno por persona, por ejemplo), como `sa` o admin:

```sql
CREATE LOGIN ximena WITH PASSWORD = N'...', DEFAULT_DATABASE = MEF_DW;   -- en master (solo sa)
USE MEF_DW;
CREATE USER ximena FOR LOGIN ximena WITH DEFAULT_SCHEMA = dw;
ALTER ROLE rol_consulta ADD MEMBER ximena;
```

---

## 1 · Levantarlo (en tu PC o en la VM)

Requisitos: Docker con el plugin `compose`, máquina **x86_64** (la imagen de SQL Server no existe para ARM),
~6 GB de RAM libres y ~15 GB de disco.

```bash
cp .env.example .env
# edita .env: las tres contraseñas (8+ caracteres, mayúsculas, minúsculas, números y símbolos; sin ')
#             MEF_UID / MEF_GID con la salida de `id -u` y `id -g`
docker compose up -d
docker compose logs mssql-init     # debe terminar en "Inicialización terminada."
```

`mssql-init` corre en cada `up` y es idempotente: no borra datos. Para **cambiar una contraseña**, edítala en
`.env` y corre `docker compose up -d` otra vez.

Los datos viven en el volumen `mef_mssql_data`: sobreviven a `docker compose down`. Solo
`docker compose down -v` los borra.

## 2 · Poblar el datamart (usuario ADMIN)

```bash
docker compose build etl                                         # una vez (y al cambiar el código)
docker compose run --rm etl python -m scripts.download_data      # ~1.4 GB desde Drive
docker compose run --rm etl python -m etl.profile                # diagnóstico (ver abajo)
docker compose run --rm etl python -m etl.run                    # parquet crudo -> warehouse/
docker compose run --rm etl python -m etl.validate
docker compose run --rm etl python -m etl.load_mssql             # warehouse/ -> SQL Server
```

La carga termina con un cuadre por año (filas y suma de cada monto, SQL Server contra parquet). Debe decir
`todo OK`; si no, el comando devuelve error.

### Configuración que pide el dato real

Ya se corrió `etl.profile` sobre los parquet del Drive (2023-2026). Pon esto en `.env` **antes** de `etl.run`:

```ini
MEF_EXEC_MODE=flow               # ¡no el valor por defecto! Los montos de ejecución vienen como flujo del mes
MEF_BUDGET_MODE_GASTO=snapshot   # PIA/PIM del gasto solo en el mes 0 (apertura)
MEF_BUDGET_MODE_INGRESO=sum      # PIA en el mes 1, PIM como modificaciones mensuales
MEF_UBIGEO_CODE=inei             # 99.8 % de las filas cruzan con el código INEI (RENIEC: 65-68 %)
```

Por qué `flow`: el devengado de 2026 sumando los meses da S/ 174.8 mil millones frente a un PIM de 286.8 mil millones
(61 % de avance a agosto, razonable); leído como acumulado daría 10 %. Además ~49 % de las series bajan algún mes, lo
que un acumulado no hace. **Confírmalo una vez** contra Consulta Amigable (devengado total de 2025) y guarda la captura
para la exposición.

### Operaciones del día a día

```bash
# llegó un mes nuevo de 2026: rehacer solo ese año
docker compose run --rm etl python -m etl.run --years 2026
docker compose run --rm etl python -m etl.load_mssql --years 2026

# borrar un año del datamart (instantáneo: vacía su partición)
docker compose run --rm etl python -m etl.load_mssql --delete --dataset gasto --years 2023

# ¿SQL Server sigue igual que los parquet?
docker compose run --rm etl python -m etl.load_mssql --check

# vaciar todo (incluidas las claves) y recargar
docker compose run --rm etl python -m etl.load_mssql --full
```

Desde SQL, el admin también puede borrar un año: `EXEC etl.usp_borrar_anio 'gasto', 2023;` y revisar el historial
en `SELECT * FROM etl.load_log ORDER BY id DESC;`.

**Qué garantiza la carga**
- Las claves sustitutas son `INT`, como pide el modelo, y **estables**: recargar un año no cambia ninguna
  (`etl.key_*` traduce el hash del ETL a un INT la primera vez y lo recuerda).
- Cada año se carga en **una transacción** (vaciar partición + insertar): si algo falla, el año queda como estaba.
- Antes de insertar se rechaza el año si alguna fila apunta a un miembro de dimensión inexistente o cae fuera del año,
  y si algún texto no cabe en el `VARCHAR(n)` del modelo.
- Las dimensiones son Tipo 1 (gana el nombre más reciente) y nunca se borran miembros.

## 3 · Cómo se conecta el equipo (usuario LECTOR)

| Dato | Valor |
|---|---|
| Servidor | IP o nombre de la VM, puerto `1433` |
| Base | `MEF_DW` |
| Usuario / clave | `mef_lector` / la de `.env` |
| Certificado | autofirmado: marcar **"Trust server certificate"** (`TrustServerCertificate=yes`) |

- **DBeaver / Azure Data Studio / SSMS**: conexión SQL Server con esos datos.
- **Power BI**: Obtener datos → SQL Server → servidor `IP,1433`, base `MEF_DW`, modo DirectQuery o Importar,
  credenciales de *Base de datos*.
- **Python**: `pymssql.connect(server="IP", port=1433, user="mef_lector", password="...", database="MEF_DW")`.

Para consultar sin escribir los JOIN están las vistas `rpt.v_gasto` y `rpt.v_ingreso`:

```sql
-- avance de ejecución por departamento, 2025
SELECT departamento,
       SUM(monto_devengado) / NULLIF(SUM(monto_pim), 0) AS avance
FROM rpt.v_gasto
WHERE anio = 2025
GROUP BY departamento
ORDER BY avance DESC;
```

Las búsquedas no distinguen mayúsculas ni tildes: `WHERE departamento = 'junin'` encuentra `JUNÍN`.

**Ingreso vs gasto: el ingreso NO trae Recursos Ordinarios.** Los RO (impuestos que recauda SUNAT para el Tesoro, ~61 %
del presupuesto) no se registran como ingreso de ninguna entidad. En 2025, en las otras cuatro fuentes el PIM de ingreso y
el de gasto coinciden exactamente; en RO, el gasto tiene S/ 166 mil millones y el ingreso nada:

| Fuente (2025, miles de millones S/) | PIM gasto | PIM ingreso | Recaudado |
|---|---:|---:|---:|
| Recursos Ordinarios | 166.4 | — | — |
| Recursos Determinados | 53.9 | 53.9 | 59.8 |
| Operaciones Oficiales de Crédito | 34.5 | 34.5 | 36.5 |
| Recursos Directamente Recaudados | 10.9 | 10.9 | 14.9 |
| Donaciones y Transferencias | 6.8 | 6.8 | 8.0 |

Por eso el control "cuadre PIM ingreso vs gasto" de `etl.validate` sale con ~61 % de diferencia: no es un error. Para
comparar "lo que una región recauda frente a lo que ejecuta", filtra **`fuente <> 'RECURSOS ORDINARIOS'`** en ambos
lados (o muestra los RO aparte, como transferencia del Tesoro).

**Ojo con PIA y PIM** (son *semiaditivos*): en el gasto están solo en el mes 0, así que `SUM` sobre el año da el
vigente; pero en el ingreso el PIM viene como modificaciones mensuales y el PIA en el mes 1. Nunca promedies ni
sumes PIM entre años distintos.

## Medido con los datos reales (Drive, 2023-2026)

Máquina de prueba: 2 núcleos, 7 GB de RAM, SQL Server limitado a 2.5–4 GB.

| Etapa | Resultado |
|---|---|
| `download_data` | 1.4 GB en ~1 min |
| `etl.run` (4 años) | 4 min · `fact_gasto` 41.1 M filas (~11 M/año) · `fact_ingreso` 2.9 M · `dim_funcional` 1.05 M metas |
| `etl.validate` | 0 FAIL · WARN esperados: cuadre PIM (ver Recursos Ordinarios), 18 de 1,892 distritos sin coordenadas (0.2 % del devengado), 3 `SEC_EJEC` con nombres distintos |
| `etl.load_mssql` | ~4.5 min por año de gasto (~64 mil filas/s), ~15 s por año de ingreso; **~20 min todo** |
| Tamaño en SQL Server | ~0.95 GB (columnstore comprime los 41 M de filas) |
| Consulta típica (avance por departamento de un año) | 0.3–0.4 s con caché; ~15 s la primera vez tras reiniciar |
| Cuadre SQL Server vs parquet | 8 de 8 año-dataset OK, al centavo |

Se probó también una caída a mitad de la carga (reinicio de la máquina mientras subía gasto 2026): ese año quedó
vacío, sin filas a medias, y `--only facts --dataset gasto --years 2026` lo completó.

**Cambio respecto al modelo:** `dim_funcional.proyecto` pasa de `VARCHAR(250)` a `VARCHAR(500)`: el MEF trae nombres de
hasta 500 caracteres en 32,693 metas (3 %). Ya está en `sql/02_schema.sql` y en `docs/modelo_objetivo.md`; falta
reflejarlo en el README del repo y en el diagrama.

## 4 · Montarlo en una VM de Azure

1. **VM**: Ubuntu 24.04 LTS, x86_64. Tamaño mínimo razonable `Standard_B2ms` (2 vCPU, 8 GB); con `Standard_D4s_v5`
   (4 vCPU, 16 GB) el ETL corre bastante más rápido. Disco de 64 GB o más.
2. **Docker**: `curl -fsSL https://get.docker.com | sh && sudo usermod -aG docker $USER` (cierra y abre sesión).
3. **Código**: `git clone` del repo, `cp .env.example .env`, contraseñas **nuevas** (no las de tu PC).
4. **Red**: elige una de dos.
   - **Recomendado: túnel SSH.** Deja `MSSQL_BIND=127.0.0.1` y no abras el 1433. Cada persona se conecta con
     `ssh -N -L 1433:localhost:1433 usuario@IP-de-la-VM` y usa `localhost,1433` en su cliente. SQL Server nunca queda
     expuesto a internet.
   - **Puerto abierto.** `MSSQL_BIND=0.0.0.0` y en el *Network Security Group* de la VM una regla de entrada al 1433
     **solo desde las IP del equipo** (nunca `Any`): un SQL Server abierto recibe ataques de fuerza bruta a `sa` en
     minutos.
5. `docker compose up -d` y luego el Paso 2. Corre la carga **dentro de la VM** (no desde tu laptop): subir
   millones de filas por internet es mucho más lento.
6. **Costo**: apaga (*Stop/Deallocate*) la VM cuando no se use; el disco se cobra igual, pero el cómputo no.

**Licencia**: `MSSQL_PID=Developer` es gratis para desarrollo y pruebas (no para producción); un proyecto de curso
encaja. `Express` también es gratis, pero limita cada base a 10 GB.

**Respaldo** (opcional, el datamart se puede reconstruir desde Drive):

```bash
docker compose exec mssql /opt/mssql-tools18/bin/sqlcmd -S localhost -U sa -P "$MSSQL_SA_PASSWORD" -C \
  -Q "BACKUP DATABASE MEF_DW TO DISK = '/var/opt/mssql/data/MEF_DW.bak' WITH INIT, COMPRESSION"
```

## Solución de problemas

| Síntoma | Causa | Qué hacer |
|---|---|---|
| `mssql` no pasa a *healthy* | contraseña de `sa` débil o poca RAM | revisa `docker compose logs mssql` |
| `mssql-init` sale con error | contraseña con `'` o que no cumple la política | cámbiala en `.env` y `docker compose up -d` |
| `no pude conectar a ...` | carga corriendo fuera de Docker con `MSSQL_HOST` equivocado | dentro del compose es `mssql`; fuera, `localhost` o la IP |
| `filas apuntan a un miembro de dim ... que no existe` | `--only facts` sin haber cargado dims | corre sin `--only` |
| `textos más largos que el VARCHAR del modelo` | un nombre del MEF no cabe en el modelo | amplía ese `VARCHAR` en `sql/02_schema.sql` (y en el README) |
| El cliente rechaza el certificado | certificado autofirmado | activa *Trust server certificate* |
| `exec format error` al levantar | máquina ARM (Mac M1/M2, Orange Pi) | usa una máquina x86_64 o la VM |
