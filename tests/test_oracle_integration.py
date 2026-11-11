"""Pruebas reales opt-in: python tests/test_oracle_integration.py --oracle.

Sin --oracle unittest discover solo informa pruebas omitidas. Cada prueba crea
fixtures TEST_ unicos, usa fechas historicas vacias y elimina exclusivamente sus
filas por IDs/tokens exactos. Requiere el modelo ya creado; no migra ni limpia el
esquema. Con --oracle un problema de conexion es un error, nunca un skip.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
import sys
import threading
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import app
import oracledb

RUN_ORACLE = '--oracle' in sys.argv


@unittest.skipUnless(RUN_ORACLE, 'Oracle real requiere --oracle explicito.')
class OracleIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = replace(app.load_settings(), pool_max=4)
        cls.db = app.Database(cls.settings)
        try:
            cls.db.test_connection()
            missing = cls.db.schema_status()
            if missing:
                raise RuntimeError('Faltan tablas: ' + ', '.join(missing))
        except Exception as exc:
            raise RuntimeError(app.safe_error(exc, cls.settings)) from None

    @contextmanager
    def direct(self):
        with app.connect_oracle(self.settings) as conn:
            conn.current_schema = self.settings.owner
            conn.call_timeout = self.settings.timeout * 1000
            conn.autocommit = False
            try:
                yield conn
            finally:
                conn.rollback()

    def setUp(self):
        self.tag = 'TEST_' + uuid.uuid4().hex[:16]
        self.emails = []
        self.product_names = []
        self.tokens = []
        self.dates_to_clean = set()
        # Escoge dos meses historicos sin fechas existentes ni ventas. No borra
        # una dimension fecha que pertenecia a otra actividad o a la semilla.
        first = uuid.uuid4().int % 240
        with self.direct() as conn:
            with conn.cursor() as cur:
                for offset in range(240):
                    month = (first + offset) % 240
                    start = date(1990 + month // 12, month % 12 + 1, 1)
                    following = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
                    end = (following.replace(day=28) + timedelta(days=4)).replace(day=1)
                    cur.execute('SELECT COUNT(*) FROM dim_fechas WHERE fecha>=:a AND fecha<:b',
                                {'a': datetime.combine(start, datetime.min.time()),
                                 'b': datetime.combine(end, datetime.min.time())})
                    if cur.fetchone()[0] == 0:
                        self.start = start
                        self.sale_date = following - timedelta(days=1)
                        self.second_date = following
                        self.period_end = end - timedelta(days=1)
                        break
                else:
                    self.fail('No hay un periodo historico vacio para fixtures aislados.')

    def tearDown(self):
        # Se registran nombres/correos/tokens ANTES de insertar para poder
        # recuperar sus IDs incluso si se perdio una respuesta tras COMMIT.
        with self.direct() as conn:
            with conn.cursor() as cur:
                for token in self.tokens:
                    cur.execute('SELECT numero_pedido FROM pedidos WHERE token_solicitud=:t', {'t': token})
                    for (number,) in cur.fetchall():
                        cur.execute('DELETE FROM hechos_pedidos WHERE numero_pedido=:n', {'n': number})
                        cur.execute('DELETE FROM pedidos WHERE numero_pedido=:n', {'n': number})
                for name in self.product_names:
                    cur.execute('SELECT id_producto FROM dim_productos WHERE nombre_producto=:n', {'n': name})
                    for (identifier,) in cur.fetchall():
                        cur.execute('DELETE FROM dim_productos WHERE id_producto=:id', {'id': identifier})
                for email in self.emails:
                    cur.execute('SELECT id_cliente FROM dim_clientes WHERE correo=:e', {'e': email})
                    for (identifier,) in cur.fetchall():
                        cur.execute('DELETE FROM dim_clientes WHERE id_cliente=:id', {'id': identifier})
                for identifier in self.dates_to_clean:
                    cur.execute('''DELETE FROM dim_fechas f WHERE f.id_fecha=:id
                        AND NOT EXISTS (SELECT 1 FROM pedidos p WHERE p.id_fecha=f.id_fecha)
                        AND NOT EXISTS (SELECT 1 FROM hechos_pedidos h WHERE h.id_fecha=f.id_fecha)''',
                                {'id': identifier})
            conn.commit()

    def client(self):
        email = f'{self.tag.lower()}_{len(self.emails)}@example.com'
        self.emails.append(email)
        return self.db.create_client(self.tag, email, '3000000000', 'Direccion ficticia TEST_')

    def product(self, stock=10, price='100.25', cost='60.10'):
        name = f'{self.tag}_{len(self.product_names)}'
        self.product_names.append(name)
        return self.db.create_product(name, 'camiseta', 'M', 'Negro',
                                      Decimal(price), Decimal(cost), stock)

    def token(self):
        token = str(uuid.uuid4())
        self.tokens.append(token)
        return token

    def order(self, client, items, when=None, token=None):
        when = when or self.sale_date
        self.dates_to_clean.add(int(when.strftime('%Y%m%d')))
        return self.db.create_order(client, when, items, token or self.token())

    def stock(self, product):
        with self.direct() as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT cantidad_inventario FROM dim_productos WHERE id_producto=:p', {'p': product})
                return int(cur.fetchone()[0])

    def assert_rejected(self, sql, params, *codes):
        with self.direct() as conn:
            with conn.cursor() as cur:
                with self.assertRaises(oracledb.DatabaseError) as raised:
                    cur.execute(sql, params)
                self.assertIn(getattr(raised.exception.args[0], 'code', None), codes)

    def test_constraints_reject_invalid_catalog_and_normalized_duplicates(self):
        client = self.client()
        product = self.product()
        with self.assertRaises(oracledb.IntegrityError):
            self.db.create_client(self.tag, '  ' + self.emails[0].upper() + '  ',
                                  '3000000000', 'Direccion ficticia TEST_')
        self.assert_rejected('UPDATE dim_clientes SET correo=UPPER(correo) WHERE id_cliente=:id',
                             {'id': client}, 2290)
        duplicate_name = self.product_names[0].lower()
        self.product_names.append(duplicate_name)
        with self.assertRaises(oracledb.IntegrityError):
            self.db.create_product(duplicate_name, 'camiseta', 'M', 'NEGRO',
                                   Decimal('100'), Decimal('60'), 1)
        for column, value in [('categoria', 'zapato'), ('talla', 'XX'),
                              ('costo_actual', -1), ('precio_actual', -1),
                              ('cantidad_inventario', -1), ('cantidad_inventario', Decimal('1.5'))]:
            with self.subTest(column=column, value=value):
                self.assert_rejected(f'UPDATE dim_productos SET {column}=:v WHERE id_producto=:id',
                                     {'v': value, 'id': product}, 2290)
        self.assertEqual(self.stock(product), 10)

    def test_fact_constraints_composite_fk_and_virtual_total(self):
        client, other = self.client(), self.client()
        product = self.product()
        result = self.order(client, [{'id_producto': product, 'cantidad': 2}])
        for column, value, code in [('cantidad', 0, 2290), ('cantidad', Decimal('1.5'), 2290),
                                   ('precio_unitario_venta', -1, 2290), ('costo_unitario_venta', -1, 2290),
                                   ('id_producto', -999999999, 2291), ('id_cliente', other, 2291),
                                   ('total_linea', 0, 54017)]:
            with self.subTest(column=column, value=value):
                self.assert_rejected(f'UPDATE hechos_pedidos SET {column}=:v WHERE numero_pedido=:n',
                                     {'v': value, 'n': result.numero_pedido}, code)
        lines = self.db.order_lines(result.numero_pedido)
        self.assertEqual(Decimal(str(lines.iloc[0].total_linea)), Decimal('200.50'))
        self.assert_rejected('DELETE FROM dim_clientes WHERE id_cliente=:id', {'id': client}, 2292)
        self.assert_rejected('INSERT INTO dim_fechas(id_fecha,fecha) VALUES(:id,:fecha)',
                             {'id': -1, 'fecha': datetime.combine(self.start, datetime.min.time())}, 2290)

    def test_multiple_products_and_repeated_cart_rows(self):
        client = self.client()
        first = self.product(10, '120.50', '70.25')
        second = self.product(8, '80.00', '30.00')
        result = self.order(client, [{'id_producto': first, 'cantidad': 1},
                                     {'id_producto': first, 'cantidad': 1},
                                     {'id_producto': second, 'cantidad': 3}])
        lines = self.db.order_lines(result.numero_pedido)
        self.assertEqual(len(lines), 2)
        self.assertEqual(result.total, Decimal('481.00'))
        self.assertEqual((self.stock(first), self.stock(second)), (8, 5))
        orders = self.db.orders(self.sale_date, self.sale_date, client)
        self.assertEqual(list(orders.numero_pedido.astype(int)), [result.numero_pedido])
        self.assertEqual(int(orders.iloc[0].unidades), 5)

    def test_insufficient_stock_rolls_back_entire_order(self):
        client = self.client()
        first, second = self.product(10), self.product(1)
        token = self.token()
        with self.assertRaisesRegex(ValueError, 'Existencias insuficientes'):
            self.order(client, [{'id_producto': first, 'cantidad': 2},
                                {'id_producto': second, 'cantidad': 2}], token=token)
        self.assertEqual((self.stock(first), self.stock(second)), (10, 1))
        with self.direct() as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT COUNT(*) FROM pedidos WHERE token_solicitud=:t', {'t': token})
                self.assertEqual(cur.fetchone()[0], 0)
                cur.execute('SELECT COUNT(*) FROM hechos_pedidos WHERE id_cliente=:c', {'c': client})
                self.assertEqual(cur.fetchone()[0], 0)

    def test_repeated_token_is_idempotent_and_rejects_changed_payload(self):
        client, product, token = self.client(), self.product(), self.token()
        items = [{'id_producto': product, 'cantidad': 2}]
        first = self.order(client, items, token=token)
        second = self.order(client, items, token=token)
        self.assertEqual(first.numero_pedido, second.numero_pedido)
        self.assertTrue(second.reused)
        self.assertEqual(second.total, first.total)
        self.assertEqual(self.stock(product), 8)
        with self.assertRaisesRegex(ValueError, 'otro pedido confirmado'):
            self.order(client, [{'id_producto': product, 'cantidad': 3}], token=token)
        self.assertEqual(self.stock(product), 8)

    def test_concurrent_last_unit_has_one_winner(self):
        clients = [self.client(), self.client()]
        product = self.product(1)
        tokens = [self.token(), self.token()]
        self.dates_to_clean.add(int(self.sale_date.strftime('%Y%m%d')))
        barrier = threading.Barrier(2)

        def buy(index):
            barrier.wait(timeout=10)
            try:
                return self.db.create_order(clients[index], self.sale_date,
                                            [{'id_producto': product, 'cantidad': 1}], tokens[index])
            except ValueError as exc:
                return exc

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(buy, i) for i in range(2)]
            outcomes = [future.result(timeout=45) for future in futures]
        winners = [x for x in outcomes if isinstance(x, app.OrderResult)]
        losers = [x for x in outcomes if isinstance(x, ValueError)]
        self.assertEqual(len(winners), 1, outcomes)
        self.assertEqual(len(losers), 1, outcomes)
        self.assertIn('Existencias insuficientes', str(losers[0]))
        self.assertEqual(self.stock(product), 0)

    def test_price_snapshots_and_monthly_reports(self):
        client, product = self.client(), self.product()
        first = self.order(client, [{'id_producto': product, 'cantidad': 2}])
        with self.direct() as conn:
            with conn.cursor() as cur:
                cur.execute('UPDATE dim_productos SET precio_actual=150.50,costo_actual=70.20 WHERE id_producto=:p',
                            {'p': product})
            conn.commit()
        self.order(client, [{'id_producto': product, 'cantidad': 1}], when=self.second_date)
        previous = self.db.order_lines(first.numero_pedido).iloc[0]
        self.assertEqual(Decimal(str(previous.precio_unitario_venta)), Decimal('100.25'))
        self.assertEqual(Decimal(str(previous.costo_unitario_venta)), Decimal('60.10'))
        report = self.db.report(self.start, self.period_end)
        summary = report['summary']
        self.assertEqual(summary['facturacion'], Decimal('351.00'))
        self.assertEqual(summary['costo'], Decimal('190.40'))
        self.assertEqual(summary['utilidad'], Decimal('160.60'))
        self.assertEqual((summary['pedidos'], summary['unidades']), (2, 3))
        self.assertAlmostEqual(summary['margen'], float(Decimal('160.60') / Decimal('351') * 100))
        self.assertEqual(list(report['monthly'].facturacion), [200.5, 150.5])
        self.assertEqual(int(report['top'].iloc[0].unidades), 3)
        self.assertEqual(report['top'].iloc[0].nombre_producto, self.product_names[0])
        self.assertEqual(len(self.db.orders(self.start, self.period_end, client)), 2)
        self.assertEqual(len(self.db.orders(self.second_date, self.second_date, client)), 1)
        empty = self.db.report(self.start, self.start)
        self.assertEqual(empty['summary']['facturacion'], 0)
        self.assertEqual(empty['summary']['margen'], 0)
        self.assertTrue(empty['top'].empty)


if __name__ == '__main__':
    if RUN_ORACLE:
        sys.argv.remove('--oracle')
    unittest.main(verbosity=2)
