-- Datos ficticios reproducibles: 20 clientes, 30 disenos, 150 variantes,
-- 60 pedidos y 150 lineas. Periodo: seis meses calendario, incluido el actual.
-- Requiere las cinco tablas vacias. No inserta fechas posteriores a hoy en Bogota.
-- Un unico bloque transaccional. El ejecutor confirma con COMMIT solo si termina
-- correctamente; en SQL Developer/SQLPlus, confirmar manualmente al finalizar.
DECLARE
    TYPE t_numeros IS TABLE OF NUMBER INDEX BY PLS_INTEGER;
    TYPE t_textos IS TABLE OF VARCHAR2(120);
    v_tallas t_textos := t_textos('XS', 'S', 'M', 'L', 'XL');
    v_nombres t_textos := t_textos(
        'Ana Torres', 'Luis Mendoza', 'Sofia Ramirez', 'Mateo Vargas',
        'Valentina Rojas', 'Nicolas Castro', 'Camila Herrera', 'Daniel Suarez',
        'Isabella Moreno', 'Santiago Leon', 'Mariana Gil', 'Sebastian Ruiz',
        'Gabriela Ortiz', 'Alejandro Vega', 'Juliana Salazar', 'Andres Molina',
        'Lucia Restrepo', 'Felipe Acosta', 'Paula Cardenas', 'David Mejia'
    );
    v_clientes t_numeros;
    v_productos t_numeros;
    v_demanda t_numeros;
    v_hoy DATE := TRUNC(CAST(SYSTIMESTAMP AT TIME ZONE 'America/Bogota' AS DATE));
    v_inicio DATE := ADD_MONTHS(TRUNC(v_hoy, 'MM'), -5);
    v_fecha DATE;
    v_mes DATE;
    v_fin_mes DATE;
    v_id_fecha NUMBER;
    v_id_cliente NUMBER;
    v_numero_pedido NUMBER;
    v_precio NUMBER;
    v_costo NUMBER;
    v_stock NUMBER;
    v_cantidad PLS_INTEGER;
    v_indice PLS_INTEGER;
    v_variantes PLS_INTEGER := 0;
    v_existentes NUMBER;
    v_stock_inicial NUMBER := 0;
    v_stock_final NUMBER;
    v_unidades NUMBER;
    v_conteo NUMBER;

    FUNCTION clave_fecha(p_fecha DATE) RETURN NUMBER IS
    BEGIN
        RETURN EXTRACT(YEAR FROM p_fecha) * 10000
             + EXTRACT(MONTH FROM p_fecha) * 100 + EXTRACT(DAY FROM p_fecha);
    END;

    FUNCTION indice_producto(p_pedido PLS_INTEGER, p_linea PLS_INTEGER)
        RETURN PLS_INTEGER IS
    BEGIN
        RETURN MOD((p_pedido - 1) * 11 + (p_linea - 1) * 37, 150) + 1;
    END;

    PROCEDURE agregar_diseno(
        p_nombre VARCHAR2, p_categoria VARCHAR2, p_color VARCHAR2,
        p_costo NUMBER, p_precio NUMBER
    ) IS
        v_inventario NUMBER;
    BEGIN
        FOR i IN 1 .. v_tallas.COUNT LOOP
            v_variantes := v_variantes + 1;
            -- Definimos existencias finales y sumamos las ventas previstas para
            -- obtener la entrada inicial. Cada venta descuenta sus unidades.
            v_inventario := CASE
                WHEN MOD(v_variantes, 17) = 0 THEN 0
                WHEN MOD(v_variantes, 11) = 0 THEN 2
                ELSE 8 + MOD(v_variantes, 13)
            END + v_demanda(v_variantes);
            INSERT INTO DIM_PRODUCTOS (
                nombre_producto, categoria, talla, color,
                costo_actual, precio_actual, cantidad_inventario
            ) VALUES (
                p_nombre, p_categoria, v_tallas(i), p_color,
                p_costo, p_precio, v_inventario
            ) RETURNING id_producto INTO v_productos(v_variantes);
            v_stock_inicial := v_stock_inicial + v_inventario;
        END LOOP;
    END;
BEGIN
    SAVEPOINT antes_semilla;
    -- Evita que otra sesion escriba entre la comprobacion de vacio y la carga.
    LOCK TABLE DIM_CLIENTES IN EXCLUSIVE MODE NOWAIT;
    LOCK TABLE DIM_PRODUCTOS IN EXCLUSIVE MODE NOWAIT;
    LOCK TABLE DIM_FECHAS IN EXCLUSIVE MODE NOWAIT;
    LOCK TABLE PEDIDOS IN EXCLUSIVE MODE NOWAIT;
    LOCK TABLE HECHOS_PEDIDOS IN EXCLUSIVE MODE NOWAIT;

    SELECT (SELECT COUNT(*) FROM DIM_CLIENTES)
         + (SELECT COUNT(*) FROM DIM_PRODUCTOS)
         + (SELECT COUNT(*) FROM DIM_FECHAS)
         + (SELECT COUNT(*) FROM PEDIDOS)
         + (SELECT COUNT(*) FROM HECHOS_PEDIDOS)
    INTO v_existentes FROM DUAL;
    IF v_existentes <> 0 THEN
        RAISE_APPLICATION_ERROR(-20001, 'La semilla requiere las cinco tablas vacias.');
    END IF;

    FOR i IN 1 .. 150 LOOP
        v_demanda(i) := 0;
    END LOOP;
    FOR pedido IN 1 .. 60 LOOP
        FOR linea IN 1 .. (1 + MOD(pedido - 1, 4)) LOOP
            v_indice := indice_producto(pedido, linea);
            v_demanda(v_indice) := v_demanda(v_indice) + 1 + MOD(pedido + linea, 3);
        END LOOP;
    END LOOP;

    FOR i IN 1 .. v_nombres.COUNT LOOP
        INSERT INTO DIM_CLIENTES (
            nombre, correo, telefono, direccion, fecha_registro, estado
        ) VALUES (
            v_nombres(i), 'cliente' || TO_CHAR(i, 'FM00') || '@example.com',
            '300000' || TO_CHAR(i, 'FM0000'),
            'Direccion ficticia ' || TO_CHAR(i) || ', Bogota',
            v_inicio - MOD(i, 15), 'ACTIVO'
        ) RETURNING id_cliente INTO v_clientes(i);
    END LOOP;

    agregar_diseno('Jean recto azul', 'Pantalon', 'Azul', 65000, 129900);
    agregar_diseno('Cargo urbano negro', 'Pantalon', 'Negro', 72000, 149900);
    agregar_diseno('Chino beige', 'Pantalon', 'Beige', 58000, 119900);
    agregar_diseno('Jogger gris', 'Pantalon', 'Gris', 48000, 99900);
    agregar_diseno('Palazzo verde oliva', 'Pantalon', 'Verde oliva', 62000, 139900);
    agregar_diseno('Camisa oxford blanca', 'camisa', 'Blanco', 45000, 99900);
    agregar_diseno('Camisa lino beige', 'camisa', 'Beige', 55000, 119900);
    agregar_diseno('Camisa cuadros azul', 'camisa', 'Azul', 42000, 89900);
    agregar_diseno('Camisa denim celeste', 'camisa', 'Celeste', 58000, 129900);
    agregar_diseno('Camisa manga corta negra', 'camisa', 'Negro', 38000, 84900);
    agregar_diseno('Camiseta basic blanca', 'camiseta', 'Blanco', 22000, 49900);
    agregar_diseno('Camiseta basic negra', 'camiseta', 'Negro', 22000, 49900);
    agregar_diseno('Camiseta oversize gris', 'camiseta', 'Gris', 28000, 64900);
    agregar_diseno('Camiseta rayas azul', 'camiseta', 'Azul', 26000, 59900);
    agregar_diseno('Camiseta estampada terracota', 'camiseta', 'Terracota', 30000, 69900);
    agregar_diseno('Short negro', 'pantaloneta', 'Negro', 32000, 74900);
    agregar_diseno('Pantaloneta deportiva azul', 'pantaloneta', 'Azul', 28000, 64900);
    agregar_diseno('Pantaloneta cargo beige', 'pantaloneta', 'Beige', 38000, 84900);
    agregar_diseno('Short denim celeste', 'pantaloneta', 'Celeste', 40000, 89900);
    agregar_diseno('Pantaloneta lino blanca', 'pantaloneta', 'Blanco', 35000, 79900);
    agregar_diseno('Cinturon clasico negro', 'accesorio', 'Negro', 18000, 44900);
    agregar_diseno('Cinturon clasico cafe', 'accesorio', 'Cafe', 18000, 44900);
    agregar_diseno('Cinturon trenzado beige', 'accesorio', 'Beige', 20000, 49900);
    agregar_diseno('Cinturon deportivo gris', 'accesorio', 'Gris', 15000, 39900);
    agregar_diseno('Cinturon textil azul', 'accesorio', 'Azul', 16000, 42900);
    agregar_diseno('Top rib blanco', 'top', 'Blanco', 18000, 44900);
    agregar_diseno('Top deportivo negro', 'top', 'Negro', 25000, 59900);
    agregar_diseno('Top tejido beige', 'top', 'Beige', 32000, 74900);
    agregar_diseno('Top manga larga vino', 'top', 'Vino', 28000, 64900);
    agregar_diseno('Top satinado verde', 'top', 'Verde', 35000, 79900);

    FOR dia IN 0 .. TRUNC(v_hoy - v_inicio) LOOP
        v_fecha := v_inicio + dia;
        v_id_fecha := clave_fecha(v_fecha);
        INSERT INTO DIM_FECHAS (id_fecha, fecha)
        VALUES (v_id_fecha, v_fecha);
    END LOOP;

    FOR pedido IN 1 .. 60 LOOP
        v_mes := ADD_MONTHS(v_inicio, TRUNC((pedido - 1) / 10));
        v_fin_mes := LEAST(LAST_DAY(v_mes), v_hoy);
        v_fecha := v_mes + TRUNC(MOD(pedido - 1, 10) * (v_fin_mes - v_mes) / 9);
        v_id_fecha := clave_fecha(v_fecha);
        v_id_cliente := v_clientes(MOD((pedido - 1) * 7, 20) + 1);
        INSERT INTO PEDIDOS (id_cliente, id_fecha, token_solicitud)
        VALUES (
            v_id_cliente, v_id_fecha,
            '00000000-0000-4000-8000-' || TO_CHAR(pedido, 'FM000000000000')
        ) RETURNING numero_pedido INTO v_numero_pedido;

        FOR linea IN 1 .. (1 + MOD(pedido - 1, 4)) LOOP
            v_indice := indice_producto(pedido, linea);
            v_cantidad := 1 + MOD(pedido + linea, 3);
            SELECT precio_actual, costo_actual, cantidad_inventario
            INTO v_precio, v_costo, v_stock
            FROM DIM_PRODUCTOS
            WHERE id_producto = v_productos(v_indice)
            FOR UPDATE;
            IF v_stock < v_cantidad THEN
                RAISE_APPLICATION_ERROR(-20002, 'Inventario insuficiente durante la semilla.');
            END IF;

            INSERT INTO HECHOS_PEDIDOS (
                numero_pedido, id_cliente, id_producto, id_fecha,
                cantidad, precio_unitario_venta, costo_unitario_venta
            ) VALUES (
                v_numero_pedido, v_id_cliente, v_productos(v_indice), v_id_fecha,
                v_cantidad, v_precio, v_costo
            );
            UPDATE DIM_PRODUCTOS
            SET cantidad_inventario = cantidad_inventario - v_cantidad
            WHERE id_producto = v_productos(v_indice);
        END LOOP;
    END LOOP;

    SELECT SUM(cantidad_inventario) INTO v_stock_final FROM DIM_PRODUCTOS;
    SELECT SUM(cantidad), COUNT(*) INTO v_unidades, v_conteo FROM HECHOS_PEDIDOS;
    IF v_variantes <> 150 OR v_conteo <> 150 OR v_unidades <> 300
       OR v_stock_final + v_unidades <> v_stock_inicial THEN
        RAISE_APPLICATION_ERROR(-20003, 'La semilla no supero su verificacion de consistencia.');
    END IF;
EXCEPTION
    WHEN OTHERS THEN
        ROLLBACK TO antes_semilla;
        RAISE;
END;
/
