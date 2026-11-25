# Bloque 6 · Build: de los hallazgos al plan de construcción

Notas de la clase:
- Con IA, el esfuerzo humano se va a **planear/diseñar** y a **probar/entregar**; escribir código cae al mínimo.
- **Decidir antes — verificar después:** confiabilidad · seguridad · arquitectura correcta.
- **Entrega el arnés:** contexto · herramientas · validación · memoria · loop de control.
- **Vigas maestras:** los bloques secuenciales que requieren criterio humano. **Paraleliza el resto.**
- **Funcional ANTES QUE BONITO.**
- 01 Parte del estándar · 02 Modela la amenaza antes del código · 03 Atrapa los errores antes de que lleguen al usuario.

## Meta

Demo para el curso: **escribirle por WhatsApp a annie, desplegada en AWS, como si fuera un negocio de servicios, y que el asesor reciba el lead calificado.**

## Backlog (sin ordenar, todo lo que salió de los bloques 1 a 5)

| # | Pendiente | Origen | Pieza del arnés |
|---|---|---|---|
| B1 | `notify_owner` envía de verdad: hoy no avisa a nadie | Bloque 5 | Herramientas · **Hecho el 2026-11-24** con SES (`app/core/email_ses.py`). Probado con SES simulado; falta un envío real en AWS |
| B2 | Espera de ráfaga + candado por conversación (I2) | Bloque 5 | Loop de control · **Hecho el 2026-11-25**: espera de 10 s (decisión de Thomas: "inmediato" puede ser ~10 s, como una persona), agrupa lo pendiente y responde una vez; un turno a la vez por conversación. Con el modelo real: 3 mensajes seguidos → 1 respuesta completa |
| B3 | Cerrar la conversación tras 30 min de inactividad | Bloque 5 | Memoria |
| B4 | Etapa del embudo por conversación; el **código** captura el lead al llegar a la etapa 3, sin duplicados (I5) | Bloques 2, 3 y 5 | Loop de control |
| B5 | Resumen para el asesor armado **por el sistema** con campos validados (nombre, servicio, fecha preferida) | Bloques 3 y 5 | Validación |
| B6 | Validar los argumentos de las herramientas (`request_type` de una lista cerrada) | Bloque 5 | Validación |
| B7 | Guardia de salida: todo precio o porcentaje de la respuesta existe en el conocimiento (I1) | Bloques 4 y 5 | Validación · **Hecho el 2026-11-24** (`app/core/guards.py`): existencia + atribución, corrige una vez y, si falla, da una respuesta segura. Con el modelo real, la pregunta del tinte salió con el precio del corte **3 de 3 veces** y se corrigió las 3: el error es sistemático, no raro. Hipótesis por probar: `RESPOND_EFFORT = "low"` |
| B8 | Detección de crisis → escala de inmediato + línea de emergencia (S1) | Bloque 5 (servicios) | Validación |
| B9 | Instrucciones y tono de un negocio de servicios de ejemplo (reemplaza la "Peluquería Demo" del seed) | Bloque 4 | Contexto |
| B10 | Script del set de evaluación contra el sandbox, N corridas; si se rompe una invariante, falla | Bloque 5 | Validación |
| B11 | Tests de aislamiento entre clientes del mismo negocio (I3) | Bloque 5 | Validación |
| B12 | Alertas de consumo (hoy solo un log) | Bloque 5 | Loop de control |
| B13 | Resumen de memoria del cliente (perezoso, al volver) | Bloque 4 | Memoria |
| B14 | Transcripción de notas de voz (Amazon Transcribe) | Bloque 4 | Contexto |
| B15 | Métricas del embudo + resultado del lead (`vendido` / `perdido`) | Bloque 2 | — |
| B16 | Dockerfile + despliegue en AWS + HTTPS | Bloque 7 | — · **Escrito el 2026-11-24** (`Dockerfile`, `infra/`, `deploy/deploy.ps1`). Las plantillas del servidor están revisadas; falta `terraform validate` y probar el build |
| B17 | App de Meta: número de prueba, token, webhook | Bloque 7 | — |

## Decisiones pendientes antes de construir

- **Canal del aviso al asesor (B1):**
  - **WhatsApp al número del asesor:** natural, pero fuera de la ventana de 24 h exige una plantilla aprobada por Meta.
  - **Correo con Amazon SES:** encaja con "proyecto en AWS"; en modo sandbox solo envía a correos verificados (suficiente para la demo).
- **Negocio de ejemplo (B9):** ¿psicólogo (muestra el manejo de crisis, más delicado), abogado, coach o discoteca?

## Ejercicio: vigas maestras vs. paralelizable

_Se completa con Thomas._
