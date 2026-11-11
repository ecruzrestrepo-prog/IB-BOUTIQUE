import json
import tempfile
import unittest
import uuid
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import app


class ConfigurationTests(unittest.TestCase):
    def test_env_only_literal_secrets_paths_and_repr(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'.env').write_text('USER_DB=file_user\nDB_PASSWORD=" literal${TOKEN}# value "\nDB_DSN=service\nWALLET_DIR=wallet\nNGROK_ENABLED=false\n')
            with patch.dict('os.environ',{'USER_DB':'wrong','DB_PASSWORD':'wrong'}):
                settings=app.load_settings(root)
            self.assertEqual(settings.user,'file_user')
            self.assertEqual(settings.password,' literal${TOKEN}# value ')
            self.assertEqual(settings.wallet_dir,str(root/'wallet'))
            self.assertTrue(settings.ready)
            self.assertFalse(settings.ngrok_enabled)
            self.assertNotIn(settings.password,repr(settings))

    def test_bad_settings_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            for text in ('STREAMLIT_PORT=1','NGROK_ENABLED=yes','ORACLE_MODE=other',
                         'QUERY_TIMEOUT_SECONDS=301','MAX_ROWS=0','POOL_MAX_CONNECTIONS=abc'):
                with self.subTest(text=text):
                    Path(directory,'.env').write_text(text)
                    with self.assertRaises(ValueError):app.load_settings(Path(directory))

    def test_errors_do_not_expose_raw_or_escaped_secrets(self):
        settings=app.Settings(password='database-secret',wallet_key='wallet-secret',ngrok_password='p"ass\\word',ngrok_token='token-secret')
        for value in (settings.password,settings.wallet_key,settings.ngrok_password,settings.ngrok_token,
                      json.dumps(settings.ngrok_password)[1:-1]):
            with self.subTest(value=value):
                self.assertNotIn(value,app.safe_error(RuntimeError('failure: '+value),settings))
        self.assertIn('Available',app.safe_error(RuntimeError('DPY-6001 unknown service'),settings))

    def test_validation_money_and_integer(self):
        self.assertEqual(app.integer('3'),3)
        self.assertEqual(app.integer(0,minimum=0),0)
        for value in (True,'NaN','Infinity',1.5,-1,1000000000):
            with self.subTest(value=value),self.assertRaises(ValueError):app.integer(value)
        self.assertEqual(app.amount('12.50'),Decimal('12.50'))
        for value in ('NaN','-1','1.001'):
            with self.subTest(value=value),self.assertRaises(ValueError):app.amount(value)
        self.assertEqual(app.money(Decimal('1234.56')),'$ 1.234,56 COP')
        self.assertEqual(app.money(1000),'$ 1.000 COP')

    def test_inclusive_date_range(self):
        bounds=app.date_bounds(date(2026,1,31),date(2026,2,1))
        self.assertEqual(bounds['end_date'],datetime(2026,2,2))
        with self.assertRaises(ValueError):app.date_bounds(date(2026,2,1),date(2026,1,1))


class SQLTests(unittest.TestCase):
    def test_valid_queries(self):
        for text in ('SELECT * FROM dim_productos',
                     "-- comment\nSELECT 'DELETE;UPDATE' x FROM dual; -- final",
                     'WITH x AS (SELECT 1 n FROM dual) SELECT * FROM x',
                     'SELECT 1 FROM dual UNION ALL SELECT 2 FROM dual',
                     "SELECT TO_CHAR(fecha,'YYYY-MM'),COUNT(*) FROM dim_fechas GROUP BY TO_CHAR(fecha,'YYYY-MM')"):
            with self.subTest(text=text):self.assertTrue(app.validate_select(text))

    def test_writes_locks_functions_and_links_rejected(self):
        for text in ('','DELETE FROM pedidos','UPDATE dim_productos SET cantidad_inventario=0',
                     'DROP TABLE pedidos','SELECT * INTO destino FROM pedidos','BEGIN NULL; END;',
                     'SELECT 1 FROM dual; DELETE FROM pedidos','SELECT * FROM pedidos FOR UPDATE',
                     'SELECT seq.NEXTVAL FROM dual','SELECT custom_function() FROM dual',
                     "SELECT DBMS_XMLGEN.GETXML('select 1 from dual') FROM dual",'SELECT * FROM pedidos@remote'):
            with self.subTest(text=text),self.assertRaises(ValueError):app.validate_select(text)

    def test_result_limit_duplicate_columns_and_readonly(self):
        conn,cur=MagicMock(),MagicMock()
        conn.cursor.return_value.__enter__.return_value=cur
        cur.description=[('TOTAL',),('TOTAL',)]
        cur.fetchmany.return_value=[(1,2),(3,4),(5,6)]
        pool=MagicMock()
        pool.acquire.return_value.__enter__.return_value=conn
        with patch.object(app,'oracle_pool',return_value=pool):
            frame=app.Database(app.Settings()).query('SELECT 1 total,2 total FROM dual',limit=2)
        self.assertEqual(frame.columns.tolist(),['total','total_2'])
        self.assertEqual(len(frame),2)
        self.assertTrue(frame.attrs['truncated'])
        self.assertEqual(cur.execute.call_args_list[0].args[0],'SET TRANSACTION READ ONLY')
        self.assertGreaterEqual(conn.rollback.call_count,2)
        conn.commit.assert_not_called()


class OrderTests(unittest.TestCase):
    def connection_fixture(self):
        conn,cur=MagicMock(),MagicMock()
        conn.cursor.return_value.__enter__.return_value=cur
        output=MagicMock();output.getvalue.return_value=[42]
        cur.var.return_value=output
        cur.rowcount=1
        pool=MagicMock();pool.acquire.return_value.__enter__.return_value=conn
        return conn,cur,pool

    def test_success_locks_sorted_saves_snapshots_and_commits_once(self):
        conn,cur,pool=self.connection_fixture()
        cur.fetchone.side_effect=[None,('ACTIVO',),(20260101,),('A',5,Decimal('10.50'),Decimal('4.25')),('B',8,Decimal('20.00'),Decimal('8.00'))]
        with patch.object(app,'oracle_pool',return_value=pool):
            result=app.Database(app.Settings()).create_order(1,date(2026,1,1),[
                {'id_producto':9,'cantidad':2},{'id_producto':3,'cantidad':1},{'id_producto':3,'cantidad':1}],str(uuid.uuid4()))
        self.assertEqual(result.total,Decimal('61.00'))
        conn.commit.assert_called_once()
        calls=cur.execute.call_args_list
        locks=[c.args[1]['id'] for c in calls if 'FROM dim_productos WHERE' in c.args[0]]
        self.assertEqual(locks,[3,9])
        lines=[c.args[1] for c in calls if 'INSERT INTO hechos_pedidos' in c.args[0]]
        self.assertEqual(lines[0]['precio'],Decimal('10.50'))
        self.assertEqual(lines[0]['cantidad'],2)
        self.assertEqual(len(lines),2)

    def test_insufficient_stock_rolls_back_without_lines(self):
        conn,cur,pool=self.connection_fixture()
        cur.fetchone.side_effect=[None,('ACTIVO',),(20260101,),('Prenda',0,10,4)]
        with patch.object(app,'oracle_pool',return_value=pool),self.assertRaisesRegex(ValueError,'Existencias insuficientes'):
            app.Database(app.Settings()).create_order(1,date(2026,1,1),[{'id_producto':3,'cantidad':1}],str(uuid.uuid4()))
        conn.commit.assert_not_called()
        self.assertGreaterEqual(conn.rollback.call_count,2)
        self.assertFalse(any('INSERT INTO hechos_pedidos' in c.args[0] for c in cur.execute.call_args_list))

    def test_replayed_token_does_not_write_even_when_stock_changed(self):
        conn,cur,pool=self.connection_fixture()
        cur.fetchone.return_value=(42,1,20260101)
        cur.fetchall.return_value=[(3,2,Decimal('10.50'))]
        with patch.object(app,'oracle_pool',return_value=pool):
            result=app.Database(app.Settings()).create_order(1,date(2026,1,1),[{'id_producto':3,'cantidad':2}],str(uuid.uuid4()))
        self.assertTrue(result.reused)
        self.assertEqual(result.total,Decimal('21.00'))
        conn.commit.assert_not_called()
        self.assertFalse(any('INSERT' in c.args[0] or 'UPDATE' in c.args[0] for c in cur.execute.call_args_list))

    def test_token_cannot_be_reused_for_different_payload(self):
        conn,cur,pool=self.connection_fixture()
        cur.fetchone.return_value=(42,1,20260101)
        cur.fetchall.return_value=[(3,2,10)]
        with patch.object(app,'oracle_pool',return_value=pool),self.assertRaisesRegex(ValueError,'otro pedido'):
            app.Database(app.Settings()).create_order(1,date(2026,1,1),[{'id_producto':3,'cantidad':3}],str(uuid.uuid4()))
        conn.commit.assert_not_called()

    def test_invalid_order_never_opens_connection(self):
        for items in ([],[{'id_producto':1,'cantidad':1.5}],[{'id_producto':1,'cantidad':0}]):
            with patch.object(app,'oracle_pool') as pool,self.assertRaises(ValueError):
                app.Database(app.Settings()).create_order(1,date(2026,1,1),items,str(uuid.uuid4()))
            pool.assert_not_called()


class ReportTests(unittest.TestCase):
    def test_empty_months_zero_margin_single_snapshot(self):
        conn,cur=MagicMock(),MagicMock()
        conn.cursor.return_value.__enter__.return_value=cur
        cur.fetchone.return_value=(0,0,0,0)
        cur.fetchall.side_effect=[[],[]]
        descriptions=iter([
            [('mes',),('facturacion',),('costo',),('utilidad',)],
            [('nombre_producto',),('unidades',),('facturacion',),('utilidad',)]])
        def frames(cursor,rows):
            import pandas as pd
            return pd.DataFrame(rows,columns=[d[0] for d in next(descriptions)])
        pool=MagicMock();pool.acquire.return_value.__enter__.return_value=conn
        with patch.object(app,'oracle_pool',return_value=pool),patch.object(app,'result_frame',side_effect=frames):
            report=app.Database(app.Settings()).report(date(2026,1,15),date(2026,3,2))
        self.assertEqual(report['summary']['margen'],0)
        self.assertEqual(len(report['monthly']),3)
        self.assertEqual(report['monthly']['facturacion'].sum(),0)
        self.assertTrue(report['top'].empty)
        pool.acquire.assert_called_once()


if __name__=='__main__':unittest.main()
