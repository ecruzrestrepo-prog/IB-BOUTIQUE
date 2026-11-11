-- Solo consultas: no modifica datos. Conteos esperados inmediatamente tras la semilla.
SELECT 'DIM_CLIENTES' AS tabla, COUNT(*) AS registros, 20 AS esperado FROM DIM_CLIENTES
UNION ALL
SELECT 'DIM_PRODUCTOS', COUNT(*), 150 FROM DIM_PRODUCTOS
UNION ALL
SELECT 'PEDIDOS', COUNT(*), 60 FROM PEDIDOS
UNION ALL
SELECT 'HECHOS_PEDIDOS', COUNT(*), 150 FROM HECHOS_PEDIDOS;

-- 30 disenos; cada uno tiene exactamente las cinco tallas permitidas.
SELECT nombre_producto, categoria, color, COUNT(*) AS variantes,
       COUNT(DISTINCT talla) AS tallas
FROM DIM_PRODUCTOS
GROUP BY nombre_producto, categoria, color
ORDER BY categoria, nombre_producto;

-- Debe devolver cero filas: variantes incompletas o repetidas.
SELECT nombre_producto, categoria, color
FROM DIM_PRODUCTOS
GROUP BY nombre_producto, categoria, color
HAVING COUNT(*) <> 5 OR COUNT(DISTINCT talla) <> 5;

-- Seis categorias, 25 variantes por categoria.
SELECT categoria, COUNT(*) AS variantes, SUM(cantidad_inventario) AS existencias
FROM DIM_PRODUCTOS
GROUP BY categoria
ORDER BY categoria;

-- Debe devolver cero filas: un pedido no puede quedar sin lineas.
SELECT p.numero_pedido
FROM PEDIDOS p
LEFT JOIN HECHOS_PEDIDOS h ON h.numero_pedido = p.numero_pedido
GROUP BY p.numero_pedido
HAVING COUNT(h.id_detalle) = 0;

-- Debe devolver cero filas: coherencia de cabecera, importes e inventario.
SELECT h.id_detalle
FROM HECHOS_PEDIDOS h
JOIN PEDIDOS p ON p.numero_pedido = h.numero_pedido
JOIN DIM_PRODUCTOS d ON d.id_producto = h.id_producto
WHERE h.id_cliente <> p.id_cliente OR h.id_fecha <> p.id_fecha
   OR h.total_linea <> h.cantidad * h.precio_unitario_venta
   OR h.cantidad <= 0 OR h.cantidad <> TRUNC(h.cantidad)
   OR d.cantidad_inventario < 0;

-- Seis meses, diez pedidos por mes, sin ventas futuras.
SELECT f.anio, f.mes, COUNT(DISTINCT h.numero_pedido) AS pedidos,
       SUM(h.cantidad) AS unidades,
       SUM(h.total_linea) AS facturacion,
       SUM(h.cantidad * h.costo_unitario_venta) AS costo_ventas,
       SUM(h.total_linea - h.cantidad * h.costo_unitario_venta) AS utilidad_bruta
FROM HECHOS_PEDIDOS h
JOIN DIM_FECHAS f ON f.id_fecha = h.id_fecha
GROUP BY f.anio, f.mes
ORDER BY f.anio, f.mes;

SELECT COUNT(*) AS pedidos_futuros
FROM PEDIDOS p
JOIN DIM_FECHAS f ON f.id_fecha = p.id_fecha
WHERE f.fecha > TRUNC(CAST(SYSTIMESTAMP AT TIME ZONE 'America/Bogota' AS DATE));

-- Ventas totales: 60 pedidos, 150 lineas y 300 unidades.
SELECT COUNT(DISTINCT numero_pedido) AS pedidos, COUNT(*) AS lineas,
       SUM(cantidad) AS unidades, SUM(total_linea) AS facturacion,
       SUM(cantidad * costo_unitario_venta) AS costo_ventas,
       SUM(total_linea - cantidad * costo_unitario_venta) AS utilidad_bruta
FROM HECHOS_PEDIDOS;

-- Constraints presentes, habilitadas y validadas.
SELECT table_name, constraint_name, constraint_type, status, validated
FROM USER_CONSTRAINTS
WHERE table_name IN ('DIM_CLIENTES', 'DIM_PRODUCTOS', 'DIM_FECHAS', 'PEDIDOS', 'HECHOS_PEDIDOS')
ORDER BY table_name, constraint_type, constraint_name;

SELECT table_name, index_name, uniqueness, status
FROM USER_INDEXES
WHERE table_name IN ('DIM_CLIENTES', 'DIM_PRODUCTOS', 'DIM_FECHAS', 'PEDIDOS', 'HECHOS_PEDIDOS')
ORDER BY table_name, index_name;
