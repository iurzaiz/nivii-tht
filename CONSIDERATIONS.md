# Documentación técnica

Decisiones de arquitectura, trade-offs y razonamiento detrás del SQL Query Assistant.

---

## Arquitectura general

El sistema se compone de tres contenedores Docker orquestados con Docker Compose:

```
Navegador (puerto 8000)
    └─▶ App FastAPI
          ├─▶ Ollama  (genera el SQL y la respuesta en lenguaje natural)
          └─▶ PostgreSQL  (datos cargados desde data.csv)
```

La app de FastAPI es el único servicio expuesto al exterior. Ollama y PostgreSQL son accesibles únicamente desde dentro de la red interna de Docker.

Consideré separar la generación de SQL en su propio servicio, pero me pareció innecesario para el problema que hay que resolver. Un solo proceso maneja todo el flujo de negocio, y los dos servicios que sí están separados (modelo y base de datos) lo están por razones concretas: tienen requisitos de recursos distintos y ciclos de vida independientes. Si en algún momento se quisiera reemplazar Ollama por vLLM o migrar de PostgreSQL a DuckDB, se puede hacer sin tocar nada del resto.

---

## El modelo: `qwen2.5-coder:7b`

Esta fue la decisión más importante del proyecto, así que tiene sentido explicarla con detalle.

El enunciado menciona SQLCoder-2-7b, T5-base y otras alternativas. Evalué varias antes de decidir:

**T5-base** quedó descartado rápidamente. Es liviano, pero la calidad del SQL que genera sobre preguntas del mundo real es insuficiente. Sin fine-tuning específico sobre el dataset, falla en consultas con agrupaciones o funciones de agregación compuestas. No justifica el ahorro de recursos.

**SQLCoder-7b** es una opción muy sólida para texto-a-SQL, ya que está fine-tuneado específicamente para esa tarea. El problema es que no está disponible en el registry de Ollama, por lo que habría que gestionar la descarga de los pesos manualmente desde Hugging Face. Eso complica considerablemente el setup para quien quiera correrlo en su propia máquina. La portabilidad era un requisito importante, así que preferí que `docker compose up` fuera condición suficiente.

**qwen2.5-coder:7b** está disponible en Ollama, genera SQL correcto para consultas sobre una sola tabla y, además, el mismo modelo puede encargarse tanto de la generación del SQL como de la respuesta en lenguaje natural. No fue necesario descargar dos modelos ni mantener dos servicios separados.

Por defecto, Ollama cuantiza el modelo a `q4_K_M`, lo que reduce el uso de memoria de ~14 GB (fp16) a ~4.5 GB. Con eso, el modelo corre en cualquier máquina con 8 GB de RAM, que me pareció un mínimo razonable para asumir del lado del evaluador.

### Limitaciones conocidas

En CPU, el modelo es lento. Una consulta puede tardar entre 30 segundos y varios minutos dependiendo del hardware. Hay dos alternativas para mitigarlo: usar `qwen2.5-coder:3b` (configurable con `MODEL_NAME=qwen2.5-coder:3b` en el `.env`) o correr Ollama de forma nativa en Mac con Apple Silicon, donde la aceleración por Metal reduce los tiempos considerablemente. Ambas opciones están documentadas en el README.

---

## Prompt engineering

Buena parte de la calidad del sistema depende de lo que se le pasa al modelo, no solo del modelo en sí.

### Valores de muestra en el schema

No alcanza con incluir nombres y tipos de columnas. El modelo necesita saber qué valores reales existen en los datos. Si no se le muestra que `week_day` contiene `'Friday'`, `'Monday'`, etc., puede escribir `WHERE week_day = 'friday'` (en minúscula) y no devolver ningún resultado.

Por eso, junto con el schema se inyectan tres valores reales de cada columna:

```
Columnas:
  - week_day (character varying): ej. Friday, Monday, Saturday
  - product_name (character varying): ej. Alfajor 70 cacao x un, Conito choc caja x12un
  ...
```

### Fecha actual en el prompt

Los modelos de lenguaje no saben qué día es hoy. Sin esta información, cuando el usuario pregunta "el mes pasado" o "esta semana", el modelo infiere una fecha a partir de su training data, que puede ser de hace años.

La solución es simple: inyectar la fecha actual al comienzo de cada prompt de generación de SQL:

```
Today's date: 2026-05-21 (Wednesday, May 21, 2026).
Use this to resolve relative time expressions like 'last month', 'this week', 'yesterday'.
```

Con eso, el modelo puede calcular correctamente los rangos de fechas para cualquier expresión relativa, sin necesidad de que el usuario especifique fechas explícitas.

### Few-shot examples

Se incluyen tres ejemplos fijos de pregunta → SQL al final del prompt. Sirven para que el modelo calibre el formato esperado (sin markdown, terminando con `;`) y entienda el nivel de complejidad de las consultas.

### Temperature 0 para la generación de SQL

El SQL tiene una respuesta correcta. No se necesita variación creativa. Con temperature 0 el modelo es determinista, lo que hace que los reintentos ante errores sean significativos: el modelo está corrigiendo algo concreto, no explorando una dirección distinta al azar.

Para la respuesta en lenguaje natural se usa temperature 0.3. Ahí sí tiene sentido que haya algo de variación en el fraseo.

---

## Loop de autocorrección

Los LLMs cometen errores. Asumir que el primer output va a ser siempre un SQL válido sería ingenuo. El sistema implementa dos capas de corrección:

**Capa 1 — validación previa a la ejecución.** `sqlparse` verifica que el output sea un SELECT. Si no lo es, o si está malformado, el error se incluye como contexto en el siguiente intento: `"El intento anterior produjo el siguiente SQL: {sql}. Error: {error}. Generá una versión corregida."` En la mayoría de los casos, un reintento es suficiente.

**Capa 2 — error de la base de datos.** Si el SQL supera la validación pero PostgreSQL lo rechaza (por ejemplo, cuando el modelo alucina el nombre de una columna), el mensaje de error se usa como contexto para generar un nuevo SQL. PostgreSQL devuelve mensajes precisos (`column "producto" does not exist`), lo que le da al modelo información suficiente para identificar y corregir el problema.

Como medida de seguridad, cualquier statement que no sea SELECT queda bloqueado antes de llegar a la base de datos. Independientemente de lo que genere el modelo, a PostgreSQL solo llegan consultas de lectura.

---

## Seguridad SQL

El sistema aplica dos capas de validación independientes antes de ejecutar cualquier query generado por el modelo.

**Capa 1 — blocklist de keywords.** Se aplica sobre el texto crudo del SQL, antes de cualquier parsing. Una expresión regular busca los verbos prohibidos:

```
INSERT · UPDATE · DELETE · DROP · ALTER · TRUNCATE
CREATE · REPLACE · MERGE · GRANT · REVOKE · EXECUTE
```

Si alguno aparece, el query se rechaza de inmediato con un error descriptivo. Esta capa es intencionalemente redundante: no depende de que el parser identifique correctamente el tipo de statement.

**Capa 2 — validación estructural con sqlparse.** Verifica que el statement sea exactamente un SELECT. Si `sqlparse` no puede determinar el tipo (lo que ocurre en inputs ambiguos o malformados), el query también se rechaza. Adicionalmente, se bloquean los inputs con múltiples statements separados por `;`, que son el vector más común de SQL injection en entornos de texto-a-SQL.

La razón de tener dos capas es que cada una cubre los puntos ciegos de la otra. La blocklist falla ante ofuscación creativa (comentarios, codificación de caracteres); el parser falla ante SQL que no puede tokenizar. Juntas, el margen de error es mínimo para el tipo de input que puede generar un LLM.

---

## Base de datos

Se eligió PostgreSQL en lugar de SQLite principalmente porque el enunciado especifica que cada servicio debe correr en su propio contenedor, y PostgreSQL encaja de forma natural en ese esquema. Adicionalmente, el uso de tipos fuertes (`DATE`, `TIME`, `NUMERIC`) reduce el margen de error en el SQL generado: el modelo tiene más información sobre el dominio de cada columna.

La carga del CSV ocurre una sola vez al iniciar la app, con una verificación previa por conteo de filas. Si la tabla ya contiene datos, el proceso se omite. El volumen de Docker persiste los datos entre reinicios, así que `docker compose down && docker compose up` no recarga todo desde cero.

Se crean índices automáticamente sobre `week_day`, `date` y `product_name`, que son las columnas que con mayor probabilidad van a aparecer en cláusulas WHERE y GROUP BY dado el tipo de consultas que tiene sentido hacerle a este dataset.

---

## Respuesta en streaming

La respuesta en lenguaje natural se transmite token por token usando Server-Sent Events. No es solo una cuestión estética: la inferencia del modelo es la parte más lenta del pipeline. Si se esperara el texto completo antes de mostrarlo, el usuario vería una pantalla en blanco por varios segundos. Con streaming, la tabla de resultados aparece de inmediato y la respuesta se va construyendo mientras el usuario ya está mirando los datos.

La implementación del lado del cliente usa `ReadableStream` nativo del navegador, sin dependencias adicionales.

---

## Decisiones de simplicidad

Varias cosas se dejaron afuera de forma deliberada:

**Caché semántico.** Útil en producción con tráfico elevado, pero innecesario para una tabla de 24k filas que ejecuta en milisegundos.

**Autenticación.** El sistema está pensado como herramienta de desarrollo local.

**Cola de mensajes.** El event loop async de FastAPI es suficiente para el volumen esperado.

**Framework de frontend.** HTML y JavaScript vanilla hacen exactamente lo que se necesita acá, sin build steps ni dependencias que dificulten el setup.

**Preguntas de clarificación.** Una variante posible sería que el modelo, antes de generar el SQL, evalúe si la pregunta es ambigua y haga una repregunta al usuario ("¿a qué período te referís con 'reciente'?"). Se descartó por tres motivos: agrega una inferencia extra que en CPU puede sumar 30-60 segundos solo para decidir si hay dudas; el dataset de una sola tabla con schema documentado y valores de muestra inyectados tiene poca ambigüedad genuina; y el loop de autocorrección ya cubre el caso real de falla con información más precisa que la que podría aportar el usuario. La feature tiene más sentido en productos con múltiples tablas y relaciones complejas, donde la ambigüedad estructural es real y el trade-off de latencia se justifica.

---

## Escalabilidad

### Si el dataset crece

Con múltiples tablas grandes, inyectar el schema completo en cada prompt deja de ser viable. El camino natural es RAG sobre el schema: embeber las descripciones de columnas y tablas, y recuperar solo las partes relevantes antes de construir el prompt. `pgvector` como extensión de PostgreSQL o Chroma como servicio separado son opciones concretas para implementarlo.

Para consultas analíticas sobre datos de gran volumen, vale la pena evaluar DuckDB o Redshift. PostgreSQL es adecuado para muchos escenarios, pero no está optimizado para scans columnares sobre tablas de cientos de millones de filas.

### Si el tráfico aumenta

La app es stateless (el schema está cacheado en memoria pero se puede externalizar a Redis). El escalado horizontal es directo:

```
           ┌──────────┐
           │  nginx   │
           └────┬─────┘
     ┌──────────┼──────────┐
  app:8001  app:8002  app:8003
     └──────────┼──────────┘
                │
        ┌───────┴────────┐
     Postgres          Ollama / vLLM
   (read replicas)    (multi-GPU)
```

El cuello de botella real va a ser la inferencia del modelo. Ollama funciona bien para un usuario a la vez, pero para tráfico concurrente conviene reemplazarlo por **vLLM**, que implementa batching continuo de requests y puede incrementar el throughput entre 10 y 50 veces dependiendo del hardware disponible.

Tiene sentido también cachear el SQL generado para preguntas similares. Un caché semántico con embeddings y búsqueda por similitud de coseno evita hacer inferencia cuando la consulta es equivalente a una reciente.

Del lado de la base de datos: réplicas de lectura para distribuir las queries SELECT, y PgBouncer para connection pooling si hay muchas instancias de la app conectándose en paralelo.
