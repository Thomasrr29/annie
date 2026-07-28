# Agente de Soporte Inteligente — Alcance y Requerimientos del MVP

**Versión:** 1.0 (borrador para validación)
**Producto:** Plataforma SaaS multi-tenant de agentes conversacionales para atención al cliente de pymes.
**Canal del MVP:** WhatsApp (arquitectura preparada para multicanal, pero solo se despliega WhatsApp).

---

## 1. Objetivo del MVP

Entregar un agente conversacional que una pyme pueda configurar sin escribir código, capaz de **responder preguntas** a partir del conocimiento del negocio y de **capturar solicitudes/acciones** para el dueño, atendiendo por WhatsApp con memoria de conversación y escalamiento a humano cuando sea necesario.

El éxito del MVP se mide por **tiempo ahorrado al negocio** y **porcentaje de conversaciones resueltas sin intervención humana**, manteniendo un **costo por conversación bajo y predecible**.

### 1.1 Principios de diseño (no negociables)

1. **Un motor, N configuraciones.** El código es único; cada negocio es un registro de configuración. Onboarding = llenar un formulario, no desplegar código.
2. **Enrutamiento por datos, no por prosa.** El orquestador es genérico y agnóstico al tenant; carga en tiempo de ejecución la lista de agentes del negocio y enruta según sus descripciones. Agregar un agente = insertar un registro, nunca editar el orquestador.
3. **Híbrido.** Reglas deterministas donde equivocarse es caro; generación con LLM donde se necesita flexibilidad. La línea entre ambos es un campo configurable por agente.
4. **La seguridad la aplica el código, no las instrucciones.** El acceso a datos se restringe por la capa de herramientas y permisos, no por lo que diga el `.md` del agente.
5. **Costo controlado desde el día uno.** Prompt caching, escalonamiento de modelos y una llamada al modelo por turno siempre que sea posible.

---

## 2. Actores / Personas

| ID | Actor | Descripción |
|----|-------|-------------|
| A1 | **Administrador del agente** | Dueño u operador de la pyme. Configura el negocio, carga conocimiento, crea/edita agentes, define horarios, revisa la bandeja de escalamiento y las métricas. |
| A2 | **Usuario final (cliente)** | Persona que escribe al negocio por WhatsApp para consultar o solicitar algo. No sabe (ni necesita saber) que habla con un agente configurable. |
| A3 | **Operador humano del negocio** | Persona del negocio que recibe los escalamientos y las solicitudes capturadas. En pymes suele ser el mismo A1, pero se modela aparte. |
| A4 | **Súper-administrador (equipo del producto)** | Nuestro equipo. Da de alta tenants, gestiona límites y, en la modalidad concierge, configura agentes/herramientas avanzadas por el cliente. |

---

## 3. Arquitectura de referencia (resumen)

```
Canal (WhatsApp)
  → Adaptador de canal (normaliza al formato común de mensaje)
  → Orquestador (carga contexto, clasifica intención, enruta)
  → Agente/handler (responde con LLM y/o llama herramientas)
  → Composición de respuesta
  → Persistencia (memoria + log)
  → (se repite en el siguiente mensaje)
```

- **Runtime:** orquestador propio (loop delgado sobre la API de mensajes de Anthropic). No se adopta Claude Agent SDK ni LangGraph en el MVP; se reservan para flujos futuros con estado/larga duración.
- **Modelos:** Haiku para clasificación/enrutamiento; Sonnet para composición de respuesta y razonamiento sobre conocimiento.
- **Almacenamiento:** un único PostgreSQL con extensión pgvector cubre los cuatro almacenes lógicos. Redis se difiere hasta que la latencia lo justifique.

### 3.1 Los cuatro almacenes lógicos

| Almacén | Contenido | Tipo de acceso |
|---------|-----------|----------------|
| Base de conocimiento (RAG) | Catálogo, FAQs, políticas, horarios del negocio | Búsqueda semántica (vector) |
| Historial de conversación | Turnos recientes por (negocio, cliente) | Lookup por clave |
| Memoria de cliente | Perfil resumido: nombre, solicitudes previas, preferencias | Lookup por clave (registro estructurado, **no** vector) |
| Log de solicitudes/respuestas | Registro de cada turno para auditoría y métricas | Escritura/analítica |

> **Regla crítica:** el historial de conversación **no** alimenta el índice RAG. Mezclar logs crudos en el vector produce fuga de datos entre clientes/tenants y "hechos" obsoletos.

---

## 4. Flujos conversacionales

El clasificador reconoce un conjunto mínimo de intenciones. Toda conversación se resuelve como combinación de estos flujos.

### F1 — Consulta de información (INFORM)
El cliente quiere saber algo (precio, horario, disponibilidad, política, "¿cómo hago…?").
1. Se carga contexto de la conversación.
2. El agente ejecuta `search_knowledge` sobre la base del negocio.
3. Compone respuesta citando solo lo recuperado; si no hay información suficiente, cae a F7.

### F2 — Ejecución/captura de acción (ACT)
El cliente quiere **hacer** algo (agendar, registrar un pedido, dejar una solicitud). En el MVP no hay pagos ni integraciones externas: "acción" significa **capturar una solicitud estructurada y notificar al negocio**.
1. El agente identifica los datos necesarios y los solicita (uno a la vez).
2. Al completarlos, ejecuta `capture_request` (guarda la solicitud estructurada) y `notify_owner`.
3. Confirma al cliente y registra el resultado.

### F3 — Saludo / conversación social (GREET)
"Hola", "gracias", "buenos días". Respuesta ligera, sin herramientas.

### F4 — Aclaración (CLARIFY)
El mensaje es ambiguo o contiene dos intenciones. El agente hace **una** pregunta, espera respuesta y vuelve a clasificar. Es un sub-loop hacia el clasificador.

### F5 — Continuación dentro de un flujo (CONTINUE)
El cliente responde a algo que el agente preguntó. No se re-clasifica desde cero: se continúa el flujo activo con el nuevo dato.

### F6 — Escalamiento a humano (ESCALATE)
Queja, molestia, solicitud explícita de hablar con una persona, o falla repetida del agente.
1. Se marca la conversación en "modo humano" (el agente deja de responder).
2. Se notifica al operador (`notify_owner`) y se envía la conversación a la bandeja de escalamiento.
3. Se informa al cliente que una persona lo atenderá.

### F7 — Fallback / fuera de alcance
El agente no puede ayudar y no debe inventar. Responde con honestidad, indica qué **sí** puede hacer y ofrece escalar (F6).

---

## 5. Funcionalidades del MVP

### 5.1 Runtime del agente (cara al usuario final)
- Recepción de mensajes de WhatsApp vía adaptador de canal.
- Carga de contexto (turnos recientes + memoria de cliente).
- Clasificación de intención y enrutamiento al agente/handler correcto del tenant.
- Ejecución de herramientas integradas: `search_knowledge`, `capture_request`, `notify_owner`.
- Composición de respuesta en el idioma del cliente (español por defecto).
- Persistencia de historial, memoria y log en cada turno.
- Escalamiento a humano y "modo humano".
- Ventana de servicio de 24h de WhatsApp aprovechada para respuestas gratuitas (sin plantillas salientes en el MVP).

### 5.2 Administración (cara al administrador)
- Alta y configuración del negocio (nombre, tono/persona, horarios, idioma).
- Carga y actualización de la base de conocimiento (documentos/FAQs) con reindexado.
- Creación, edición y activación/desactivación de agentes (cuerpo `.md` + metadatos: intención que atiende, herramientas permitidas, alcance de memoria, estilo de respuesta).
- Bandeja de escalamiento (conversaciones en modo humano) y solicitudes capturadas.
- Panel de métricas básicas (volumen, % resuelto sin humano, escalamientos, tiempo de respuesta).
- Prueba en vivo del agente antes de publicarlo ("sandbox" de chat).

---

## 6. Requerimientos funcionales (RF)

### Canal
- **RF-01** El sistema debe recibir mensajes entrantes de WhatsApp y responder el webhook rápidamente (ACK inmediato), procesando la respuesta del agente de forma asíncrona.
- **RF-02** El sistema debe normalizar todo mensaje entrante a un formato común independiente del canal (preparado para futuros canales, pero solo WhatsApp activo).
- **RF-03** El sistema debe identificar al cliente por la clave (negocio, número de teléfono).

### Orquestador y agentes
- **RF-04** El orquestador debe cargar, en cada turno, el contexto de conversación y la memoria de cliente.
- **RF-05** El orquestador debe clasificar la intención del mensaje entre: INFORM, ACT, GREET, CLARIFY, CONTINUE, ESCALATE, FALLBACK.
- **RF-06** El orquestador debe cargar la lista de agentes activos del tenant y enrutar según sus descripciones, sin requerir cambios de código al agregar/quitar agentes.
- **RF-07** Cada agente debe ejecutarse con únicamente las herramientas que su configuración le permite (allowlist).
- **RF-08** El sistema debe soportar el flujo CLARIFY (una pregunta, re-clasificación) y CONTINUE (continuar el flujo activo sin re-clasificar).

### Herramientas integradas
- **RF-09** `search_knowledge`: búsqueda semántica sobre la base de conocimiento del tenant solicitante, restringida a ese tenant.
- **RF-10** `capture_request`: registro de una solicitud estructurada asociada al negocio y al cliente.
- **RF-11** `notify_owner`: notificación al operador del negocio (por el medio configurado) ante una solicitud capturada o un escalamiento.

### Escalamiento humano
- **RF-12** El sistema debe permitir marcar una conversación en "modo humano", tras lo cual el agente no responde automáticamente.
- **RF-13** El sistema debe enviar a la bandeja de escalamiento toda conversación en modo humano, con su historial.
- **RF-14** El sistema debe permitir al operador retomar la conversación y (opcionalmente) devolverla al modo automático.

### Persistencia
- **RF-15** El sistema debe persistir cada turno (entrada y salida) en el log.
- **RF-16** El sistema debe actualizar el historial de conversación reciente por (negocio, cliente).
- **RF-17** El sistema debe mantener un registro estructurado de memoria de cliente, actualizable de forma deliberada (no volcado automático de logs).

### Administración
- **RF-18** El administrador debe poder crear/editar el perfil del negocio (nombre, tono, horarios, idioma).
- **RF-19** El administrador debe poder cargar y actualizar la base de conocimiento; el sistema debe reindexarla.
- **RF-20** El administrador debe poder crear/editar/activar/desactivar agentes con su cuerpo de instrucciones y metadatos.
- **RF-21** El administrador debe poder ver la bandeja de escalamiento y las solicitudes capturadas.
- **RF-22** El administrador debe poder ver métricas básicas del agente.
- **RF-23** El administrador debe poder probar el agente en un chat de prueba antes de publicarlo.

### Multi-tenant
- **RF-24** Todo dato (conocimiento, historial, memoria, solicitudes) debe estar asociado a un tenant y ser accesible **solo** dentro de ese tenant.

---

## 7. Requerimientos no funcionales (RNF)

- **RNF-01 (Rendimiento):** el webhook de WhatsApp debe responder ACK en < 2 s; la respuesta del agente al cliente debe entregarse típicamente en pocos segundos.
- **RNF-02 (Costo):** costo por conversación bajo y predecible. Obligatorio: prompt caching sobre el prefijo estable (system prompt, definiciones de herramientas, contexto del negocio); escalonamiento de modelos (Haiku para enrutamiento, Sonnet para composición); minimizar llamadas al modelo por turno.
- **RNF-03 (Escalabilidad):** el orquestador debe ser sin estado (stateless); toda la configuración y el estado viven en la base de datos. La configuración es dato, no archivos en disco.
- **RNF-04 (Aislamiento / Seguridad):** aislamiento estricto entre tenants a nivel de datos y de herramientas. Ninguna entrada del cliente debe poder inducir acceso a datos de otro tenant. Las herramientas construyen sus consultas; el LLM no arma consultas crudas a la base de datos.
- **RNF-05 (Privacidad / datos personales):** el manejo de datos personales de clientes (incluido número de teléfono y contenido de conversaciones) debe cumplir la normativa colombiana de protección de datos (Ley 1581 de 2012 / Habeas Data) y las políticas de WhatsApp/Meta. Incluir aviso de tratamiento de datos y mecanismo de consulta/eliminación. *(Confirmar detalles con asesoría legal antes de producción.)*
- **RNF-06 (Disponibilidad):** objetivo de disponibilidad del servicio de recepción y respuesta ≥ 99% en el MVP.
- **RNF-07 (Observabilidad):** logging de cada turno, de las llamadas a herramientas y de métricas de costo (tokens, lecturas de caché) para diagnosticar y medir.
- **RNF-08 (Mantenibilidad):** cada agente `.md` debe estar acotado a una función; evitar archivos monolíticos que crecen (aprendizaje del patrón "Agent Operating Procedures": se vuelve difícil de depurar al crecer).
- **RNF-09 (Idioma):** español como idioma por defecto; el agente debe responder en el idioma del cliente cuando difiera.
- **RNF-10 (Resiliencia):** ante falla de una herramienta o del modelo, el agente debe degradar con gracia (mensaje honesto + opción de escalar), nunca romper la conversación.

---

## 8. Historias de Usuario (HU)

Formato de criterios de aceptación: **Dado / Cuando / Entonces**.

### 8.1 Administrador del agente

**HU-ADM-01 — Configurar el negocio**
Como administrador, quiero configurar el nombre, tono, horarios e idioma de mi negocio, para que el agente responda con la identidad correcta.
- Dado que estoy en el panel, cuando guardo los datos del negocio, entonces el agente los usa en sus respuestas y respeta los horarios definidos.

**HU-ADM-02 — Cargar conocimiento**
Como administrador, quiero cargar mis FAQs, catálogo y políticas, para que el agente responda con información real de mi negocio.
- Dado que subo o actualizo un documento, cuando finaliza el reindexado, entonces el agente puede responder preguntas basadas en ese contenido.
- Dado que no existe información sobre una pregunta, entonces el agente **no** inventa y cae a fallback.

**HU-ADM-03 — Crear un agente**
Como administrador, quiero crear un agente indicando qué intención atiende, qué herramientas puede usar y su estilo, para cubrir un tipo de conversación específico.
- Dado que creo un agente y lo activo, cuando un cliente escribe algo que coincide con su descripción, entonces el orquestador enruta a ese agente, sin cambios de código.

**HU-ADM-04 — Restringir herramientas de un agente**
Como administrador, quiero definir qué herramientas puede usar cada agente, para controlar qué puede hacer.
- Dado que un agente no tiene permitida una herramienta, cuando el modelo intenta usarla, entonces la ejecución se bloquea.

**HU-ADM-05 — Probar antes de publicar**
Como administrador, quiero probar el agente en un chat de prueba, para validar su comportamiento sin exponerlo a clientes reales.
- Dado que estoy en el sandbox, cuando envío mensajes de prueba, entonces veo las respuestas y las herramientas invocadas sin afectar datos de producción.

**HU-ADM-06 — Ver la bandeja de escalamiento**
Como operador, quiero ver las conversaciones escaladas con su historial, para atender personalmente lo que el agente no resolvió.
- Dado que una conversación entra en modo humano, cuando abro la bandeja, entonces veo el historial completo y puedo responder.

**HU-ADM-07 — Ver solicitudes capturadas**
Como operador, quiero recibir y ver las solicitudes que capturó el agente, para darles seguimiento.
- Dado que un cliente completa una solicitud, cuando reviso las notificaciones/bandeja, entonces encuentro la solicitud estructurada con sus datos.

**HU-ADM-08 — Ver métricas**
Como administrador, quiero ver volumen, % resuelto sin humano y escalamientos, para saber si el agente me ahorra tiempo.
- Dado un rango de fechas, cuando abro métricas, entonces veo los indicadores del período.

### 8.2 Usuario final (cliente)

**HU-USR-01 — Obtener información**
Como cliente, quiero preguntar por WhatsApp y recibir una respuesta correcta, para resolver mi duda sin esperar.
- Dado que pregunto algo cubierto por el conocimiento del negocio, cuando envío el mensaje, entonces recibo una respuesta basada en información real del negocio.

**HU-USR-02 — Ser entendido cuando soy ambiguo**
Como cliente, quiero que el agente me pregunte si no entiende, en lugar de responder cualquier cosa.
- Dado un mensaje ambiguo o con dos intenciones, cuando lo envío, entonces el agente hace una pregunta de aclaración antes de responder.

**HU-USR-03 — Dejar una solicitud**
Como cliente, quiero pedir/agendar algo por WhatsApp, para no tener que llamar o ir en persona.
- Dado que solicito una acción, cuando entrego los datos necesarios, entonces el agente confirma que la solicitud quedó registrada y el negocio es notificado.

**HU-USR-04 — Continuidad en la conversación**
Como cliente, quiero que el agente recuerde lo que venimos hablando, para no repetir todo.
- Dado que respondo a una pregunta del agente, cuando envío el dato, entonces el agente continúa el flujo sin reiniciar.

**HU-USR-05 — Hablar con una persona**
Como cliente, quiero poder pedir hablar con una persona, para casos que el agente no resuelve.
- Dado que pido un humano (o estoy molesto/el agente falla repetidamente), cuando lo expreso, entonces la conversación se escala y se me informa que una persona me atenderá.

**HU-USR-06 — Ser reconocido si ya soy cliente**
Como cliente que regresa, quiero que el agente recuerde datos básicos míos, para una atención más ágil.
- Dado que ya interactué antes, cuando vuelvo a escribir, entonces el agente puede usar mi memoria de cliente (nombre, solicitudes previas) manteniendo el aislamiento por negocio.

**HU-USR-07 — Respuesta honesta cuando no se puede**
Como cliente, quiero que el agente sea honesto cuando no puede ayudar, en lugar de inventar.
- Dado un tema fuera de alcance, cuando pregunto, entonces el agente lo dice, indica qué sí puede hacer y ofrece escalar.

---

## 9. Modelo de datos (nivel conceptual)

- **Tenant/Negocio:** id, nombre, tono/persona, horarios, idioma, medio de notificación.
- **Agente (configuración):** id, tenant_id, nombre, descripción-para-enrutamiento, cuerpo de instrucciones (`.md`), herramientas permitidas, alcance de memoria, estilo, estado (activo/inactivo).
- **Base de conocimiento:** documentos + embeddings, por tenant.
- **Conversación / historial:** por (tenant, cliente): turnos recientes, estado (auto/humano), flujo activo.
- **Memoria de cliente:** por (tenant, cliente): perfil estructurado.
- **Solicitud capturada:** id, tenant_id, cliente, tipo, datos estructurados, estado, timestamp.
- **Log de turnos:** entrada, salida, intención detectada, herramientas usadas, métricas de tokens/costo.

---

## 10. Métricas de éxito del MVP

- **% de conversaciones resueltas sin humano** (indicador principal de valor).
- **Tiempo de respuesta** promedio al cliente.
- **Costo por conversación** (tokens efectivos con caché aplicada).
- **Tasa de escalamiento** y motivos.
- **Tiempo ahorrado estimado** al negocio (proxy: consultas resueltas × tiempo medio de atención manual).

---

## 11. Fuera de alcance del MVP (roadmap posterior)

- **Pagos** (membresías, tickets, cobros).
- **Integraciones con APIs externas del cliente** (POS, calendarios, generación de documentos): modalidad **concierge / tier 2**, configuradas por nuestro equipo vía admin interno, usando el mismo esquema de configuración.
- **Constructor self-service de herramientas** (tier 3): registrar endpoints propios desde el panel.
- **Multicanal activo** (email, chat web, ecommerce): el adaptador queda en la arquitectura, pero solo se despliega WhatsApp.
- **Resolución de identidad entre canales** (unir email + WhatsApp del mismo cliente): requiere señal verificada; no se une por conjetura.
- **Campañas/mensajes salientes** (plantillas con costo por mensaje de WhatsApp).
- **Memoria semántica de largo plazo avanzada** y frameworks de orquestación (Claude Agent SDK / LangGraph) para flujos con estado/larga duración.

---

## 12. Supuestos y riesgos

**Supuestos**
- El acceso a WhatsApp se hace inicialmente vía BSP (p. ej. 360dialog/Wati) para acelerar verificación y aprobación.
- La mayoría del tráfico del MVP es reactivo, dentro de la ventana de servicio gratuita de 24h.
- El operador humano está disponible para atender escalamientos.

**Riesgos**
- **Costo por conversación** si no se aplica caching/tiering desde el inicio (riesgo #1 según Gartner: costos crecientes).
- **Fuga entre tenants** si el aislamiento no se aplica en la capa de herramientas.
- **Sobre-alcance:** intentar multicanal, pagos o integraciones antes de validar el loop central con clientes reales de WhatsApp.
- **Agentes monolíticos** difíciles de depurar si crecen sin acotarse.
- **Privacidad de datos** si no se incorpora el aviso de tratamiento y el cumplimiento normativo desde el diseño.
