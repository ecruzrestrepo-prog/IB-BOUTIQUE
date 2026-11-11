-- Modelo de la tienda: ejecutar una vez, despues de limpiar el esquema previsto.
-- Oracle confirma implicitamente el DDL. Este archivo no borra objetos existentes.
-- Las columnas virtuales se consultan, pero se omiten en INSERT y UPDATE.

CREATE TABLE DIM_CLIENTES (
    id_cliente NUMBER GENERATED ALWAYS AS IDENTITY,
    nombre VARCHAR2(120 CHAR) NOT NULL,
    correo VARCHAR2(254 CHAR) NOT NULL,
    telefono VARCHAR2(30 CHAR) NOT NULL,
    direccion VARCHAR2(250 CHAR) NOT NULL,
    fecha_registro DATE DEFAULT SYSDATE NOT NULL,
    estado VARCHAR2(8 CHAR) DEFAULT 'ACTIVO' NOT NULL,
    CONSTRAINT pk_dim_clientes PRIMARY KEY (id_cliente),
    CONSTRAINT uq_clientes_correo UNIQUE (correo),
    CONSTRAINT ck_clientes_nombre CHECK (TRIM(nombre) IS NOT NULL),
    CONSTRAINT ck_clientes_correo CHECK (
        TRIM(correo) IS NOT NULL AND correo = LOWER(TRIM(correo))
    ),
    CONSTRAINT ck_clientes_telefono CHECK (TRIM(telefono) IS NOT NULL),
    CONSTRAINT ck_clientes_direccion CHECK (TRIM(direccion) IS NOT NULL),
    CONSTRAINT ck_clientes_estado CHECK (estado IN ('ACTIVO', 'INACTIVO'))
);

CREATE TABLE DIM_PRODUCTOS (
    id_producto NUMBER GENERATED ALWAYS AS IDENTITY,
    nombre_producto VARCHAR2(120 CHAR) NOT NULL,
    categoria VARCHAR2(20 CHAR) NOT NULL,
    talla VARCHAR2(2 CHAR) NOT NULL,
    color VARCHAR2(40 CHAR) NOT NULL,
    costo_actual NUMBER(12,2) NOT NULL,
    precio_actual NUMBER(12,2) NOT NULL,
    cantidad_inventario NUMBER DEFAULT 0 NOT NULL,
    CONSTRAINT pk_dim_productos PRIMARY KEY (id_producto),
    CONSTRAINT ck_productos_nombre CHECK (TRIM(nombre_producto) IS NOT NULL),
    CONSTRAINT ck_productos_categoria CHECK (
        categoria IN ('Pantalon', 'camisa', 'camiseta', 'pantaloneta', 'accesorio', 'top')
    ),
    CONSTRAINT ck_productos_talla CHECK (talla IN ('XS', 'S', 'M', 'L', 'XL')),
    CONSTRAINT ck_productos_color CHECK (TRIM(color) IS NOT NULL),
    CONSTRAINT ck_productos_costo CHECK (costo_actual >= 0),
    CONSTRAINT ck_productos_precio CHECK (precio_actual >= 0),
    -- NUMBER sin escala conserva fracciones para rechazarlas, sin redondearlas.
    CONSTRAINT ck_productos_stock CHECK (
        cantidad_inventario BETWEEN 0 AND 999999999
        AND cantidad_inventario = TRUNC(cantidad_inventario)
    )
);

-- Una variante no puede repetirse cambiando mayusculas o espacios externos.
CREATE UNIQUE INDEX uq_productos_variante ON DIM_PRODUCTOS (
    LOWER(TRIM(nombre_producto)), categoria, talla, LOWER(TRIM(color))
);

CREATE TABLE DIM_FECHAS (
    id_fecha NUMBER NOT NULL,
    fecha DATE NOT NULL,
    dia GENERATED ALWAYS AS (EXTRACT(DAY FROM fecha)) VIRTUAL NOT NULL,
    mes GENERATED ALWAYS AS (EXTRACT(MONTH FROM fecha)) VIRTUAL NOT NULL,
    anio GENERATED ALWAYS AS (EXTRACT(YEAR FROM fecha)) VIRTUAL NOT NULL,
    CONSTRAINT pk_dim_fechas PRIMARY KEY (id_fecha),
    CONSTRAINT uq_fechas_fecha UNIQUE (fecha),
    CONSTRAINT ck_fechas_sin_hora CHECK (fecha = TRUNC(fecha)),
    CONSTRAINT ck_fechas_id CHECK (
        id_fecha = EXTRACT(YEAR FROM fecha) * 10000
                 + EXTRACT(MONTH FROM fecha) * 100
                 + EXTRACT(DAY FROM fecha)
    )
);

CREATE TABLE PEDIDOS (
    numero_pedido NUMBER GENERATED ALWAYS AS IDENTITY,
    id_cliente NUMBER NOT NULL,
    id_fecha NUMBER NOT NULL,
    token_solicitud VARCHAR2(36 CHAR) NOT NULL,
    CONSTRAINT pk_pedidos PRIMARY KEY (numero_pedido),
    CONSTRAINT uq_pedidos_token UNIQUE (token_solicitud),
    CONSTRAINT uq_pedidos_contexto UNIQUE (numero_pedido, id_cliente, id_fecha),
    CONSTRAINT fk_pedidos_cliente FOREIGN KEY (id_cliente)
        REFERENCES DIM_CLIENTES (id_cliente),
    CONSTRAINT fk_pedidos_fecha FOREIGN KEY (id_fecha)
        REFERENCES DIM_FECHAS (id_fecha),
    CONSTRAINT ck_pedidos_token CHECK (TRIM(token_solicitud) IS NOT NULL)
);

CREATE TABLE HECHOS_PEDIDOS (
    id_detalle NUMBER GENERATED ALWAYS AS IDENTITY,
    numero_pedido NUMBER NOT NULL,
    id_cliente NUMBER NOT NULL,
    id_producto NUMBER NOT NULL,
    id_fecha NUMBER NOT NULL,
    cantidad NUMBER NOT NULL,
    precio_unitario_venta NUMBER(12,2) NOT NULL,
    costo_unitario_venta NUMBER(12,2) NOT NULL,
    total_linea GENERATED ALWAYS AS (cantidad * precio_unitario_venta) VIRTUAL NOT NULL,
    CONSTRAINT pk_hechos_pedidos PRIMARY KEY (id_detalle),
    CONSTRAINT uq_hechos_pedido_producto UNIQUE (numero_pedido, id_producto),
    CONSTRAINT fk_hechos_cliente FOREIGN KEY (id_cliente)
        REFERENCES DIM_CLIENTES (id_cliente),
    CONSTRAINT fk_hechos_producto FOREIGN KEY (id_producto)
        REFERENCES DIM_PRODUCTOS (id_producto),
    CONSTRAINT fk_hechos_fecha FOREIGN KEY (id_fecha)
        REFERENCES DIM_FECHAS (id_fecha),
    -- Preserva la estrella y garantiza un solo cliente/fecha por pedido.
    CONSTRAINT fk_hechos_pedido_contexto
        FOREIGN KEY (numero_pedido, id_cliente, id_fecha)
        REFERENCES PEDIDOS (numero_pedido, id_cliente, id_fecha),
    CONSTRAINT ck_hechos_cantidad CHECK (
        cantidad BETWEEN 1 AND 999999999 AND cantidad = TRUNC(cantidad)
    ),
    CONSTRAINT ck_hechos_precio CHECK (precio_unitario_venta >= 0),
    CONSTRAINT ck_hechos_costo CHECK (costo_unitario_venta >= 0)
);

CREATE INDEX ix_pedidos_cliente_fecha ON PEDIDOS (id_cliente, id_fecha);
CREATE INDEX ix_pedidos_fecha ON PEDIDOS (id_fecha);
CREATE INDEX ix_hechos_cliente ON HECHOS_PEDIDOS (id_cliente);
CREATE INDEX ix_hechos_producto ON HECHOS_PEDIDOS (id_producto);
CREATE INDEX ix_hechos_fecha ON HECHOS_PEDIDOS (id_fecha);
CREATE INDEX ix_hechos_pedido_contexto ON HECHOS_PEDIDOS (numero_pedido, id_cliente, id_fecha);
