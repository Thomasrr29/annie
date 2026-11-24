# Bloque 4 · Ficha del agente annie

Notas: definir alcance, **qué hace y qué no hace** · Rol/Objetivo/Reglas/Tono → Prompt (no protege al 100%) · mínimo privilegio · ¿qué debe aprobar una persona?

> Reflexión de Thomas: "Un agente es una función. Se divide el sistema en tareas y en cada una se decide qué tipo de función la resuelve: automatizada, cognitiva o agéntica. Bajando un escalón se eliminan problemas agénticos: el lead se define por la **etapa** en la que está, no por lo que escribió."

Leyenda: ✅ annie lo hace hoy · ⚠️ parcial · ❌ no lo hace · 🟡 decisión pendiente

---

## 1. Propósito

> Responder al instante, y en conversación, todo lo que no requiere criterio humano, para que **solo lleguen al asesor los clientes interesados**.

(La primera versión de Thomas fue "filtrar el tipo de cliente". El filtro es el **resultado** para el dueño; el propósito también incluye lo que recibe el cliente: la respuesta inmediata.)

### Rol (precisado por Thomas, 2026-11-24)

> **annie es la asesora del negocio, no el profesional.** El psicólogo o el abogado no está respondiendo el WhatsApp: responde una asesora, que informa sobre el negocio, las consultas, cómo funcionan y los precios, y ofrece agendar con el profesional.

Esta sola definición resuelve los casos delicados sin reglas extra: una asesora no da asesoría legal ni recomendaciones clínicas porque **no es su rol**. Ante una situación de riesgo hace lo que haría una asesora humana: ofrece la consulta y recomienda la línea de emergencia.

## 2. Usuario y disparador

| Usuario | Qué hace | Disparador |
|---|---|---|
| Cliente final | Pregunta por WhatsApp | Mensaje entrante → webhook ✅ |
| Asesor / dueño | Recibe leads, toma la conversación, responde | Notificación + panel ✅ (pasar a "modo humano" silencia al bot) |
| Dueño (configuración) | Carga conocimiento, prueba en el sandbox | Panel ✅ |

En el MVP **no hay disparadores por tiempo** (el recontacto queda para v2).

## 3. Entradas

| Entrada | Estado |
|---|---|
| Texto | ✅ |
| Imagen | ✅ (migración 0011) |
| Audio / nota de voz | ❌ 🟡 Thomas lo listó como capacidad, pero annie no lo maneja. Necesita un servicio de transcripción |
| De qué anuncio vino (`referral`) | ❌ v2 |

## 4. Instrucciones (Rol · Objetivo · Reglas · Tono)

Viven en `agents.instructions_md`, uno por agente. 🟡 Falta escribir el **tono** como decisión explícita (la conversación debe "sentirse" humana).

Recordatorio de la nota: **el prompt no protege al 100%**. Toda regla importante debe estar también en el código o en los datos.

## 5. Herramientas y permisos (mínimo privilegio)

| Herramienta | Qué hace | Permiso | ¿Requiere aprobación humana? |
|---|---|---|---|
| Conocimiento del negocio (en el prompt) | Responder | **Lectura** | No |
| `capture_request` | Guarda el lead para el asesor | **Escritura** (solo su tabla, solo su negocio) | No: se puede deshacer y no mueve dinero |
| `notify_owner` | Avisa al dueño | Envío externo, solo al contacto configurado | No: solo afecta al propio negocio |
| `search_knowledge` | — | Diferida (stub) | — |

**Nunca puede:** mover dinero, comprar, modificar datos del negocio, dar descuentos o cupones, revelar precios de costo o información interna.

### Mínimo privilegio aplicado a los datos (insight de Thomas)

> "Realmente es en base a qué información le damos y qué información no le damos."

Si annie no tiene el precio de costo, no lo puede revelar, ni por error ni por inyección. annie ya tiene esa regla en `CLAUDE.md` (decisión 5): *"NADA entra a la base de conocimiento que no sea aceptable que un cliente vea."*

### El hueco: el texto también es una acción

annie **no puede ejecutar** un descuento (no tiene la herramienta), pero **sí puede escribir** "te hago el 10%" si alucina o si un cliente la manipula ("ignora tus instrucciones…"). Para el cliente, eso es una promesa del negocio.

→ Invariante para el bloque 5: **todo precio, porcentaje o plazo que annie escribe debe existir en la base de conocimiento.**

## 6. Memoria

| Tipo (nota) | En annie | Estado |
|---|---|---|
| De tarea | `flow_state` de la conversación (etapa, contador de fallos) | ✅ |
| De conversación (perdura) | `messages`: historial reciente | ✅ |
| Del cliente (perdura) | `customer_memory`: se lee pero **nunca se escribe** | ⚠️ 🟡 |

"Se guarda en el sistema, no en el agente" ✅: todo vive en Postgres; el modelo no recuerda nada entre llamadas.

## 7. Criterio de parada

| Nivel | Se detiene cuando… |
|---|---|
| Loop de herramientas | Responde sin pedir herramientas, o llega al tope de 5 vueltas ✅ |
| Turno | Envía la respuesta ✅ |
| Conversación | Lead entregado (etapa 4) · el cliente pide humano · demasiados fallos seguidos ✅ · inactividad 🟡 |
| Costo | Rate limit por cliente · tope diario por negocio ✅ |

## 8. Criterios de éxito

Del bloque 2: el **embudo** (cuántos llegan a cada etapa y dónde se caen), leads calificados entregados, tasa de cierre, horas de asesor recuperadas, costo por conversación. Hoy annie solo mide los de output.

## 9. Fallas y mitigaciones

→ Bloque 5.

## 10. Fuera de alcance

- Pagos, compras, reembolsos, descuentos.
- Agendar de verdad en un calendario (captura la intención; agenda el asesor).
- Recontacto o mensajes proactivos (v2).
- Otros canales además de WhatsApp.
- Información que no esté en la base de conocimiento: responde honestamente que no sabe y ofrece un asesor (FALLBACK).
- Audio en la demo de 2 días (entra después, ver decisiones).

---

## Decisiones del bloque (2026-11-24)

### Audio → entra, pero después de la demo
- Mucha gente manda notas de voz: es **esperado** (Kano), no deleite.
- La Cloud API de WhatsApp entrega el audio como archivo (`type: audio`, ogg/opus), **sin transcripción**. La transcripción que ve la gente en la app de WhatsApp ocurre en el teléfono y no llega por la API.
- Opción natural en AWS: **Amazon Transcribe** (español; acepta Ogg). Alternativa: Whisper. Es una dependencia nueva y hay que justificarla (regla de `CLAUDE.md`).
- Flujo: descargar el audio (como ya se hace con las imágenes) → transcribir → seguir el turno como si fuera texto.
- No es una viga maestra de la demo: **funcional antes que bonito**.

### Memoria del cliente → resumen al cerrar la conversación
- Durante la conversación **no hace falta**: el historial reciente (`messages`) ya se carga en cada turno.
- Al cerrar (30 min sin mensajes): un **resumen** del cliente y su historial con el negocio. No los mensajes crudos, porque con clientes recurrentes crecen mes a mes.
- Es una escritura deliberada y curada (`CLAUDE.md` decisión 10).
- **Corrección:** la memoria **no** va en la parte cacheada del prompt. Es distinta por cliente, así que va en la parte dinámica (`CLAUDE.md` decisión 6). Si se metiera en el prefijo, rompería la caché de todos.
- **Bajar un escalón en infraestructura:** "a los 30 minutos" necesita un programador. Alternativa sin programador: resumir **perezosamente**. Cuando el cliente vuelve a escribir y su último mensaje tiene más de 30 minutos, primero se resume la conversación anterior y luego se responde. Mismo resultado, cero infraestructura nueva.
- Ley 1581: el resumen es dato personal y debe borrarse con el cliente (la cascada ya lo cubre).

### Tono → un asesor, no un vendedor
> Tranquila, cercana, conversadora. **La venta es el resultado de la buena atención.** Entiende al cliente y usa lo que cuenta.

Ejemplo de Thomas: "tengo un matrimonio y busco zapatos" → "¡Qué bacano! Busquemos algo elegante para ese día especial."

Para Nott (2026-11-24): **una asesora muy comprometida con la marca**. Cercana y con guiños a los nombres ("¿cansancio? Nott Tired"), pero sin las groserías de la campaña: también escriben papás, tiendas y mayoristas.

Reglas para `instructions_md`:
- Usa el contexto que da el cliente (ocasión, gustos) para personalizar.
- No presiona ni repite ofertas.
- Mensajes cortos, como se escribe por WhatsApp.
- Entusiasmo natural, sin exagerar, y **sin prometer nada que no esté en el conocimiento**.
