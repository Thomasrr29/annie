# Bloque 2 · Define: ¿cómo sabremos que funcionó?

Notas: **"Las métricas definen el éxito"** · Actividad → Output → Resultado → Impacto.

## La escalera aplicada a annie

| Escalón | Pregunta | Métrica en annie | ¿annie la mide hoy? |
|---|---|---|---|
| **Actividad** | ¿Qué hace el sistema? | Mensajes atendidos, turnos, conversaciones | ✅ `turnos`, `conversaciones` |
| **Output** | ¿Qué produce? | Respuestas al instante sin criterio humano · % de conversaciones sin humano · costo por conversación | ✅ `porcentaje_sin_humano`, `costo_usd_por_conversacion`, `latencia_ms_media` |
| **Resultado** | ¿Qué cambia en el comportamiento? | El lead sigue conversando y llega calificado · el asesor solo atiende interesados | ❌ No hay etiqueta de lead |
| **Impacto** | ¿Qué cambia en el negocio? | Más ventas por lead (tasa de cierre) · horas de asesor recuperadas · ventas por peso invertido en pauta | ❌ La venta ocurre fuera del sistema |

Hoy annie mide bien **actividad y output**, y no puede ver **resultado ni impacto**.

## La trampa del "% sin humano"

Con el objetivo de filtrar, una conversación que termina sin escalar puede ser:
- ✅ un curioso que quedó bien informado y se fue (filtro exitoso),
- ❌ un lead interesado que se cansó del bot y se fue (venta perdida).

Las dos suman igual al "% sin humano". Optimizar esa métrica sola puede **esconder ventas perdidas**.

## Calidad de la conversación (Kano: esperado)

Thomas: la conversación no debe ser corta, debe **sentirse** como conversación y ser precisa.

Señales de frustración, de la más barata a la más cara:
1. **Sin modelo (reglas):** el cliente repite la misma pregunta · pide "un asesor" o "una persona" · el bot cae en FALLBACK · el cliente deja de responder justo después de la respuesta del bot.
2. **Con modelo (LLM como juez):** Haiku califica una **muestra** de conversaciones cerradas (frustración, precisión, si se respondió lo que preguntó). En lote y fuera del turno, para no romper "una llamada al modelo por turno". Es el patrón **evaluador** de la clase de agentes.

## Comprador ≠ usuario

| | Quién | Qué le duele | Cómo se le vende |
|---|---|---|---|
| **Comprador** | El dueño | Plata: asesores caros y ventas perdidas | Impacto en pesos |
| **Usuario** | El asesor comercial | Responder "precio" todo el día a curiosos | Solo le llegan leads interesados |
| **Beneficiario** | El cliente final | Esperar | Respuesta al instante |

## Supuestos a vigilar

- **"El 80% de las conversaciones queda en el bot"** es una hipótesis, no un dato. Medirla en el piloto.
- Sin línea base (cuánto tardan hoy y cuánto cierran hoy) no se puede demostrar mejora. Ver el experimento del cliente incógnito en `01_define_problema.md`.

## ¿Qué es un lead calificado?

Thomas: **la calificación es un camino, no un mensaje.** "Precio" como primer mensaje no dice nada: lo escriben tanto el curioso como el comprador. Lo que revela la intención es **la secuencia de preguntas**: quien va a comprar va quitando objeciones una por una.

Y lo más importante: **los curiosos se filtran solos.** El bot no tiene que adivinar quién es quién. Tiene que responder bien, y el comportamiento revela la intención.

### El embudo de la conversación

| Etapa | Señales | Ejemplo |
|---|---|---|
| 0 · Contacto | Primer mensaje, genérico | "precio", "info" |
| 1 · Exploración | Preguntas de información | "¿cómo es el servicio?", "¿qué incluye?" |
| 2 · Objeciones | Preguntas que quitan barreras | "¿formas de pago?", "¿garantía?", "¿dónde quedan?" |
| 3 · Intención | Preguntas de acción | "¿tienen cupo el sábado?", "¿cómo agendo?", "¿hacen envíos a…?" |
| 4 · Entregado | Pasa al asesor con resumen | `capture_request` + `notify_owner` |

**Lead calificado = llegó a la etapa 3** (o a la 2 con varias objeciones resueltas).

Este embudo **es la métrica de resultado** que faltaba: cuántas conversaciones llegan a cada etapa y **en cuál se cae la gente**.

## Recontacto a los que se quedaron en el camino

Idea de Thomas: escribirle a quien se quedó a mitad del embudo ("¿quedaste con alguna duda?").

Choca con tres cosas:
1. **Reglas de WhatsApp:** dentro de las 24 h desde el último mensaje del cliente se puede escribir texto libre. Después solo con **plantillas aprobadas por Meta**: las de marketing se pagan y requieren consentimiento.
2. **Calidad del número:** si mucha gente bloquea o reporta, Meta baja la calificación del número y lo puede restringir. Para un negocio que vende por WhatsApp, eso es una **puerta casi sin reversa**.
3. **annie hoy:** `CLAUDE.md` decisión 7, "sin plantillas salientes ni mensajes proactivos en el MVP". Además, annie solo reacciona a mensajes entrantes. El recontacto necesita **otro disparador**: un programador de tareas (en AWS sería EventBridge Scheduler).

Dos puertas:
- **Recontacto único, dentro de las 24 h, solo a quien llegó a la etapa 2 o 3:** puerta de dos vías. Exposición acotada, se puede medir y apagar.
- **Campañas de remarketing con plantillas:** más cerca de una vía (reputación del número, Ley 1581). Necesita análisis.

Decisión: **fuera del MVP, candidata a v2**, empezando por la versión acotada.

## Pendientes que salen de aquí

- [ ] **Etiqueta del lead** (calificado / con pregunta / no interesado): es una clasificación, es decir una tarea cognitiva (bloque 3).
- [ ] **Cerrar el ciclo de la venta:** que el asesor marque el lead como `vendido` o `perdido`. Hoy `RequestStatus` solo tiene `new / seen / in_progress / done`, y "done" no dice si hubo venta.
- [ ] **Notas de voz:** en LatAm la gente manda muchos audios. annie hoy maneja texto e imagen. El audio necesita un servicio de transcripción aparte (la API de Claude no recibe audio). Decidir si es alcance del MVP o "fuera de alcance".
