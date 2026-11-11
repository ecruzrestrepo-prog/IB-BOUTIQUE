#!/usr/bin/env python3
"""Administración explícita del esquema de la tienda; nunca la ejecuta Streamlit.

    python tools/admin_oracle.py preflight
    python tools/admin_oracle.py backup
    python tools/admin_oracle.py migrate --backup-dir backups/<directorio>
    python tools/admin_oracle.py verify
    python tools/admin_oracle.py restore --backup-dir backups/<directorio>

Solo .env aporta configuración. migrate verifica el respaldo antes del primer
DROP; restore exige que ninguna tabla respaldada exista. El DDL Oracle tiene
commit implícito: un fallo de DDL necesita reparación o restauración explícita.
"""
from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

import oracledb
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_OWNER = "EMILIO_CRUZ10"
LEGACY_TABLES = frozenset({"KARDEX", "ESTUDIANTE", "ASIGNATURAS", "PROFESOR",
                           "PLAN", "PROGRAMA", "ENTRENAMIENTO_CLIENTES"})
NEW_TABLES = frozenset({"DIM_CLIENTES", "DIM_PRODUCTOS", "DIM_FECHAS", "PEDIDOS",
                        "HECHOS_PEDIDOS"})
SCRIPT_NAMES = ("00_limpiar_kardex.sql", "01_crear_modelo.sql",
                "02_datos_ejemplo.sql", "03_verificar.sql")


class AdminError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    user: str
    password: str = field(repr=False)
    dsn: str = field(repr=False)
    owner: str
    wallet_dir: str
    wallet_key: str = field(repr=False)
    mode: str = "thin"
    lib_dir: str = ""
    connect_timeout: int = 10
    query_timeout: int = 30


def configuration() -> Config:
    values = dotenv_values(ROOT / ".env", interpolate=False)

    def val(key: str, default: str = "") -> str:
        return str(values.get(key) or default).strip()

    def secret(key: str) -> str:
        value = values.get(key)
        return "" if value is None else str(value)

    def path(key: str) -> str:
        value = val(key)
        if not value:
            return ""
        p = Path(value).expanduser()
        return str(p if p.is_absolute() else ROOT / p)

    user = val("USER_DB").strip('"')
    owner = val("DB_SCHEMA", user).strip('"').upper()
    result = Config(user, secret("DB_PASSWORD"), val("DB_DSN"), owner,
                    path("WALLET_DIR"), secret("WALLET_KEY"),
                    val("ORACLE_MODE", "thin").lower(), path("ORACLE_CLIENT_LIB_DIR"),
                    int(val("CONNECT_TIMEOUT_SECONDS", "10")),
                    int(val("QUERY_TIMEOUT_SECONDS", "30")))
    if not result.user or not result.password or not result.dsn:
        raise AdminError("Completa USER_DB, DB_PASSWORD y DB_DSN en .env.")
    if result.owner != EXPECTED_OWNER or result.user.upper() != EXPECTED_OWNER:
        raise AdminError(f"Esta herramienta solo administra {EXPECTED_OWNER} con su propio usuario.")
    if result.mode not in {"thin", "thick"}:
        raise AdminError("ORACLE_MODE debe ser thin o thick.")
    if not 1 <= result.connect_timeout <= 120 or not 1 <= result.query_timeout <= 300:
        raise AdminError("Los límites de conexión y consulta de .env no son válidos.")
    return result


def connect(config: Config):
    oracledb.defaults.fetch_decimals = True
    if config.mode == "thick":
        oracledb.init_oracle_client(lib_dir=config.lib_dir or None,
                                   config_dir=config.wallet_dir or None)
    params = oracledb.ConnectParams(user=config.user, password=config.password,
                                    config_dir=config.wallet_dir or None)
    params.parse_connect_string(config.dsn)
    params.set(tcp_connect_timeout=config.connect_timeout, retry_count=0, retry_delay=0)
    if config.mode == "thin" and config.wallet_dir:
        params.set(wallet_location=config.wallet_dir, wallet_password=config.wallet_key or None)
    conn = oracledb.connect(params=params)
    conn.call_timeout = config.query_timeout * 1000
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT USER, SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA') FROM DUAL")
            if tuple(cur.fetchone()) != (EXPECTED_OWNER, EXPECTED_OWNER):
                raise AdminError("La sesión pertenece a otro usuario o esquema; no se continuará.")
    except Exception:
        conn.close()
        raise
    return conn


def ident(value: str) -> str:
    if not re.fullmatch(r"[A-Z][A-Z0-9_$#]*", value):
        raise AdminError("El catálogo contiene un identificador no admitido por esta migración.")
    return '"' + value + '"'


def target(table: str) -> str:
    return ident(EXPECTED_OWNER) + "." + ident(table)


def rows(conn, sql: str, **binds) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(sql, binds)
        names = [d[0].lower() for d in cur.description]
        return [dict(zip(names, row)) for row in cur.fetchall()]


def inventory(conn) -> dict[str, Any]:
    owner = EXPECTED_OWNER
    result = {
        "owner": owner,
        "objects": rows(conn, "SELECT OBJECT_NAME, OBJECT_TYPE, STATUS, GENERATED FROM USER_OBJECTS ORDER BY OBJECT_TYPE, OBJECT_NAME"),
        "tables": rows(conn, "SELECT TABLE_NAME FROM USER_TABLES ORDER BY TABLE_NAME"),
        "columns": rows(conn, "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_ID, DATA_TYPE, DATA_LENGTH, DATA_PRECISION, DATA_SCALE, NULLABLE, VIRTUAL_COLUMN FROM USER_TAB_COLS WHERE HIDDEN_COLUMN='NO' ORDER BY TABLE_NAME, COLUMN_ID"),
        "identities": rows(conn, "SELECT TABLE_NAME, COLUMN_NAME, GENERATION_TYPE, SEQUENCE_NAME FROM USER_TAB_IDENTITY_COLS ORDER BY TABLE_NAME, COLUMN_NAME"),
        "constraints": rows(conn, "SELECT TABLE_NAME, CONSTRAINT_NAME, CONSTRAINT_TYPE, STATUS, VALIDATED, R_OWNER, R_CONSTRAINT_NAME, DELETE_RULE, INDEX_NAME FROM USER_CONSTRAINTS ORDER BY TABLE_NAME, CONSTRAINT_NAME"),
        "indexes": rows(conn, "SELECT INDEX_NAME, TABLE_OWNER, TABLE_NAME, INDEX_TYPE, GENERATED FROM USER_INDEXES ORDER BY INDEX_NAME"),
        "triggers": rows(conn, "SELECT TRIGGER_NAME, TABLE_OWNER, TABLE_NAME, STATUS FROM USER_TRIGGERS ORDER BY TRIGGER_NAME"),
        "lobs": rows(conn, "SELECT TABLE_NAME, COLUMN_NAME, SEGMENT_NAME, INDEX_NAME FROM USER_LOBS ORDER BY TABLE_NAME, COLUMN_NAME"),
        "foreign_keys": rows(conn, """SELECT C.OWNER, C.TABLE_NAME, C.CONSTRAINT_NAME,
            P.OWNER AS PARENT_OWNER, P.TABLE_NAME AS PARENT_TABLE
            FROM ALL_CONSTRAINTS C JOIN ALL_CONSTRAINTS P
              ON C.R_OWNER=P.OWNER AND C.R_CONSTRAINT_NAME=P.CONSTRAINT_NAME
            WHERE C.CONSTRAINT_TYPE='R' AND (C.OWNER=:owner OR P.OWNER=:owner)
            ORDER BY C.OWNER,C.TABLE_NAME,C.CONSTRAINT_NAME""", owner=owner),
        "dependencies": rows(conn, """SELECT OWNER, NAME, TYPE, REFERENCED_OWNER,
            REFERENCED_NAME, REFERENCED_TYPE FROM ALL_DEPENDENCIES
            WHERE OWNER=:owner OR REFERENCED_OWNER=:owner
            ORDER BY OWNER,NAME,TYPE,REFERENCED_OWNER,REFERENCED_NAME""", owner=owner),
        "privileges": rows(conn, "SELECT PRIVILEGE FROM SESSION_PRIVS ORDER BY PRIVILEGE"),
        "roles": rows(conn, "SELECT ROLE FROM SESSION_ROLES ORDER BY ROLE"),
        "quota": rows(conn, "SELECT TABLESPACE_NAME, BYTES, MAX_BYTES FROM USER_TS_QUOTAS ORDER BY TABLESPACE_NAME"),
        "recyclebin": rows(conn, "SELECT OBJECT_NAME, ORIGINAL_NAME, TYPE FROM USER_RECYCLEBIN ORDER BY OBJECT_NAME"),
    }
    result["counts"] = {
        row["table_name"]: int(rows(conn, f"SELECT COUNT(*) AS N FROM {target(row['table_name'])}")[0]["n"])
        for row in result["tables"] if row["table_name"] in LEGACY_TABLES | NEW_TABLES
    }
    return result


def migration_problems(info: dict[str, Any]) -> list[str]:
    problems = []
    tables = {row["table_name"] for row in info["tables"]}
    unexpected = tables - LEGACY_TABLES
    if unexpected:
        problems.append("Tablas fuera de la lista de limpieza: " + ", ".join(sorted(unexpected)))
    allowed = {(name, "TABLE") for name in tables & LEGACY_TABLES}
    allowed.update((r["index_name"], "INDEX") for r in info["indexes"]
                   if r["table_owner"] == EXPECTED_OWNER and r["table_name"] in LEGACY_TABLES)
    allowed.update((r["trigger_name"], "TRIGGER") for r in info["triggers"]
                   if r["table_owner"] == EXPECTED_OWNER and r["table_name"] in LEGACY_TABLES)
    allowed.update((r["sequence_name"], "SEQUENCE") for r in info["identities"]
                   if r["table_name"] in LEGACY_TABLES)
    allowed.update((r["segment_name"], "LOB") for r in info["lobs"]
                   if r["table_name"] in LEGACY_TABLES)
    recycled = {r["object_name"] for r in info["recyclebin"]}
    unknown = {(r["object_name"], r["object_type"]) for r in info["objects"]
               if (r["object_name"], r["object_type"]) not in allowed
               and r["object_name"] not in recycled}
    if unknown:
        problems.append("Objetos fuera de la lista de limpieza: " + ", ".join(f"{kind} {name}" for name, kind in sorted(unknown)))
    for fk in info["foreign_keys"]:
        if (fk["owner"] != EXPECTED_OWNER or fk["parent_owner"] != EXPECTED_OWNER
                or fk["table_name"] not in LEGACY_TABLES or fk["parent_table"] not in LEGACY_TABLES):
            problems.append(f"FK fuera del modelo antiguo: {fk['owner']}.{fk['constraint_name']}")
    allowed_names = {name for name, _ in allowed}
    for dep in info["dependencies"]:
        if dep["referenced_owner"] == EXPECTED_OWNER and dep["referenced_name"] in allowed_names:
            if dep["owner"] != EXPECTED_OWNER or dep["name"] not in allowed_names:
                problems.append(f"Dependencia ajena: {dep['owner']}.{dep['name']}")
    privileges = {r["privilege"] for r in info["privileges"]}
    for required in ("CREATE SESSION", "CREATE TABLE"):
        if required not in privileges:
            problems.append("Falta privilegio " + required)
    return problems


def require_migratable(info):
    problems = migration_problems(info)
    if problems:
        raise AdminError("Migración detenida: " + " | ".join(problems))


def encode(value):
    if isinstance(value, oracledb.LOB):
        value = value.read()
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, Decimal):
        return {"$type": "decimal", "value": str(value)}
    if isinstance(value, datetime):
        return {"$type": "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {"$type": "date", "value": value.isoformat()}
    if isinstance(value, bytes):
        return {"$type": "bytes", "value": base64.b64encode(value).decode("ascii")}
    if isinstance(value, float):
        return {"$type": "float", "value": value.hex()}
    raise AdminError(f"Tipo no respaldable: {type(value).__name__}; no se borrará nada.")


def decode(value):
    if not isinstance(value, dict):
        return value
    converters = {"decimal": Decimal, "datetime": datetime.fromisoformat,
                  "date": date.fromisoformat, "bytes": base64.b64decode,
                  "float": float.fromhex}
    kind = value.get("$type")
    if kind not in converters or set(value) != {"$type", "value"}:
        raise AdminError("Valor inválido en el respaldo.")
    return converters[kind](value["value"])


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=encode)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def private_write(path: Path, content: str):
    with path.open("x", encoding="utf-8") as handle:
        os.chmod(path, 0o600)
        handle.write(content)


def table_data(conn, table: str, columns: list[str], destination: Path | None = None):
    row_hashes = []
    handle = destination.open("x", encoding="utf-8") if destination else None
    try:
        if destination:
            os.chmod(destination, 0o600)
        with conn.cursor() as cur:
            cur.arraysize = 500
            cur.execute(f"SELECT {', '.join(map(ident, columns))} FROM {target(table)}")
            while batch := cur.fetchmany(500):
                for row in batch:
                    line = canonical([encode(v) for v in row])
                    row_hashes.append(hashlib.sha256(line.encode("utf-8")).hexdigest())
                    if handle:
                        handle.write(line + "\n")
    finally:
        if handle:
            handle.close()
    return {"rows": len(row_hashes), "data_sha256": digest(sorted(row_hashes))}


def get_ddl(conn, kind: str, name: str) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT DBMS_METADATA.GET_DDL(:kind,:name,:owner) FROM DUAL",
                    kind=kind, name=name, owner=EXPECTED_OWNER)
        value = cur.fetchone()[0]
        return value.read() if isinstance(value, oracledb.LOB) else value


def table_order(info) -> list[str]:
    remaining = {r["table_name"] for r in info["tables"]}
    parents = {name: set() for name in remaining}
    for fk in info["foreign_keys"]:
        if fk["table_name"] in parents and fk["parent_table"] != fk["table_name"]:
            parents[fk["table_name"]].add(fk["parent_table"])
    result = []
    while remaining:
        ready = sorted(name for name in remaining if not (parents[name] & remaining))
        if not ready:
            raise AdminError("El esquema tiene FKs cíclicas no previstas; revisar antes de respaldar.")
        result.extend(ready)
        remaining.difference_update(ready)
    return result


def make_backup(conn) -> Path:
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute("SET TRANSACTION READ ONLY")
    info = inventory(conn)
    require_migratable(info)
    base = ROOT / "backups"
    base.mkdir(mode=0o700, exist_ok=True)
    folder = base / datetime.now(timezone.utc).strftime("kardex_%Y%m%dT%H%M%S_%fZ")
    folder.mkdir(mode=0o700)
    manifest = {"format_version": 1, "owner": EXPECTED_OWNER,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "inventory": info, "tables": [], "extra_ddl": [], "complete": False}
    try:
        for table in table_order(info):
            columns = [r["column_name"] for r in info["columns"]
                       if r["table_name"] == table and r["virtual_column"] == "NO"]
            ddl = get_ddl(conn, "TABLE", table)
            private_write(folder / f"{table}.sql", ddl)
            result = table_data(conn, table, columns, folder / f"{table}.jsonl")
            manifest["tables"].append({"name": table, "columns": columns,
                                        "ddl_file": f"{table}.sql", "data_file": f"{table}.jsonl",
                                        "ddl_sha256": hashlib.sha256(ddl.encode("utf-8")).hexdigest(), **result})
        constraint_indexes = {r["index_name"] for r in info["constraints"] if r["index_name"]}
        extras = [("INDEX", r["index_name"]) for r in info["indexes"]
                  if r["index_name"] not in constraint_indexes and r["index_type"] != "LOB"]
        extras += [("TRIGGER", r["trigger_name"]) for r in info["triggers"]]
        for kind, name in extras:
            filename = f"{kind}_{name}.sql"
            private_write(folder / filename, get_ddl(conn, kind, name))
            manifest["extra_ddl"].append({"kind": kind, "name": name, "file": filename})
        manifest["files"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir() if p.is_file()}
        manifest["complete"] = True
        private_write(folder / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2, default=encode) + "\n")
        load_backup(folder)
        return folder
    except Exception:
        # Un respaldo parcial no recibe manifest completo y jamás permite migrate.
        raise
    finally:
        conn.rollback()


def load_backup(folder: Path):
    folder = folder.resolve()
    try:
        data = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        if data.get("format_version") != 1 or data.get("owner") != EXPECTED_OWNER or data.get("complete") is not True:
            raise AdminError("El respaldo no está completo o corresponde a otro esquema.")
        for name, expected in data["files"].items():
            if Path(name).name != name or (folder / name).is_symlink():
                raise AdminError("Ruta no válida en el respaldo.")
            if hashlib.sha256((folder / name).read_bytes()).hexdigest() != expected:
                raise AdminError("El respaldo no pasó la verificación de integridad: " + name)
        for table in data["tables"]:
            if table["name"] not in LEGACY_TABLES:
                raise AdminError("El respaldo contiene una tabla fuera del modelo antiguo.")
            for key in ("ddl_file", "data_file"):
                if table[key] not in data["files"]:
                    raise AdminError("Un archivo del respaldo no tiene hash de integridad.")
            for column in table["columns"]:
                ident(column)
            row_hashes = []
            for line in (folder / table["data_file"]).read_text(encoding="utf-8").splitlines():
                values = json.loads(line)
                if not isinstance(values, list) or len(values) != len(table["columns"]):
                    raise AdminError("Número de columnas inválido en el respaldo.")
                [decode(value) for value in values]
                row_hashes.append(hashlib.sha256(line.encode("utf-8")).hexdigest())
            if len(row_hashes) != table["rows"] or digest(sorted(row_hashes)) != table["data_sha256"]:
                raise AdminError("El conteo o hash de filas no coincide en " + table["name"])
        for item in data["extra_ddl"]:
            if item["kind"] not in {"INDEX", "TRIGGER"} or item["file"] not in data["files"]:
                raise AdminError("DDL adicional inválido en el respaldo.")
        return data
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise AdminError("Respaldo ausente, incompleto o inválido.") from exc


def split_sql(text: str) -> list[str]:
    """Lee nuestros scripts SQL/SQLPlus: SQL con ; y bloques con / en línea sola."""
    output, buffer = [], []
    quote = None
    block_comment = False
    plsql = False
    for line in text.splitlines(keepends=True):
        if line.strip() == "/" and quote is None and not block_comment:
            statement = "".join(buffer).strip()
            if statement:
                output.append(statement)
            buffer, plsql = [], False
            continue
        i = 0
        while i < len(line):
            char = line[i]
            nxt = line[i + 1:i + 2]
            if block_comment:
                if char == "*" and nxt == "/":
                    block_comment = False
                    i += 2
                    buffer.append(" ")
                    continue
            elif quote:
                buffer.append(char)
                if char == quote:
                    if nxt == quote:
                        buffer.append(nxt)
                        i += 2
                        continue
                    quote = None
            elif char == "-" and nxt == "-":
                buffer.append("\n")
                break
            elif char == "/" and nxt == "*":
                block_comment = True
                i += 2
                continue
            elif char in {"'", '"'}:
                quote = char
                buffer.append(char)
            elif char == ";" and not plsql:
                statement = "".join(buffer).strip()
                if statement:
                    output.append(statement)
                buffer = []
            else:
                buffer.append(char)
                start = "".join(buffer).lstrip().upper()
                if re.match(r"^(DECLARE|BEGIN)\s", start) or re.match(r"^CREATE\s+(OR\s+REPLACE\s+)?(TRIGGER|PROCEDURE|FUNCTION|PACKAGE|TYPE)\s", start):
                    plsql = True
            i += 1
    if quote or block_comment:
        raise AdminError("Script SQL incompleto: comillas o comentario sin cerrar.")
    remaining = "".join(buffer).strip()
    if remaining:
        if plsql:
            raise AdminError("Bloque PL/SQL sin terminador / en línea independiente.")
        output.append(remaining)
    return output


def execute_script(conn, path: Path, *, transactional=False):
    statements = split_sql(path.read_text(encoding="utf-8"))
    if not statements:
        raise AdminError("Script vacío: " + path.name)
    with conn.cursor() as cur:
        for number, sql in enumerate(statements, 1):
            first = sql.split(None, 1)[0].upper()
            if first in {"SET", "SPOOL", "WHENEVER", "CONNECT", "EXIT", "HOST", "PROMPT", "@"}:
                raise AdminError("Directiva SQLPlus no admitida en " + path.name)
            if transactional and first not in {"INSERT", "UPDATE", "DELETE", "MERGE", "SELECT", "WITH", "DECLARE", "BEGIN"}:
                raise AdminError("El script transaccional contiene DDL o control de transacción: " + path.name)
            try:
                cur.execute(sql)
                if cur.description:
                    result = cur.fetchmany(101)
                    print(canonical({"script": path.name, "statement": number,
                                     "columns": [d[0] for d in cur.description], "rows": result[:100]}))
            except oracledb.Error as exc:
                detail = getattr(exc.args[0], "full_code", "Oracle") if exc.args else "Oracle"
                raise AdminError(f"{path.name}, sentencia {number}: {detail}. La operación se detuvo; revisar el estado antes de reintentar.") from exc


def verify(conn, *, seed=False):
    actual = {r["table_name"] for r in rows(conn, "SELECT TABLE_NAME FROM USER_TABLES")}
    if actual != NEW_TABLES:
        raise AdminError("El esquema no contiene exclusivamente la transición esperada del kardex a la tienda.")
    invalid = rows(conn, "SELECT TABLE_NAME, CONSTRAINT_NAME FROM USER_CONSTRAINTS WHERE STATUS <> 'ENABLED' OR VALIDATED <> 'VALIDATED'")
    if invalid:
        raise AdminError("Hay restricciones deshabilitadas o sin validar.")
    errors = rows(conn, """SELECT
        (SELECT COUNT(*) FROM PEDIDOS P WHERE NOT EXISTS
            (SELECT 1 FROM HECHOS_PEDIDOS H WHERE H.NUMERO_PEDIDO=P.NUMERO_PEDIDO)) AS EMPTY_ORDERS,
        (SELECT COUNT(*) FROM HECHOS_PEDIDOS H JOIN PEDIDOS P ON P.NUMERO_PEDIDO=H.NUMERO_PEDIDO
            WHERE H.ID_CLIENTE<>P.ID_CLIENTE OR H.ID_FECHA<>P.ID_FECHA
               OR H.TOTAL_LINEA<>H.CANTIDAD*H.PRECIO_UNITARIO_VENTA) AS INCONSISTENT_LINES,
        (SELECT COUNT(*) FROM DIM_PRODUCTOS WHERE CANTIDAD_INVENTARIO<0) AS NEGATIVE_STOCK,
        (SELECT COUNT(*) FROM PEDIDOS P JOIN DIM_FECHAS F ON F.ID_FECHA=P.ID_FECHA
            WHERE F.FECHA>TRUNC(CAST(SYSTIMESTAMP AT TIME ZONE 'America/Bogota' AS DATE))) AS FUTURE_ORDERS
        FROM DUAL""")[0]
    if any(errors.values()):
        raise AdminError("La verificación detectó pedidos, importes, fechas o existencias incoherentes.")
    counts = {name: int(rows(conn, f"SELECT COUNT(*) AS N FROM {target(name)}")[0]["n"])
              for name in sorted(NEW_TABLES)}
    if seed:
        expected = {"DIM_CLIENTES": 20, "DIM_PRODUCTOS": 150, "PEDIDOS": 60, "HECHOS_PEDIDOS": 150}
        if any(counts[name] != count for name, count in expected.items()):
            raise AdminError("Los conteos de la semilla no coinciden con el contrato esperado.")
        totals = rows(conn, """SELECT SUM(CANTIDAD) AS UNIDADES, SUM(TOTAL_LINEA) AS FACTURACION,
            SUM(CANTIDAD*COSTO_UNITARIO_VENTA) AS COSTO,
            SUM(TOTAL_LINEA-CANTIDAD*COSTO_UNITARIO_VENTA) AS UTILIDAD FROM HECHOS_PEDIDOS""")[0]
        if totals != {"unidades": 300, "facturacion": 24267000, "costo": 10815000, "utilidad": 13452000}:
            raise AdminError("Los totales contables de la semilla no coinciden con lo esperado.")
        variants = rows(conn, """SELECT COUNT(*) AS DISENOS, MIN(VARIANTES) AS MINIMO, MAX(VARIANTES) AS MAXIMO
            FROM (SELECT NOMBRE_PRODUCTO,CATEGORIA,COLOR,COUNT(DISTINCT TALLA) AS VARIANTES
                  FROM DIM_PRODUCTOS GROUP BY NOMBRE_PRODUCTO,CATEGORIA,COLOR)""")[0]
        if variants != {"disenos": 30, "minimo": 5, "maximo": 5}:
            raise AdminError("La semilla debe contener 30 diseños con cinco tallas cada uno.")
        stock = rows(conn, "SELECT SUM(CANTIDAD_INVENTARIO) AS TOTAL FROM DIM_PRODUCTOS")[0]["total"]
        if stock != 1813:
            raise AdminError("El saldo de inventario de la semilla no coincide con lo esperado.")
    execute_script(conn, ROOT / "sql" / "03_verificar.sql", transactional=True)
    print(canonical({"verification": "ok", "counts": counts}))


def migrate(conn, folder: Path):
    manifest = load_backup(folder)
    info = inventory(conn)
    require_migratable(info)
    if digest(info) != digest(manifest["inventory"]):
        raise AdminError("El inventario cambió desde el respaldo. Genera otro respaldo antes de migrar.")
    for table in manifest["tables"]:
        current = table_data(conn, table["name"], table["columns"])
        if current != {"rows": table["rows"], "data_sha256": table["data_sha256"]}:
            raise AdminError("Los datos cambiaron desde el respaldo: " + table["name"])
        current_ddl = get_ddl(conn, "TABLE", table["name"])
        if hashlib.sha256(current_ddl.encode("utf-8")).hexdigest() != table["ddl_sha256"]:
            raise AdminError("La estructura cambió desde el respaldo: " + table["name"])
    # Parsear todos los archivos antes del primer DDL para detectar faltantes/errores.
    for name in SCRIPT_NAMES:
        split_sql((ROOT / "sql" / name).read_text(encoding="utf-8"))
    conn.rollback()
    print("Respaldo e inventario verificados. Iniciando DDL explícito del esquema previsto.", flush=True)
    execute_script(conn, ROOT / "sql" / SCRIPT_NAMES[0])
    execute_script(conn, ROOT / "sql" / SCRIPT_NAMES[1])
    try:
        execute_script(conn, ROOT / "sql" / SCRIPT_NAMES[2], transactional=True)
        verify(conn, seed=True)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    print("Migración completada y datos de ejemplo confirmados.")


def restore(conn, folder: Path):
    manifest = load_backup(folder)
    existing = {r["table_name"] for r in rows(conn, "SELECT TABLE_NAME FROM USER_TABLES")}
    if existing & (LEGACY_TABLES | NEW_TABLES):
        raise AdminError("La restauración requiere que no existan tablas del kardex ni de la tienda. No se borrará ninguna automáticamente.")
    if existing:
        raise AdminError("El esquema contiene tablas ajenas; no se restaurará.")
    for table in manifest["tables"]:
        ddl = (folder / table["ddl_file"]).read_text(encoding="utf-8").strip().rstrip(";")
        with conn.cursor() as cur:
            cur.execute(ddl)
    identity_columns = manifest["inventory"]["identities"]
    for item in identity_columns:
        if item["generation_type"] == "ALWAYS":
            with conn.cursor() as cur:
                cur.execute(f"ALTER TABLE {target(item['table_name'])} MODIFY ({ident(item['column_name'])} GENERATED BY DEFAULT AS IDENTITY)")
    try:
        with conn.cursor() as cur:
            for table in manifest["tables"]:
                placeholders = ", ".join(f":{i + 1}" for i in range(len(table["columns"])))
                sql = f"INSERT INTO {target(table['name'])} ({', '.join(map(ident, table['columns']))}) VALUES ({placeholders})"
                with (folder / table["data_file"]).open(encoding="utf-8") as handle:
                    batch = []
                    for line in handle:
                        batch.append([decode(v) for v in json.loads(line)])
                        if len(batch) >= 500:
                            cur.executemany(sql, batch)
                            batch = []
                    if batch:
                        cur.executemany(sql, batch)
        for table in manifest["tables"]:
            restored = table_data(conn, table["name"], table["columns"])
            if restored != {"rows": table["rows"], "data_sha256": table["data_sha256"]}:
                raise AdminError("La verificación de restauración falló para " + table["name"])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    for item in identity_columns:
        generation = item["generation_type"]
        if generation not in {"ALWAYS", "BY DEFAULT", "BY DEFAULT ON NULL"}:
            raise AdminError("Generación identity desconocida en el respaldo.")
        with conn.cursor() as cur:
            cur.execute(f"ALTER TABLE {target(item['table_name'])} MODIFY ({ident(item['column_name'])} GENERATED {generation} AS IDENTITY (START WITH LIMIT VALUE))")
    for extra in manifest["extra_ddl"]:
        ddl = (folder / extra["file"]).read_text(encoding="utf-8").strip()
        if extra["kind"] != "TRIGGER":
            ddl = ddl.rstrip(";")
        with conn.cursor() as cur:
            cur.execute(ddl)
    print("Respaldo restaurado; filas verificadas y contadores identity ajustados.")


def safe_message(exc: Exception) -> str:
    if isinstance(exc, AdminError):
        return str(exc)
    codes = sorted(set(re.findall(r"(?:ORA|DPY)-\d{4,5}", str(exc))))
    if "DPY-6001" in codes or "ORA-12514" in codes:
        return "DPY-6001 / ORA-12514: el listener responde pero no reconoce el servicio. Verifica que Autonomous esté Available y que el wallet corresponda a la base. No se modificó la base si el fallo ocurrió al conectar."
    if isinstance(exc, OSError):
        return "No se pudo acceder al archivo o resolver/conectar el servicio. Revisa las rutas locales, la red y el wallet."
    return "Fallo de administración" + (" (" + ", ".join(codes) + ")" if codes else "") + ". No se muestran detalles que puedan contener credenciales."


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["preflight", "backup", "migrate", "verify", "restore"])
    parser.add_argument("--backup-dir", type=Path, help="Respaldo completo creado por esta herramienta.")
    args = parser.parse_args(argv)
    if args.command in {"migrate", "restore"} and args.backup_dir is None:
        parser.error("migrate y restore requieren --backup-dir")
    try:
        config = configuration()
        with connect(config) as conn:
            if args.command == "preflight":
                info = inventory(conn)
                print(json.dumps(info, ensure_ascii=False, indent=2, default=encode))
                problems = migration_problems(info)
                print(canonical({"can_migrate": not problems, "problems": problems}))
                return 2 if problems else 0
            if args.command == "backup":
                folder = make_backup(conn)
                print(canonical({"backup": str(folder), "integrity": "verified"}))
            elif args.command == "migrate":
                migrate(conn, args.backup_dir.resolve())
            elif args.command == "verify":
                verify(conn)
            elif args.command == "restore":
                restore(conn, args.backup_dir.resolve())
        return 0
    except Exception as exc:
        print(safe_message(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
