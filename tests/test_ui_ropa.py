"""Flujos del dashboard de ropa con Oracle sustituido por dobles de prueba."""
import unittest
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
from streamlit.testing.v1 import AppTest


class RopaUITests(unittest.TestCase):
    def setUp(self):
        self.settings = SimpleNamespace(ready=True, owner="ROPA_TEST", max_rows=1000)
        self.db = MagicMock()
        self.db.schema_status.return_value = []
        self.db.clients.return_value = pd.DataFrame([
            {"id_cliente": 1, "nombre": "Cliente de prueba", "correo": "cliente@example.test", "telefono": "3000000000", "direccion": "Calle de prueba", "estado": "ACTIVO"},
        ])
        self.db.products.return_value = pd.DataFrame([
            {"id_producto": 1, "nombre_producto": "Camiseta básica", "categoria": "Camiseta", "talla": "M", "color": "Blanco", "precio_actual": 50000, "costo_actual": 25000, "cantidad_inventario": 4},
            {"id_producto": 2, "nombre_producto": "Pantaloneta negra", "categoria": "Pantaloneta", "talla": "S", "color": "Negro", "precio_actual": 60000, "costo_actual": 30000, "cantidad_inventario": 0},
        ])
        self.db.report.return_value = {
            "summary": {"facturacion": 100000, "costo": 50000, "utilidad": 50000, "pedidos": 1, "unidades": 2, "margen": 50},
            "monthly": pd.DataFrame([{"mes": "2026-11", "facturacion": 100000, "costo": 50000, "utilidad": 50000}]),
            "top": pd.DataFrame([{"nombre_producto": "Camiseta básica", "unidades": 2, "facturacion": 100000, "utilidad": 50000}]),
        }
        self.db.orders.return_value = pd.DataFrame([{"numero_pedido": 10, "fecha": date(2026, 11, 5), "nombre_cliente": "Cliente de prueba", "lineas": 1, "unidades": 2, "total": 100000, "costo": 50000, "utilidad": 50000}])
        self.db.order_lines.return_value = pd.DataFrame([{"nombre_producto": "Camiseta básica", "talla": "M", "color": "Blanco", "cantidad": 2, "precio_unitario_venta": 50000, "costo_unitario_venta": 25000, "total_linea": 100000}])
        self.db.metadata.return_value = pd.DataFrame([{"table_name": "PRODUCTO", "column_name": "ID_PRODUCTO", "data_type": "NUMBER", "nullable": "N"}])
        self.db.query.return_value = pd.DataFrame([{"total": 2}])
        self.db.create_client.return_value = 2
        self.db.create_product.return_value = 3
        self.db.create_order.return_value = SimpleNamespace(numero_pedido=11, total=100000, reused=False)
        self.db.test_connection.return_value = {"user": "ROPA_TEST", "schema": "ROPA_TEST"}
        for target, kwargs in [
            ("app.load_settings", {"return_value": self.settings}),
            ("app.Database", {"return_value": self.db}),
            ("app.safe_error", {"side_effect": lambda exc, settings: str(exc)}),
            ("app.today_bogota", {"return_value": date(2026, 11, 5)}),
        ]:
            started = patch(target, **kwargs)
            started.start()
            self.addCleanup(started.stop)
        self.app = AppTest.from_string("import app\napp.render_ui()", default_timeout=15).run()

    def healthy(self):
        self.assertEqual([item.message for item in self.app.exception], [])
        self.assertEqual([item.value for item in self.app.error], [])

    def navigate(self, name):
        self.app.radio(key="ropa_page").set_value(name).run()
        self.healthy()

    def button(self, label):
        return next(item for item in self.app.button if item.label == label)

    def test_all_six_pages_render(self):
        for name in ["Resumen y contabilidad", "Clientes", "Productos e inventario", "Crear pedido", "Consultar pedidos", "Laboratorio SQL"]:
            with self.subTest(name=name):
                self.navigate(name)

    def test_oracle_decimal_values_render_inventory_and_cart(self):
        frame = self.db.products.return_value.copy()
        for column in ("id_producto", "precio_actual", "costo_actual", "cantidad_inventario"):
            frame[column] = frame[column].map(lambda value: Decimal(str(value)))
        self.db.products.return_value = frame
        self.navigate("Productos e inventario")
        self.assertIn("$ 100.000", [item.value for item in self.app.metric])
        self.navigate("Crear pedido")
        self.button("Agregar al pedido").click().run()
        self.healthy()

    def test_client_form_submits_and_preserves_values_on_error(self):
        self.navigate("Clientes")
        self.app.text_input(key="ropa_client_nombre").set_value("Ana de prueba")
        self.app.text_input(key="ropa_client_correo").set_value("ana@example.test")
        self.app.text_input(key="ropa_client_telefono").set_value("3010000000")
        self.app.text_input(key="ropa_client_direccion").set_value("Calle 1")
        self.db.create_client.side_effect = ValueError("Correo duplicado")
        self.button("Guardar cliente").click().run()
        self.assertEqual(self.app.text_input(key="ropa_client_nombre").value, "Ana de prueba")
        self.assertIn("Correo duplicado", [item.value for item in self.app.error])
        self.db.create_client.assert_called_once_with("Ana de prueba", "ana@example.test", "3010000000", "Calle 1", estado="ACTIVO")

    def test_product_form_submits_prices_and_stock(self):
        self.navigate("Productos e inventario")
        self.app.text_input(key="ropa_product_nombre").set_value("Top básico")
        self.app.text_input(key="ropa_product_color").set_value("Negro")
        self.app.number_input(key="ropa_product_precio").set_value(75000.0)
        self.app.number_input(key="ropa_product_costo").set_value(35000.0)
        self.app.number_input(key="ropa_product_stock").set_value(10)
        self.button("Guardar prenda").click().run()
        self.healthy()
        args = self.db.create_product.call_args.args
        self.assertEqual(args[0], "Top básico")
        self.assertEqual(args[3:], ("Negro", 75000.0, 35000.0, 10))

    def test_cart_rejects_excess_quantity(self):
        self.navigate("Crear pedido")
        self.app.number_input(key="ropa_cart_quantity").set_value(5)
        self.button("Agregar al pedido").click().run()
        self.assertTrue(self.app.error)
        self.assertEqual(self.app.session_state.ropa_cart, {})
        self.db.create_order.assert_not_called()

    def test_order_retry_reuses_token_and_clears_cart_after_success(self):
        self.navigate("Crear pedido")
        self.app.number_input(key="ropa_cart_quantity").set_value(2)
        self.button("Agregar al pedido").click().run()
        self.healthy()
        self.db.create_order.side_effect = [RuntimeError("Conexión interrumpida"), SimpleNamespace(numero_pedido=11, total=100000, reused=True)]
        self.button("Confirmar y guardar pedido").click().run()
        self.assertTrue(self.app.error)
        self.assertEqual(self.app.session_state.ropa_cart, {1: 2})
        first_token = self.db.create_order.call_args.args[3]
        # Si el COMMIT ya ocurrió, puede haber agotado stock o cambiado el
        # cliente. La recuperación no depende de los catálogos actuales.
        self.db.products.return_value = self.db.products.return_value.iloc[0:0]
        self.db.clients.return_value = self.db.clients.return_value.iloc[0:0]
        self.db.products.reset_mock()
        self.db.clients.reset_mock()
        self.button("Confirmar y guardar pedido").click().run()
        self.assertEqual([item.message for item in self.app.exception], [])
        self.assertEqual([item.value for item in self.app.error], [])
        self.assertEqual(self.db.create_order.call_args.args[3], first_token)
        self.assertEqual(self.db.create_order.call_args.args[2], [{"id_producto": 1, "cantidad": 2}])
        self.assertEqual(self.app.session_state.ropa_cart, {})
        self.assertTrue(any("recuperado" in item.value for item in self.app.success))

    def test_multiple_products_and_repeated_line_are_aggregated(self):
        self.db.products.return_value.loc[1, "cantidad_inventario"] = 5
        self.navigate("Crear pedido")
        self.button("Agregar al pedido").click().run()
        self.button("Agregar al pedido").click().run()
        self.app.selectbox(key="ropa_cart_product").set_value(2)
        self.app.number_input(key="ropa_cart_quantity").set_value(3)
        self.button("Agregar al pedido").click().run()
        self.healthy()
        self.assertEqual(self.app.session_state.ropa_cart, {1: 2, 2: 3})
        self.button("Confirmar y guardar pedido").click().run()
        self.healthy()
        self.assertEqual(self.db.create_order.call_args.args[2], [{"id_producto": 1, "cantidad": 2}, {"id_producto": 2, "cantidad": 3}])

    def test_order_requires_active_clients(self):
        self.db.clients.return_value = self.db.clients.return_value.iloc[0:0]
        self.navigate("Crear pedido")
        self.assertTrue(any("cliente activo" in item.value for item in self.app.info))
        self.assertFalse(any(item.label == "Confirmar y guardar pedido" for item in self.app.button))

    def test_sql_error_discards_old_result(self):
        self.navigate("Laboratorio SQL")
        self.button("Ejecutar consulta").click().run()
        self.healthy()
        self.assertIn("ropa_sql_result", self.app.session_state)
        self.db.query.side_effect = ValueError("Solo se permite SELECT")
        self.app.text_area(key="ropa_sql_text").set_value("DELETE FROM PRODUCTO")
        self.button("Ejecutar consulta").click().run()
        self.assertNotIn("ropa_sql_result", self.app.session_state)
        self.assertIn("Solo se permite SELECT", [item.value for item in self.app.error])
        self.assertEqual(self.app.text_area(key="ropa_sql_text").value, "DELETE FROM PRODUCTO")

    def test_invalid_date_range_does_not_query(self):
        self.app.radio(key="ropa_report_period").set_value("Rango de fechas").run()
        self.db.report.reset_mock()
        self.app.date_input(key="ropa_report_start").set_value(date(2026, 11, 6)).run()
        self.healthy()
        self.assertTrue(self.app.warning)
        self.db.report.assert_not_called()

    def test_missing_credentials_does_not_query_or_fall_back(self):
        self.settings.ready = False
        self.db.reset_mock()
        self.app.run()
        self.healthy()
        self.assertTrue(self.app.warning)
        self.db.schema_status.assert_not_called()
        self.db.report.assert_not_called()

    def test_missing_schema_is_explained(self):
        self.db.schema_status.return_value = ["PRODUCTO"]
        self.db.report.reset_mock()
        self.app.run()
        self.healthy()
        self.assertTrue(self.app.warning)
        self.db.report.assert_not_called()


if __name__ == "__main__":
    unittest.main()
