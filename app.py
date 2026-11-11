"""Marca de ropa. Arranque: python app.py. La UI y Oracle viven en este archivo."""
from __future__ import annotations

import argparse
import atexit
import errno
import hashlib
import json
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time as day_time, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen
from zoneinfo import ZoneInfo

import numpy as np
import oracledb
import pandas as pd
import sqlglot
import streamlit as st
from dotenv import dotenv_values
from sqlglot import exp

# Oracle NUMBER se conserva como Decimal, incluidos totales con centavos.
oracledb.defaults.fetch_decimals = True

ROOT = Path(__file__).resolve().parent
CATEGORIES = ('Pantalon', 'camisa', 'camiseta', 'pantaloneta', 'accesorio', 'top')
SIZES = ('XS', 'S', 'M', 'L', 'XL')
TABLES = ('DIM_CLIENTES', 'DIM_PRODUCTOS', 'DIM_FECHAS', 'PEDIDOS', 'HECHOS_PEDIDOS')


def today_bogota() -> date:
    return datetime.now(ZoneInfo('America/Bogota')).date()


def money(value) -> str:
    amount = Decimal(str(value or 0)).quantize(Decimal('0.01'))
    formatted = f'{amount:,.0f}' if amount == amount.to_integral_value() else f'{amount:,.2f}'
    return '$ ' + formatted.translate(str.maketrans({',':'.', '.':','})) + ' COP'


@dataclass(frozen=True)
class Settings:
    user: str = ''
    password: str = field(default='', repr=False)
    dsn: str = field(default='', repr=False)
    schema: str = 'EMILIO_CRUZ10'
    wallet_dir: str = ''
    wallet_key: str = field(default='', repr=False)
    oracle_mode: str = 'thin'
    lib_dir: str = ''
    max_rows: int = 20000
    timeout: int = 30
    connect_timeout: int = 10
    pool_wait_timeout: int = 30
    pool_max: int = 4
    port: int = 8501
    ngrok_enabled: bool = True
    ngrok_token: str = field(default='', repr=False)
    ngrok_user: str = field(default='', repr=False)
    ngrok_password: str = field(default='', repr=False)

    @property
    def ready(self):
        return bool(self.user and self.password and self.dsn)

    @property
    def owner(self):
        value = self.schema or self.user
        return value[1:-1] if value.startswith('"') and value.endswith('"') else value.upper()

    @property
    def fingerprint(self):
        return hashlib.sha256(repr(tuple(getattr(self, k) for k in self.__dataclass_fields__)).encode()).hexdigest()


def load_settings(root: Path = ROOT) -> Settings:
    """Una sola fuente; nunca consulta os.environ ni st.secrets."""
    root = Path(root)
    config = dotenv_values(root / '.env', interpolate=False)

    def read(key, default=''):
        value = config.get(key)
        return default if value is None or value == '' else str(value)

    def number(key, default, low, high):
        try:
            value = int(read(key, str(default)))
        except ValueError:
            raise ValueError(f'{key} debe ser un número entero.') from None
        if not low <= value <= high:
            raise ValueError(f'{key} debe estar entre {low} y {high}.')
        return value

    def path(key):
        value = read(key)
        if not value:
            return ''
        p = Path(value).expanduser()
        return str(p if p.is_absolute() else root / p)

    enabled = read('NGROK_ENABLED', 'true').lower()
    if enabled not in ('true', 'false'):
        raise ValueError('NGROK_ENABLED debe ser true o false.')
    settings = Settings(
        user=read('USER_DB'), password=read('DB_PASSWORD'), dsn=read('DB_DSN'),
        schema=read('DB_SCHEMA', read('USER_DB')), wallet_dir=path('WALLET_DIR'),
        wallet_key=read('WALLET_KEY'), oracle_mode=read('ORACLE_MODE', 'thin').lower(),
        lib_dir=path('ORACLE_CLIENT_LIB_DIR'),
        max_rows=number('MAX_ROWS', 20000, 100, 100000),
        timeout=number('QUERY_TIMEOUT_SECONDS', 30, 1, 300),
        connect_timeout=number('CONNECT_TIMEOUT_SECONDS', 10, 1, 120),
        pool_wait_timeout=number('POOL_WAIT_TIMEOUT_SECONDS', 30, 1, 300),
        pool_max=number('POOL_MAX_CONNECTIONS', 4, 1, 16),
        port=number('STREAMLIT_PORT', 8501, 1024, 65535),
        ngrok_enabled=enabled == 'true', ngrok_token=read('NGROK_AUTHTOKEN'),
        ngrok_user=read('NGROK_BASIC_USER'), ngrok_password=read('NGROK_BASIC_PASSWORD'),
    )
    if settings.oracle_mode not in ('thin', 'thick'):
        raise ValueError('ORACLE_MODE debe ser thin o thick.')
    if settings.owner:
        quote_identifier(settings.owner)
    return settings


def safe_error(exc: Exception, settings: Settings) -> str:
    text = str(exc)
    code = re.search(r'(?:ORA|DPY)-\d+', text)
    if code and code.group() in ('ORA-12514', 'DPY-6001'):
        return 'ORA-12514: Oracle no reconoce el servicio. Comprueba que Autonomous esté Available y que el wallet corresponda al servicio actual.'
    if code and code.group() == 'ORA-00001':
        return 'Ya existe un registro con esos datos únicos (correo o variante de producto).'
    if code and code.group() in ('ORA-02290', 'ORA-01400', 'ORA-02291'):
        return f'{code.group()}: los datos no cumplen las restricciones del modelo.'
    if code and code.group() in ('ORA-00054', 'ORA-30006'):
        return 'Otro pedido está utilizando estos productos. Reintenta con el mismo carrito.'
    if code and code.group() == 'DPY-4005':
        return 'No se pudo obtener una conexión Oracle a tiempo. Usa Probar conexión Oracle para diagnosticar el servicio.'
    secrets = set()
    for secret in (settings.password, settings.wallet_key, settings.dsn, settings.ngrok_token,
                   settings.ngrok_user, settings.ngrok_password):
        if secret:
            secrets.update((secret, repr(secret)[1:-1], json.dumps(secret)[1:-1],
                            json.dumps(json.dumps(secret)[1:-1])[1:-1]))
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, '[oculto]')
    return text[:900] or type(exc).__name__


def quote_identifier(name):
    if not name or not re.fullmatch(r'[\w$# -]{1,128}', name):
        raise ValueError('Nombre de esquema o tabla no válido.')
    return '"' + name + '"'


_client_lock = threading.Lock()
_thick_config = None


def connection_params(settings: Settings, pooled=False):
    global _thick_config
    if not settings.ready:
        raise ValueError('Completa USER_DB, DB_PASSWORD y DB_DSN en .env.')
    if settings.oracle_mode == 'thick':
        with _client_lock:
            config = (settings.lib_dir, settings.wallet_dir)
            if _thick_config is not None and config != _thick_config:
                raise ValueError('Reinicia la aplicación después de cambiar el wallet o el cliente Oracle.')
            if _thick_config is None:
                oracledb.init_oracle_client(lib_dir=settings.lib_dir or None, config_dir=settings.wallet_dir or None)
                _thick_config = config
    elif not oracledb.is_thin_mode():
        raise ValueError('Reinicia la aplicación para cambiar a modo thin.')
    factory = oracledb.PoolParams if pooled else oracledb.ConnectParams
    params = factory(user=settings.user.strip('"'), password=settings.password,
                     config_dir=settings.wallet_dir or None)
    params.parse_connect_string(settings.dsn)
    params.set(tcp_connect_timeout=settings.connect_timeout, retry_count=0, retry_delay=0)
    if settings.oracle_mode == 'thin' and settings.wallet_dir:
        params.set(wallet_location=settings.wallet_dir, wallet_password=settings.wallet_key or None)
    if pooled:
        params.set(min=1, max=settings.pool_max, increment=1, timeout=60,
                   wait_timeout=settings.pool_wait_timeout * 1000,
                   getmode=oracledb.POOL_GETMODE_TIMEDWAIT, ping_interval=30, ping_timeout=5000)
    return params


@st.cache_resource(show_spinner=False)
def oracle_pool(settings: Settings):
    return oracledb.create_pool(params=connection_params(settings, pooled=True))


def connect_oracle(settings: Settings):
    return oracledb.connect(params=connection_params(settings))


def nonempty(value, label, maximum):
    value = str(value).strip() if value is not None else ''
    if not value or len(value) > maximum:
        raise ValueError(f'{label}: escribe entre 1 y {maximum} caracteres.')
    return value


def integer(value, label='Cantidad', minimum=1):
    try:
        if isinstance(value, (bool, np.bool_)):
            raise ValueError
        parsed = Decimal(str(value))
        if not parsed.is_finite() or parsed != parsed.to_integral_value() or parsed < minimum or parsed > 999999999:
            raise ValueError
        return int(parsed)
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f'{label} debe ser un entero entre {minimum} y 999999999.') from None


def amount(value, label='Importe'):
    try:
        parsed = Decimal(str(value))
        if not parsed.is_finite() or parsed < 0 or parsed > Decimal('9999999999.99'):
            raise ValueError
        rounded = parsed.quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
        if rounded != parsed:
            raise ValueError
        return rounded
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f'{label} debe ser no negativo y tener como máximo dos decimales.') from None


def valid_date(value):
    if isinstance(value, datetime):
        value = value.date()
    if not isinstance(value, date):
        raise ValueError('Selecciona una fecha válida.')
    return value


def date_bounds(start, end):
    start, end = valid_date(start), valid_date(end)
    if start > end:
        raise ValueError('La fecha inicial no puede ser posterior a la final.')
    return {'start_date': datetime.combine(start, day_time()),
            'end_date': datetime.combine(end + timedelta(days=1), day_time())}


def validate_select(sql: str) -> str:
    if not sql.strip() or len(sql) > 50000:
        raise ValueError('Escribe una consulta de hasta 50.000 caracteres.')
    try:
        tokens = sqlglot.tokenize(sql, read='oracle')
        if tokens and tokens[-1].token_type == sqlglot.TokenType.SEMICOLON:
            token = tokens.pop()
            sql = sql[:token.start] + ' ' + sql[token.end + 1:]
        statements = [x for x in sqlglot.parse(sql, read='oracle', error_level=sqlglot.ErrorLevel.RAISE) if x is not None]
    except sqlglot.errors.SqlglotError:
        raise ValueError('No se pudo interpretar el SQL de Oracle.') from None
    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        raise ValueError('Ejecuta una única consulta SELECT o WITH … SELECT.')
    forbidden = {'Insert','Update','Delete','Merge','Create','Drop','Alter','Command','Into','Lock',
                 'Transaction','Commit','Rollback','Grant','Revoke','Execute','NextValueFor'}
    for node in statements[0].walk():
        if type(node).__name__ in forbidden:
            raise ValueError('El laboratorio solo permite consultas: no admite DML, DDL ni FOR UPDATE.')
        if isinstance(node, exp.Column) and node.name.upper() in ('NEXTVAL', 'CURRVAL'):
            raise ValueError('El laboratorio no permite utilizar secuencias.')
        if isinstance(node, exp.Anonymous) and node.name.upper() not in {
                'TO_CHAR','TO_DATE','TO_NUMBER','TRUNC','ADD_MONTHS','MONTHS_BETWEEN','LAST_DAY',
                'NVL','NVL2','DECODE','REGEXP_LIKE','REGEXP_REPLACE','REGEXP_SUBSTR','LISTAGG'}:
            raise ValueError('El laboratorio no permite funciones de usuario ni paquetes PL/SQL.')
        if isinstance(node, exp.Dot) and isinstance(node.expression, exp.Func):
            raise ValueError('El laboratorio no permite llamadas a paquetes PL/SQL.')
        if isinstance(node, exp.Table) and ('@' in node.sql() or node.catalog):
            raise ValueError('El laboratorio no permite enlaces a otras bases de datos.')
    return sql.strip()


def result_frame(cursor, rows):
    names, used = [], set()
    for column in cursor.description:
        name = original = str(column[0]).lower()
        counter = 2
        while name in used:
            name = f'{original}_{counter}'
            counter += 1
        names.append(name)
        used.add(name)
    values = [[v.read() if isinstance(v, oracledb.LOB) else v for v in row] for row in rows]
    return pd.DataFrame(values, columns=names)


@dataclass(frozen=True)
class OrderResult:
    numero_pedido: int
    total: Decimal
    reused: bool = False


class Database:
    def __init__(self, settings: Settings):
        self.settings = settings

    @contextmanager
    def connection(self, readonly=False):
        with oracle_pool(self.settings).acquire() as conn:
            conn.call_timeout = self.settings.timeout * 1000
            conn.current_schema = self.settings.owner
            conn.autocommit = False
            conn.rollback()
            try:
                if readonly:
                    with conn.cursor() as cur:
                        cur.execute('SET TRANSACTION READ ONLY')
                yield conn
            finally:
                # También elimina cualquier transacción incompleta antes de devolver al pool.
                try:
                    conn.rollback()
                except oracledb.Error:
                    pass

    def _read(self, sql, params=None, limit=None):
        limit = self.settings.max_rows if limit is None else limit
        with self.connection(readonly=True) as conn:
            with conn.cursor() as cur:
                cur.arraysize = min(1000, limit + 1)
                cur.execute(sql, params or {})
                rows = cur.fetchmany(limit + 1)
                frame = result_frame(cur, rows[:limit])
        frame.attrs['truncated'] = len(rows) > limit
        return frame

    def query(self, sql, params=None, limit=1000):
        if params is not None and not isinstance(params, dict):
            raise ValueError('Los parámetros deben ser un objeto JSON.')
        return self._read(validate_select(sql), params, min(integer(limit, 'Límite'), self.settings.max_rows))

    def test_connection(self):
        with connect_oracle(self.settings) as conn:
            conn.call_timeout = self.settings.timeout * 1000
            conn.current_schema = self.settings.owner
            with conn.cursor() as cur:
                cur.execute("SELECT USER, SYS_CONTEXT('USERENV','CURRENT_SCHEMA') FROM DUAL")
                user, schema = cur.fetchone()
                return {'user': user, 'schema': schema}

    def schema_status(self):
        data = self._read('SELECT table_name FROM all_tables WHERE owner=:owner', {'owner': self.settings.owner})
        return sorted(set(TABLES) - set(data.table_name))

    def metadata(self):
        return self._read("""SELECT table_name,column_name,data_type,nullable FROM all_tab_columns
          WHERE owner=:owner AND table_name IN ('DIM_CLIENTES','DIM_PRODUCTOS','DIM_FECHAS','PEDIDOS','HECHOS_PEDIDOS')
          ORDER BY table_name,column_id""", {'owner': self.settings.owner})

    def clients(self, search='', status='Todos'):
        clauses, params = [], {}
        if search.strip():
            clauses.append('(INSTR(LOWER(nombre),:q)>0 OR INSTR(LOWER(correo),:q)>0)')
            params['q'] = search.strip().lower()
        if status != 'Todos':
            clauses.append('estado=:estado')
            params['estado'] = status
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        return self._read('SELECT * FROM dim_clientes' + where + ' ORDER BY nombre,id_cliente', params)

    def products(self, search='', category='Todas', size='Todas', color='', availability='Todas'):
        clauses, params = [], {}
        if search.strip():
            clauses.append('INSTR(LOWER(nombre_producto),:q)>0')
            params['q'] = search.strip().lower()
        for column, value in (('categoria',category), ('talla',size)):
            if value and value != 'Todas':
                clauses.append(f'{column}=:{column}')
                params[column] = value
        if color.strip():
            clauses.append('INSTR(LOWER(color),:color)>0')
            params['color'] = color.strip().lower()
        if availability == 'Con existencias':
            clauses.append('cantidad_inventario>0')
        elif availability == 'Agotados':
            clauses.append('cantidad_inventario=0')
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        return self._read('SELECT * FROM dim_productos' + where + ' ORDER BY nombre_producto,id_producto', params)

    def create_client(self, nombre, correo, telefono, direccion, estado='ACTIVO'):
        values = {'nombre':nonempty(nombre,'Nombre',120), 'correo':nonempty(correo,'Correo',254).lower(),
                  'telefono':nonempty(telefono,'Teléfono',30), 'direccion':nonempty(direccion,'Dirección',250),
                  'estado':estado, 'fecha_registro':datetime.now(ZoneInfo('America/Bogota')).replace(tzinfo=None)}
        if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', values['correo']):
            raise ValueError('Escribe un correo válido.')
        if estado not in ('ACTIVO','INACTIVO'):
            raise ValueError('El estado debe ser ACTIVO o INACTIVO.')
        with self.connection() as conn:
            with conn.cursor() as cur:
                output = cur.var(oracledb.DB_TYPE_NUMBER)
                cur.execute('''INSERT INTO dim_clientes(nombre,correo,telefono,direccion,fecha_registro,estado)
                    VALUES(:nombre,:correo,:telefono,:direccion,:fecha_registro,:estado)
                    RETURNING id_cliente INTO :new_id''', {**values,'new_id':output})
                identifier = int(output.getvalue()[0])
            conn.commit()
        return identifier

    def create_product(self, nombre_producto,categoria,talla,color,precio_actual,costo_actual,cantidad_inventario):
        if categoria not in CATEGORIES or talla not in SIZES:
            raise ValueError('Selecciona una categoría y talla válidas.')
        values = {'nombre_producto':nonempty(nombre_producto,'Nombre',120),'categoria':categoria,
                  'talla':talla,'color':nonempty(color,'Color',40),
                  'precio_actual':amount(precio_actual,'Precio'),'costo_actual':amount(costo_actual,'Costo'),
                  'cantidad_inventario':integer(cantidad_inventario,'Inventario',0)}
        with self.connection() as conn:
            with conn.cursor() as cur:
                output = cur.var(oracledb.DB_TYPE_NUMBER)
                cur.execute('''INSERT INTO dim_productos(nombre_producto,categoria,talla,color,precio_actual,costo_actual,cantidad_inventario)
                  VALUES(:nombre_producto,:categoria,:talla,:color,:precio_actual,:costo_actual,:cantidad_inventario)
                  RETURNING id_producto INTO :new_id''', {**values,'new_id':output})
                identifier = int(output.getvalue()[0])
            conn.commit()
        return identifier

    @staticmethod
    def _existing_order(cur, token, client, date_id, quantities):
        cur.execute('SELECT numero_pedido,id_cliente,id_fecha FROM pedidos WHERE token_solicitud=:token', {'token':token})
        header = cur.fetchone()
        if header is None:
            return None
        cur.execute('''SELECT id_producto,cantidad,precio_unitario_venta FROM hechos_pedidos
                       WHERE numero_pedido=:numero ORDER BY id_producto''', {'numero':header[0]})
        lines = cur.fetchall()
        stored = {int(row[0]):int(row[1]) for row in lines}
        if int(header[1]) != client or int(header[2]) != date_id or stored != quantities:
            raise ValueError('Este intento ya corresponde a otro pedido confirmado. Inicia un pedido nuevo para cambiar el carrito.')
        total = sum((Decimal(str(row[1])) * Decimal(str(row[2])) for row in lines), Decimal('0'))
        return OrderResult(int(header[0]),total,True)

    def create_order(self, id_cliente, fecha, items, token_solicitud):
        client = integer(id_cliente,'Cliente')
        fecha = valid_date(fecha)
        if fecha > today_bogota():
            raise ValueError('La fecha de venta no puede ser futura.')
        try:
            token = str(uuid.UUID(str(token_solicitud)))
        except (ValueError,AttributeError):
            raise ValueError('El identificador del carrito no es válido. Inicia un pedido nuevo.') from None
        if not items:
            raise ValueError('Agrega al menos un producto al pedido.')
        quantities = {}
        for item in items:
            product = integer(item['id_producto'],'Producto')
            quantities[product] = integer(quantities.get(product,0) + integer(item['cantidad']), 'Cantidad')
        date_id = int(fecha.strftime('%Y%m%d'))
        committing = False
        try:
            with self.connection() as conn:
                with conn.cursor() as cur:
                    existing = self._existing_order(cur,token,client,date_id,quantities)
                    if existing:
                        return existing
                    cur.execute('SELECT estado FROM dim_clientes WHERE id_cliente=:id FOR UPDATE WAIT 5', {'id':client})
                    customer = cur.fetchone()
                    if customer is None or customer[0] != 'ACTIVO':
                        raise ValueError('Selecciona un cliente activo.')
                    cur.execute('SELECT id_fecha FROM dim_fechas WHERE id_fecha=:id', {'id':date_id})
                    if cur.fetchone() is None:
                        try:
                            cur.execute('INSERT INTO dim_fechas(id_fecha,fecha) VALUES(:id,:fecha)',
                                        {'id':date_id,'fecha':datetime.combine(fecha,day_time())})
                        except oracledb.IntegrityError as exc:
                            if getattr(exc.args[0], 'code', None) != 1:
                                raise
                            cur.execute('SELECT id_fecha FROM dim_fechas WHERE id_fecha=:id',{'id':date_id})
                            if cur.fetchone() is None:
                                raise
                    output = cur.var(oracledb.DB_TYPE_NUMBER)
                    try:
                        cur.execute('''INSERT INTO pedidos(id_cliente,id_fecha,token_solicitud)
                          VALUES(:cliente,:fecha,:token) RETURNING numero_pedido INTO :numero''',
                          {'cliente':client,'fecha':date_id,'token':token,'numero':output})
                    except oracledb.IntegrityError as exc:
                        if getattr(exc.args[0], 'code', None) != 1:
                            raise
                        conn.rollback()
                        existing = self._existing_order(cur,token,client,date_id,quantities)
                        if existing:
                            return existing
                        raise
                    number = int(output.getvalue()[0])
                    locked = []
                    for product, quantity in sorted(quantities.items()):
                        cur.execute('''SELECT nombre_producto,cantidad_inventario,precio_actual,costo_actual
                          FROM dim_productos WHERE id_producto=:id FOR UPDATE WAIT 5''', {'id':product})
                        row = cur.fetchone()
                        if row is None:
                            raise ValueError(f'El producto {product} ya no existe.')
                        if row[1] < quantity:
                            raise ValueError(f'Existencias insuficientes para {row[0]}: disponibles {int(row[1])}, solicitadas {quantity}.')
                        locked.append((product,quantity,Decimal(str(row[2])),Decimal(str(row[3]))))
                    total = Decimal('0')
                    for product,quantity,price,cost in locked:
                        cur.execute('''INSERT INTO hechos_pedidos(numero_pedido,id_cliente,id_producto,id_fecha,
                          cantidad,precio_unitario_venta,costo_unitario_venta)
                          VALUES(:pedido,:cliente,:producto,:fecha,:cantidad,:precio,:costo)''',
                          {'pedido':number,'cliente':client,'producto':product,'fecha':date_id,
                           'cantidad':quantity,'precio':price,'costo':cost})
                        cur.execute('''UPDATE dim_productos SET cantidad_inventario=cantidad_inventario-:cantidad
                          WHERE id_producto=:producto AND cantidad_inventario>=:cantidad''',
                          {'cantidad':quantity,'producto':product})
                        if cur.rowcount != 1:
                            raise ValueError('Las existencias cambiaron. Reintenta el pedido.')
                        total += price * quantity
                committing = True
                conn.commit()
                return OrderResult(number,total)
        except oracledb.Error:
            if committing:
                # Resultado incierto del COMMIT: comprobar el token en una conexión nueva.
                try:
                    with connect_oracle(self.settings) as conn:
                        conn.current_schema = self.settings.owner
                        conn.call_timeout = self.settings.timeout * 1000
                        with conn.cursor() as cur:
                            existing = self._existing_order(cur,token,client,date_id,quantities)
                            if existing:
                                return existing
                except oracledb.Error:
                    pass
            raise

    def orders(self, start, end, id_cliente=None):
        params = date_bounds(start,end)
        customer = ''
        if id_cliente is not None:
            customer = ' AND p.id_cliente=:cliente'
            params['cliente'] = integer(id_cliente,'Cliente')
        return self._read('''SELECT p.numero_pedido,f.fecha,c.nombre nombre_cliente,COUNT(h.id_detalle) lineas,
          SUM(h.cantidad) unidades,SUM(h.total_linea) total,SUM(h.cantidad*h.costo_unitario_venta) costo,
          SUM(h.total_linea-h.cantidad*h.costo_unitario_venta) utilidad
          FROM pedidos p JOIN dim_clientes c ON c.id_cliente=p.id_cliente
          JOIN dim_fechas f ON f.id_fecha=p.id_fecha JOIN hechos_pedidos h ON h.numero_pedido=p.numero_pedido
          WHERE f.fecha>=:start_date AND f.fecha<:end_date''' + customer + '''
          GROUP BY p.numero_pedido,f.fecha,c.nombre ORDER BY f.fecha DESC,p.numero_pedido DESC''', params)

    def order_lines(self, numero):
        return self._read('''SELECT h.id_producto,p.nombre_producto,p.categoria,p.talla,p.color,h.cantidad,
          h.precio_unitario_venta,h.costo_unitario_venta,h.total_linea,
          h.total_linea-h.cantidad*h.costo_unitario_venta utilidad
          FROM hechos_pedidos h JOIN dim_productos p ON p.id_producto=h.id_producto
          WHERE h.numero_pedido=:numero ORDER BY h.id_detalle''', {'numero':integer(numero,'Pedido')})

    def report(self, start, end):
        params = date_bounds(start,end)
        base = ''' FROM hechos_pedidos h JOIN dim_fechas f ON f.id_fecha=h.id_fecha
          WHERE f.fecha>=:start_date AND f.fecha<:end_date'''
        # Una sola transacción de lectura para una fotografía consistente de todos los gráficos.
        with self.connection(readonly=True) as conn:
            with conn.cursor() as cur:
                cur.execute('''SELECT NVL(SUM(h.total_linea),0) facturacion,
                  NVL(SUM(h.cantidad*h.costo_unitario_venta),0) costo,
                  COUNT(DISTINCT h.numero_pedido) pedidos,NVL(SUM(h.cantidad),0) unidades''' + base,params)
                sales,cost,orders,units = cur.fetchone()
                sales,cost = Decimal(str(sales)),Decimal(str(cost))
                summary = {'facturacion':sales,'costo':cost,'utilidad':sales-cost,
                           'pedidos':int(orders),'unidades':int(units),
                           'margen':float((sales-cost)/sales*100) if sales else 0.0}
                cur.execute('''SELECT TRUNC(f.fecha,'MM') mes,SUM(h.total_linea) facturacion,
                  SUM(h.cantidad*h.costo_unitario_venta) costo,
                  SUM(h.total_linea-h.cantidad*h.costo_unitario_venta) utilidad''' + base +
                  " GROUP BY TRUNC(f.fecha,'MM') ORDER BY mes",params)
                monthly = result_frame(cur,cur.fetchall())
                cur.execute('''SELECT p.nombre_producto,SUM(h.cantidad) unidades,SUM(h.total_linea) facturacion,
                  SUM(h.total_linea-h.cantidad*h.costo_unitario_venta) utilidad
                  FROM hechos_pedidos h JOIN dim_fechas f ON f.id_fecha=h.id_fecha
                  JOIN dim_productos p ON p.id_producto=h.id_producto
                  WHERE f.fecha>=:start_date AND f.fecha<:end_date GROUP BY p.nombre_producto
                  ORDER BY unidades DESC,facturacion DESC,p.nombre_producto FETCH FIRST 10 ROWS ONLY''',params)
                top = result_frame(cur,cur.fetchall())
        months = pd.date_range(start=valid_date(start).replace(day=1),end=valid_date(end).replace(day=1),freq='MS')
        monthly['mes'] = pd.to_datetime(monthly['mes'])
        monthly = monthly.set_index('mes').reindex(months,fill_value=0).rename_axis('mes').reset_index()
        for column in ('facturacion','costo','utilidad'):
            monthly[column] = pd.to_numeric(monthly[column]).fillna(0)
        return {'summary':summary,'monthly':monthly,'top':top}


# La interfaz se integra debajo; no crea conexiones ni túneles al importar.

def _ui_style():
    st.markdown("""<style>
    .stApp {background:#f7f8f5;color:#20332d;}
    [data-testid="stSidebar"] {background:#fff;border-right:1px solid #e3e8df;}
    .block-container {max-width:1400px;padding-top:3rem;padding-bottom:3rem;}
    h1,h2,h3 {letter-spacing:-.035em;color:#203d32;}
    [data-testid="stMetric"] {background:white;border:1px solid #e1e7dc;border-radius:13px;padding:17px;}
    [data-testid="stMetricValue"] {font-size:1.45rem;}
    [data-testid="stMetricValue"] > div {white-space:normal;overflow:visible;text-overflow:clip;}
    [data-testid="stButton"] button,[data-testid="stFormSubmitButton"] button {border-radius:8px;}
    [data-testid="stTextArea"] textarea {font-family:ui-monospace,monospace;font-size:.9rem;}
    .ropa-eyebrow {font-size:.72rem;letter-spacing:.17em;color:#7b8d80;font-weight:700;margin-bottom:.6rem;}
    .ropa-brand {font-size:1.8rem;font-weight:800;letter-spacing:-.07em;color:#244e3a;margin-bottom:0;}
    .ropa-caption {font-size:.78rem;color:#7b8d80;margin-bottom:1.5rem;}
    @media(max-width:1100px){
      .st-key-ropa_report_metrics [data-testid="stHorizontalBlock"], .st-key-ropa_inventory_metrics [data-testid="stHorizontalBlock"] {flex-wrap:wrap;}
      .st-key-ropa_report_metrics [data-testid="stColumn"], .st-key-ropa_inventory_metrics [data-testid="stColumn"] {flex:1 1 calc(50% - 1rem)!important;min-width:180px!important;}
      .st-key-ropa_report_charts [data-testid="stHorizontalBlock"] {flex-wrap:wrap;}
      .st-key-ropa_report_charts [data-testid="stColumn"] {flex:1 1 100%!important;min-width:100%!important;}
    }
    @media(max-width:700px){.block-container{padding-top:2rem;}[data-testid="stMetricValue"]{font-size:1.35rem;}}
    </style>""", unsafe_allow_html=True)


def _ui_csv(frame):
    output = frame.copy()
    for column in output.select_dtypes(include=["object", "string", "str"]).columns:
        output[column] = output[column].map(
            lambda value: "'" + value if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else value)
    return output.to_csv(index=False).encode("utf-8-sig")


def _ui_table(frame, key, filename, labels=None):
    if frame.attrs.get("truncated"):
        st.warning("La consulta alcanzó el límite de filas. Los registros y totales mostrados abarcan solo las filas cargadas; ajusta los filtros para ver más detalle.")
    if frame.empty:
        st.info("No hay registros para esta selección.")
        return
    st.dataframe(frame, hide_index=True, width="stretch", column_config=labels or {})
    st.download_button("Descargar CSV", _ui_csv(frame), filename, "text/csv", key=f"ropa_csv_{key}")


def _ui_error(exc, settings):
    st.error(safe_error(exc, settings))


def _ui_dates(prefix, monthly=False):
    from datetime import timedelta
    current = today_bogota()
    if monthly:
        mode = st.radio("Período", ["Mes", "Rango de fechas"], horizontal=True, key=f"ropa_{prefix}_period")
    else:
        mode = "Rango de fechas"
    if mode == "Mes":
        month = st.date_input("Mes a analizar", value=current.replace(day=1), key=f"ropa_{prefix}_month", help="Se analizará el mes completo de la fecha seleccionada.")
        start = month.replace(day=1)
        end = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    else:
        left, right = st.columns(2)
        start = left.date_input("Desde", value=current.replace(day=1), key=f"ropa_{prefix}_start")
        end = right.date_input("Hasta", value=current, key=f"ropa_{prefix}_end")
    if start > end:
        st.warning("La fecha inicial debe ser anterior o igual a la fecha final.")
        return None
    return start, end


def _ui_report(db, settings):
    st.title("Resumen y contabilidad")
    st.caption("Ventas, costo de las prendas vendidas y utilidad bruta. Valores en pesos colombianos (COP).")
    dates = _ui_dates("report", monthly=True)
    if dates is None:
        return
    report = db.report(*dates)
    summary = report["summary"]
    with st.container(key="ropa_report_metrics"):
        columns = st.columns(4)
    columns[0].metric("Facturación", money(summary["facturacion"]).removesuffix(" COP"))
    columns[1].metric("Costo de ventas", money(summary["costo"]).removesuffix(" COP"))
    columns[2].metric("Utilidad bruta", money(summary["utilidad"]).removesuffix(" COP"))
    columns[3].metric("Margen bruto", f"{float(summary['margen'] or 0):.1f}%")
    a, b = st.columns(2)
    a.caption(f"{int(summary['pedidos']):,} pedidos en el período")
    b.caption(f"{int(summary['unidades']):,} prendas vendidas")
    st.caption("Utilidad bruta = facturación − costo histórico de las prendas vendidas. Este resumen no incluye impuestos ni gastos operativos.")
    monthly, top = report["monthly"], report["top"]
    with st.container(key="ropa_report_charts"):
        left, right = st.columns([1.1, 1])
    with left:
        st.subheader("Evolución mensual")
        if not monthly.empty:
            plot = monthly.copy()
            plot["mes"] = pd.to_datetime(plot["mes"]).dt.strftime("%Y-%m")
            for column in ("facturacion", "costo", "utilidad"):
                plot[column] = pd.to_numeric(plot[column], errors="coerce").fillna(0)
            st.bar_chart(plot.set_index("mes")[["facturacion", "costo", "utilidad"]], color=["#276247", "#bfaa7b", "#87ae93"], stack=False)
        _ui_table(monthly, "monthly", "resumen_mensual.csv")
    with right:
        st.subheader("Productos más vendidos")
        if not top.empty:
            plot = top.head(10).copy()
            plot["unidades"] = pd.to_numeric(plot["unidades"], errors="coerce").fillna(0)
            st.bar_chart(plot.set_index("nombre_producto")[["unidades"]], horizontal=True, color="#276247", sort="-unidades")
        _ui_table(top, "top", "productos_mas_vendidos.csv")


def _ui_clients(db, settings):
    st.title("Clientes")
    st.caption("Registra clientes y encuentra sus datos de contacto.")
    with st.expander("Registrar cliente", expanded=True):
        with st.form("ropa_client_form", clear_on_submit=False):
            a, b = st.columns(2)
            nombre = a.text_input("Nombre completo", max_chars=120, key="ropa_client_nombre")
            correo = b.text_input("Correo electrónico", max_chars=254, key="ropa_client_correo")
            telefono = a.text_input("Teléfono", max_chars=30, key="ropa_client_telefono")
            direccion = b.text_input("Dirección", max_chars=250, key="ropa_client_direccion")
            submitted = st.form_submit_button("Guardar cliente", type="primary")
        if submitted:
            try:
                identifier = db.create_client(nombre.strip(), correo.strip(), telefono.strip(), direccion.strip(), estado="ACTIVO")
                st.success(f"Cliente registrado · ID {identifier}")
            except Exception as exc:
                _ui_error(exc, settings)
    st.subheader("Consultar clientes")
    a, b = st.columns([3, 1])
    search = a.text_input("Buscar cliente", placeholder="Nombre o correo", key="ropa_client_search")
    status = b.selectbox("Estado", ["Todos", "ACTIVO", "INACTIVO"], key="ropa_client_status")
    frame = db.clients(search=search, status=status)
    st.caption(f"{len(frame):,} clientes encontrados")
    _ui_table(frame, "clients", "clientes.csv", {"id_cliente": "ID", "nombre": "Nombre", "correo": "Correo", "telefono": "Teléfono", "direccion": "Dirección", "estado": "Estado"})


def _ui_products(db, settings):
    st.title("Productos e inventario")
    st.caption("Cada registro identifica una prenda por su categoría, talla y color.")
    with st.expander("Registrar prenda", expanded=True):
        with st.form("ropa_product_form", clear_on_submit=False):
            nombre = st.text_input("Nombre de la prenda", max_chars=120, key="ropa_product_nombre")
            a, b, c = st.columns(3)
            categoria = a.selectbox("Categoría", CATEGORIES, key="ropa_product_categoria")
            talla = b.selectbox("Talla", SIZES, key="ropa_product_talla")
            color = c.text_input("Color", max_chars=40, key="ropa_product_color")
            precio = a.number_input("Precio de venta (COP)", min_value=0.0, value=0.0, step=1000.0, format="%.2f", key="ropa_product_precio")
            costo = b.number_input("Costo unitario (COP)", min_value=0.0, value=0.0, step=1000.0, format="%.2f", key="ropa_product_costo")
            cantidad = c.number_input("Existencias iniciales", min_value=0, value=0, step=1, key="ropa_product_stock")
            submitted = st.form_submit_button("Guardar prenda", type="primary")
        if submitted:
            try:
                identifier = db.create_product(nombre.strip(), categoria, talla, color.strip(), precio, costo, int(cantidad))
                st.success(f"Prenda registrada · ID {identifier}")
            except Exception as exc:
                _ui_error(exc, settings)
    st.subheader("Consultar inventario")
    search = st.text_input("Buscar prenda", placeholder="Nombre de la prenda", key="ropa_product_search")
    a, b, c, d = st.columns(4)
    category = a.selectbox("Filtrar categoría", ["Todas", *CATEGORIES], key="ropa_product_filter_category")
    size = b.selectbox("Filtrar talla", ["Todas", *SIZES], key="ropa_product_filter_size")
    color_filter = c.text_input("Filtrar color", key="ropa_product_filter_color")
    availability = d.selectbox("Disponibilidad", ["Todas", "Con existencias", "Agotados"], key="ropa_product_filter_stock")
    frame = db.products(search=search, category=category, size=size, color=color_filter, availability=availability)
    with st.container(key="ropa_inventory_metrics"):
        a, b, c = st.columns(3)
    a.metric("Referencias", len(frame))
    b.metric("Unidades disponibles", int(frame["cantidad_inventario"].sum()) if not frame.empty else 0)
    value = sum((Decimal(str(cost)) * Decimal(str(quantity))
                 for cost, quantity in zip(frame["costo_actual"], frame["cantidad_inventario"])),
                Decimal('0')) if not frame.empty else Decimal('0')
    c.metric("Inventario al costo", money(value).removesuffix(" COP"))
    _ui_table(frame, "products", "inventario.csv", {"id_producto": "ID", "nombre_producto": "Prenda", "categoria": "Categoría", "talla": "Talla", "color": "Color", "precio_actual": "Precio (COP)", "costo_actual": "Costo (COP)", "cantidad_inventario": "Existencias"})


def _ui_product_label(row):
    return f"{row['nombre_producto']} · {row['talla']} · {row['color']} · {money(row['precio_actual'])} · {int(row['cantidad_inventario'])} disponibles"


def _ui_save_order(db, settings, attempt):
    try:
        result = db.create_order(attempt["client"], attempt["date"], attempt["items"], attempt["token"])
        st.session_state.ropa_order_notice = f"Pedido #{result.numero_pedido} {'recuperado' if result.reused else 'registrado'} · Total {money(result.total)}"
        st.session_state.ropa_cart = {}
        st.session_state.pop("ropa_order_attempt", None)
        st.rerun()
    except Exception as exc:
        # Un fallo de conexión puede ocurrir después del COMMIT. El siguiente
        # intento debe consultar el mismo token y conservar el pedido original.
        attempt["uncertain"] = not isinstance(exc, ValueError)
        st.session_state.ropa_order_attempt = attempt
        _ui_error(exc, settings)


def _ui_create_order(db, settings):
    st.title("Crear pedido")
    st.caption("Selecciona un cliente y agrega una o varias prendas. El inventario se actualiza al confirmar el pedido.")
    notice = st.session_state.pop("ropa_order_notice", None)
    if notice:
        st.success(notice)
    pending = st.session_state.get("ropa_order_attempt")
    if pending and pending.get("uncertain"):
        st.warning("La conexión se interrumpió y el resultado del pedido está pendiente de verificación. Reintenta para recuperar el pedido o guardarlo una sola vez.")
        st.caption(f"Cliente #{pending['client']} · Fecha {pending['date']:%d/%m/%Y}")
        st.dataframe(pd.DataFrame(pending["display"]), hide_index=True, width="stretch")
        if st.button("Confirmar y guardar pedido", type="primary"):
            _ui_save_order(db, settings, pending)
        return
    clients = db.clients(status="ACTIVO")
    products = db.products()
    if clients.empty:
        st.info("Registra al menos un cliente activo para crear pedidos.")
        return
    if products.empty:
        st.info("Registra al menos una prenda para crear pedidos.")
        return
    client_map = {int(row["id_cliente"]): f"{row['nombre']} · {row['correo']} · #{int(row['id_cliente'])}" for _, row in clients.iterrows()}
    product_map = {int(row["id_producto"]): row.to_dict() for _, row in products.iterrows()}
    a, b = st.columns([2, 1])
    client = a.selectbox("Cliente del pedido", list(client_map), format_func=client_map.get, key="ropa_order_client")
    order_date = b.date_input("Fecha del pedido", value=today_bogota(), max_value=today_bogota(), key="ropa_order_date")
    available = {identifier: row for identifier, row in product_map.items() if int(row["cantidad_inventario"]) > 0}
    if "ropa_cart" not in st.session_state:
        st.session_state.ropa_cart = {}
    if available:
        with st.form("ropa_cart_add_form"):
            a, b = st.columns([4, 1])
            product = a.selectbox("Prenda", list(available), format_func=lambda identifier: _ui_product_label(available[identifier]), key="ropa_cart_product")
            quantity = b.number_input("Cantidad", min_value=1, value=1, step=1, key="ropa_cart_quantity")
            add = st.form_submit_button("Agregar al pedido")
        if add:
            total_quantity = st.session_state.ropa_cart.get(product, 0) + int(quantity)
            if total_quantity > int(available[product]["cantidad_inventario"]):
                st.error("La cantidad solicitada supera las existencias de esta prenda.")
            else:
                st.session_state.ropa_cart[product] = total_quantity
                st.rerun()
    else:
        st.info("No hay prendas con existencias disponibles.")
    cart = st.session_state.ropa_cart
    st.subheader("Detalle del pedido")
    if not cart:
        st.info("El pedido está vacío. Agrega una prenda para continuar.")
        return
    display, invalid = [], False
    for identifier, quantity in cart.items():
        row = product_map.get(identifier)
        if row is None:
            invalid = True
            display.append({"ID": identifier, "Prenda": "Producto no disponible", "Cantidad": quantity, "Precio (COP)": 0, "Subtotal (COP)": 0})
        else:
            invalid = invalid or quantity > int(row["cantidad_inventario"])
            display.append({"ID": identifier, "Prenda": row["nombre_producto"], "Talla": row["talla"], "Color": row["color"], "Cantidad": quantity, "Precio (COP)": float(row["precio_actual"]), "Subtotal (COP)": float(row["precio_actual"]) * quantity})
    frame = pd.DataFrame(display)
    st.dataframe(frame, hide_index=True, width="stretch")
    st.metric("Total estimado", money(frame["Subtotal (COP)"].sum()).removesuffix(" COP"))
    st.caption("El precio y las existencias se verifican nuevamente en Oracle al guardar.")
    a, b, c = st.columns([3, 1, 1])
    remove = a.selectbox("Quitar una prenda", list(cart), format_func=lambda identifier: f"#{identifier} · {product_map.get(identifier, {}).get('nombre_producto', 'No disponible')}", key="ropa_cart_remove")
    if b.button("Quitar prenda"):
        del cart[remove]
        st.rerun()
    if c.button("Vaciar pedido"):
        st.session_state.ropa_cart = {}
        st.session_state.pop("ropa_order_attempt", None)
        st.rerun()
    if invalid:
        st.warning("El inventario cambió. Quita las prendas sin disponibilidad y agrega cantidades válidas.")
    if st.button("Confirmar y guardar pedido", type="primary", disabled=invalid):
        items = [{"id_producto": int(identifier), "cantidad": int(quantity)} for identifier, quantity in sorted(cart.items())]
        signature = (int(client), order_date.isoformat(), tuple((item["id_producto"], item["cantidad"]) for item in items))
        attempt = st.session_state.get("ropa_order_attempt")
        if not attempt or attempt["signature"] != signature:
            attempt = {"signature": signature, "token": str(uuid.uuid4()), "client": int(client), "date": order_date, "items": items, "display": display, "uncertain": False}
            st.session_state.ropa_order_attempt = attempt
        _ui_save_order(db, settings, attempt)


def _ui_orders(db, settings):
    st.title("Consultar pedidos")
    st.caption("Busca por cliente y fechas. Consulta las prendas y los valores históricos de cada pedido.")
    dates = _ui_dates("orders")
    if dates is None:
        return
    clients = db.clients()
    choices = {None: "Todos los clientes"}
    choices.update({int(row["id_cliente"]): f"{row['nombre']} · #{int(row['id_cliente'])}" for _, row in clients.iterrows()})
    client = st.selectbox("Filtrar cliente", list(choices), format_func=choices.get, key="ropa_orders_client")
    frame = db.orders(*dates, id_cliente=client)
    _ui_table(frame, "orders", "pedidos.csv", {"numero_pedido": "Pedido", "fecha": "Fecha", "nombre_cliente": "Cliente", "lineas": "Referencias", "unidades": "Unidades", "total": "Total (COP)", "costo": "Costo (COP)", "utilidad": "Utilidad (COP)"})
    if not frame.empty:
        st.subheader("Detalle de un pedido")
        number = st.selectbox("Número de pedido", frame["numero_pedido"].tolist(), key="ropa_order_detail")
        _ui_table(db.order_lines(int(number)), "order_lines", f"pedido_{number}.csv")


def _ui_sql(db, settings):
    import json
    st.title("Laboratorio SQL")
    st.caption("Ejecuta consultas SELECT o WITH sobre Oracle. El editor trabaja en modo de lectura.")
    examples = {
        "Productos disponibles": "SELECT id_producto, nombre_producto, talla, color, cantidad_inventario\nFROM DIM_PRODUCTOS\nWHERE cantidad_inventario > 0\nORDER BY nombre_producto",
        "Clientes": "SELECT id_cliente, nombre, correo, estado\nFROM DIM_CLIENTES\nORDER BY nombre",
        "Pedidos por cliente": "SELECT p.numero_pedido, f.fecha, h.id_producto, h.cantidad, h.total_linea\nFROM PEDIDOS p\nJOIN DIM_FECHAS f ON f.id_fecha = p.id_fecha\nJOIN HECHOS_PEDIDOS h ON h.numero_pedido = p.numero_pedido\nWHERE p.id_cliente = :id_cliente\nORDER BY f.fecha DESC, p.numero_pedido, h.id_producto",
    }
    a, b = st.columns([4, 1])
    example = a.selectbox("Consulta de ejemplo", list(examples), key="ropa_sql_example")
    if b.button("Cargar ejemplo"):
        st.session_state.ropa_sql_text = examples[example]
        st.session_state.ropa_sql_params = '{"id_cliente": 1}' if example == "Pedidos por cliente" else "{}"
    st.session_state.setdefault("ropa_sql_text", examples["Productos disponibles"])
    st.session_state.setdefault("ropa_sql_params", "{}")
    with st.form("ropa_sql_form"):
        sql = st.text_area("Consulta SQL", height=220, key="ropa_sql_text")
        a, b = st.columns([3, 1])
        parameters = a.text_area("Parámetros JSON", height=90, key="ropa_sql_params")
        limit = b.number_input("Máximo de filas", min_value=1, max_value=int(settings.max_rows), value=min(1000, int(settings.max_rows)), step=1, key="ropa_sql_limit")
        execute = st.form_submit_button("Ejecutar consulta", type="primary")
    if execute:
        st.session_state.pop("ropa_sql_result", None)
        st.session_state.pop("ropa_sql_error", None)
        try:
            params = json.loads(parameters.strip() or "{}")
            if not isinstance(params, dict) or any(not isinstance(key, str) or not (value is None or isinstance(value, (str, int, float))) for key, value in params.items()):
                raise ValueError("Los parámetros deben ser un objeto JSON de textos, números o null.")
            with st.spinner("Consultando Oracle…"):
                frame = db.query(sql, params=params, limit=int(limit))
            st.session_state.ropa_sql_result = (sql, frame)
        except Exception as exc:
            st.session_state.ropa_sql_error = safe_error(exc, settings)
    if st.session_state.get("ropa_sql_error"):
        st.error(st.session_state.ropa_sql_error)
    result = st.session_state.get("ropa_sql_result")
    if result is not None:
        st.subheader("Resultado")
        st.caption(f"{len(result[1]):,} filas")
        if result[1].attrs.get("truncated"):
            st.warning("El resultado supera el límite de filas. Agrega filtros o aumenta el límite.")
        with st.expander("Consulta ejecutada"):
            st.code(result[0], language="sql")
        _ui_table(result[1], "sql", "consulta_oracle.csv")
    with st.expander("Diccionario de datos"):
        st.dataframe(db.metadata(), hide_index=True, width="stretch")


def render_ui(public_url=""):
    st.set_page_config(page_title="Ropa · Inventario y ventas", page_icon="◈", layout="wide")
    _ui_style()
    try:
        settings = load_settings()
    except Exception:
        st.error("No se pudo leer la configuración de .env. Revisa los campos y sus valores.")
        return
    # Cambiar límites o credenciales del túnel no debe descartar un pedido
    # pendiente de verificar. Solo un cambio de base invalida la sesión.
    import hashlib
    identity = (getattr(settings, "user", ""), settings.owner,
                getattr(settings, "dsn", ""), getattr(settings, "wallet_dir", ""))
    signature = hashlib.sha256(repr(identity).encode()).hexdigest()
    if st.session_state.get("ropa_settings_signature") != signature:
        for key in list(st.session_state):
            if key.startswith("ropa_") and key != "ropa_page":
                del st.session_state[key]
        st.session_state.ropa_settings_signature = signature
    db = Database(settings)
    with st.sidebar:
        st.markdown('<div class="ropa-brand">ropa.</div><div class="ropa-caption">INVENTARIO · VENTAS · CONTABILIDAD</div>', unsafe_allow_html=True)
        page = st.radio("Navegación", ["Resumen y contabilidad", "Clientes", "Productos e inventario", "Crear pedido", "Consultar pedidos", "Laboratorio SQL"], key="ropa_page", label_visibility="collapsed")
        st.divider()
        st.caption(f"Oracle Database · {settings.owner or 'sin configurar'}")
        if public_url:
            st.link_button("Abrir enlace compartido", public_url, width="stretch")
            st.caption("El acceso compartido requiere usuario y contraseña.")
        if st.button("Actualizar datos", key="ropa_refresh"):
            st.session_state.pop("ropa_sql_result", None)
            st.session_state.pop("ropa_sql_error", None)
            st.rerun()
        with st.expander("Conexión y configuración"):
            st.caption("La configuración se lee de .env. Las credenciales no se muestran en pantalla.")
            if st.button("Probar conexión", disabled=not settings.ready, key="ropa_test_connection"):
                try:
                    result = db.test_connection()
                    st.success("Conexión Oracle correcta")
                    st.write(result)
                except Exception as exc:
                    _ui_error(exc, settings)
    if not settings.ready:
        st.title("Conecta tu base de datos")
        st.warning("Completa USER_DB, DB_PASSWORD y DB_DSN en .env para usar el dashboard.")
        st.info("Para Oracle con wallet, configura también WALLET_DIR y WALLET_KEY.")
        return
    try:
        missing = db.schema_status()
        if missing:
            st.warning("La base todavía no tiene todas las tablas del proyecto de ropa.")
            st.write("Tablas faltantes: " + ", ".join(missing))
            return
        handlers = {"Resumen y contabilidad": _ui_report, "Clientes": _ui_clients, "Productos e inventario": _ui_products, "Crear pedido": _ui_create_order, "Consultar pedidos": _ui_orders, "Laboratorio SQL": _ui_sql}
        handlers[page](db, settings)
    except Exception as exc:
        _ui_error(exc, settings)
        st.info("Revisa la conexión y vuelve a actualizar los datos. Los cambios pendientes del pedido se conservan.")

# ---- Arranque y ciclo de vida del túnel, independiente de los reruns de la UI ----

def ngrok_policy(settings: Settings) -> str:
    if not settings.ngrok_token:
        raise ValueError('Completa NGROK_AUTHTOKEN en .env para activar el enlace compartible.')
    if not settings.ngrok_user or ':' in settings.ngrok_user or any(ord(c)<32 for c in settings.ngrok_user):
        raise ValueError('Completa NGROK_BASIC_USER en .env (sin dos puntos ni caracteres de control).')
    if not 8 <= len(settings.ngrok_password) <= 128:
        raise ValueError('NGROK_BASIC_PASSWORD debe tener entre 8 y 128 caracteres.')
    if '${' in settings.ngrok_user or '${' in settings.ngrok_password:
        raise ValueError('Las credenciales de acceso ngrok no pueden contener la secuencia ${, reservada por ngrok.')
    return json.dumps({'on_http_request':[{'actions':[{'type':'basic-auth','config':{
        'credentials':[settings.ngrok_user+':'+settings.ngrok_password], 'enforce':True,
        'realm':'Marca de ropa'}}]}]})


def require_free_port(port):
    with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as probe:
        try:
            probe.bind(('127.0.0.1',port))
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                raise ValueError(f'El puerto {port} está ocupado. Cierra la instancia anterior o cambia STREAMLIT_PORT en .env.') from None
            raise ValueError(f'No se pudo abrir el puerto {port}. Revisa los permisos de red del proceso.') from None


def streamlit_command(settings, public_url=''):
    command = [sys.executable,'-m','streamlit','run',str(ROOT/'app.py'),
               '--server.address=127.0.0.1',f'--server.port={settings.port}',
               '--server.headless=true','--server.enableCORS=true','--server.enableXsrfProtection=true',
               '--browser.gatherUsageStats=false']
    if public_url:
        parsed = urlparse(public_url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('ngrok devolvió una URL HTTPS no válida.')
        command += [f'--browser.serverAddress={parsed.hostname}','--browser.serverPort=443']
    command += ['--','--ui']
    if public_url:
        command += ['--public-url',public_url]
    return command


def wait_for_server(child,port,timeout=30):
    deadline = time.monotonic()+timeout
    while time.monotonic()<deadline:
        if child.poll() is not None:
            raise RuntimeError('Streamlit terminó antes de completar el arranque.')
        try:
            with urlopen(f'http://127.0.0.1:{port}/_stcore/health',timeout=1) as response:
                if response.status == 200:
                    return
        except (OSError,TimeoutError):
            pass
        time.sleep(.2)
    raise RuntimeError('Streamlit no respondió en 30 segundos; revisa el puerto y el entorno virtual.')


def launch(settings: Settings) -> int:
    require_free_port(settings.port)
    ngrok_sdk, public_url, child = None,'',None
    cleaned = False
    old_handlers = {}

    def cleanup():
        nonlocal cleaned
        if cleaned:
            return
        cleaned=True
        if ngrok_sdk is not None and public_url:
            try:
                ngrok_sdk.disconnect(public_url)
            except Exception:
                pass
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)

    def stop(signum,frame):
        raise KeyboardInterrupt

    atexit.register(cleanup)
    try:
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT,signal.SIGTERM):
                old_handlers[signum]=signal.signal(signum,stop)
        if settings.ngrok_enabled:
            try:
                policy=ngrok_policy(settings)
                import ngrok as sdk
                ngrok_sdk=sdk
                listener=sdk.forward(f'127.0.0.1:{settings.port}',authtoken=settings.ngrok_token,
                                     schemes=['HTTPS'],traffic_policy=policy)
                public_url=listener.url()
                # Valida la URL antes de pasarla al subproceso.
                streamlit_command(settings,public_url)
            except Exception as exc:
                if public_url and ngrok_sdk is not None:
                    try:ngrok_sdk.disconnect(public_url)
                    except Exception:pass
                public_url=''
                print('Acceso local únicamente. '+safe_error(exc,settings),flush=True)
        child=subprocess.Popen(streamlit_command(settings,public_url),cwd=str(ROOT),start_new_session=True)
        wait_for_server(child,settings.port)
        print(f'\nDashboard local: http://127.0.0.1:{settings.port}',flush=True)
        if public_url:
            print(f'Enlace para compartir: {public_url}\nAcceso protegido por usuario y contraseña.',flush=True)
        print('Mantén esta terminal abierta. Ctrl+C cierra Streamlit y el túnel.\n',flush=True)
        return child.wait()
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print('No se pudo iniciar la aplicación: '+safe_error(exc,settings),file=sys.stderr,flush=True)
        return 1
    finally:
        cleanup()
        atexit.unregister(cleanup)
        for signum,handler in old_handlers.items():
            signal.signal(signum,handler)


def main():
    parser=argparse.ArgumentParser(description='Dashboard de ropa con Oracle y ngrok; configuración en .env.')
    parser.add_argument('--ui',action='store_true',help=argparse.SUPPRESS)
    parser.add_argument('--public-url',default='',help=argparse.SUPPRESS)
    args=parser.parse_args()
    if args.ui:
        render_ui(public_url=args.public_url)
        return 0
    try:
        settings=load_settings()
        return launch(settings)
    except Exception as exc:
        # Sin Settings válido no imprimir valores del .env ni errores completos de parsing.
        print(f'No se pudo cargar o iniciar el proyecto: {type(exc).__name__}. Revisa .env y el puerto configurado.',file=sys.stderr)
        return 1


if __name__=='__main__':
    main_result=main()
    if '--ui' not in sys.argv:
        raise SystemExit(main_result)
