# Experimento: ¿por qué annie dice "$30.000" para el tinte?

**Fecha:** 2026-11-24 · **Pregunta:** "hola, ¿cuánto vale un tinte y qué horario tienen el sábado?"
**Error:** el modelo escribe "El tinte completo vale $30.000... perdón, $90.000" ($30.000 es el corte).
**Pregunta de Thomas:** ¿es por el *effort* o por cómo le llega la información del contexto?

## Método

Mismo código de producción (`build_system_prefix` + `build_messages` + `run_agent_turn`), el agente y el negocio **leídos de la base**, modelo real. Se cambia un factor a la vez. 12 llamadas por celda.

## Resultados

| Orden del conocimiento | Mensaje del cliente | Effort | Errores |
|---|---|---|---|
| seed (Horarios, Servicios…) | duplicado | low | 0/12 |
| seed | una vez | low | 0/12 |
| **prod** (Políticas, Servicios, Horarios…) | **duplicado** | low | **6/12** |
| prod | una vez | low | 0/12 |
| prod | duplicado | **medium** | **5/12** |

## Conclusión

1. **No es el effort.** Subirlo a medium casi no cambia nada (6/12 → 5/12).
2. **Es el contexto.** El error aparece solo cuando se juntan dos defectos de cómo se arma el prompt:
   - **El mensaje del cliente llega duplicado.** El historial se lee después de guardar el mensaje entrante (así que ya lo incluye) y `build_messages` lo vuelve a pegar al final. El modelo recibe `"…¿cuánto vale un tinte…?\n…¿cuánto vale un tinte…?"`.
   - **El orden del conocimiento es aleatorio.** Los documentos se ordenan por `created_at, id`. El seed los crea en la misma transacción (mismo `created_at`), así que el orden lo decide el `id`, un UUID aleatorio. El negocio de la base quedó con "Políticas de cita" primero.
   - Cada defecto por separado: 0 errores. Juntos: la mitad de las veces.
3. Una primera réplica a mano dio **0/30**, porque usaba otro orden. Lección: **para reproducir un error hay que reproducir el contexto exacto**, no uno parecido. La primera réplica casi lleva a la conclusión equivocada ("no se reproduce").

## Conexión con las notas

- *"En un agente se evalúa el camino → TODO"*: el error no estaba en el modelo, estaba en **lo que el sistema le entrega** (contexto = la primera pieza del arnés).
- *"Decide before — verify after"*: la hipótesis más intuitiva (el effort) era falsa. Medir antes de cambiar evitó pagar más por cada respuesta sin arreglar nada.
- *"Córrelo varias veces"*: con 1 o 2 corridas por celda no se ve nada. Con 12 aparece el patrón.
- La guardia de precios sigue siendo necesaria: es defensa en profundidad. Arreglar el contexto baja la frecuencia del error; la guardia evita que el error que quede llegue al cliente.

## Después de corregir (mismo día)

Dos arreglos en el contexto:
1. **Sin duplicado:** el historial se lee **antes** de guardar el mensaje entrante (`_resolve_and_load`); el mensaje nuevo llega una sola vez. Test de regresión: `test_el_mensaje_nuevo_no_se_duplica_en_el_prompt`.
2. **Orden estable:** el conocimiento se ordena por `created_at`, luego `title`, luego `id`. Un empate ya no depende de un UUID aleatorio. Test: `test_knowledge_context_empate_de_carga_se_ordena_por_titulo`.

Medición de punta a punta por el sandbox (el mismo camino que WhatsApp), 12 veces la misma pregunta:

| | Antes | Después |
|---|---|---|
| Primer intento con el precio del corte | 3/3 por el sandbox (y 6/12 en el experimento) | **0/12** |
| Respuestas finales con $30.000 (lo que ve el cliente) | 0, gracias a la guardia | **0/12** |

0/12 no prueba que el error ya no exista: prueba que bajó mucho. La guardia se queda como red de seguridad.

## El paso final: el modelo ya no escribe precios (catálogo)

Thomas: *"aquí no deberían haber errores"*. Reducir no basta: con datos tan básicos, el cliente no debe ver un precio equivocado nunca.

La garantía no puede venir del modelo, que es probabilístico aunque tenga la información enfrente. Tiene que venir del sistema:

- **Tabla `catalog_items`** (migración 0012): lo que vende el negocio y su precio. Los precios salen del texto del conocimiento.
- **El modelo ve el catálogo sin cifras:** `- Tinte completo: {{precio:tinte-completo}}`. No puede copiar mal un número que nunca vio.
- **El sistema pone el número:** reemplaza cada referencia por el precio real (`app/core/catalog.py`).
- **La guardia, en modo estricto:** un precio escrito a mano, una referencia que no existe o la referencia de otro ítem no salen. Se corrigen una vez y, si no, se da una respuesta segura.

Medición por el sandbox, con el modelo real: 5 preguntas × 6 corridas (un precio, dos precios en la misma frase, un servicio que no existe y un intento de descuento):

| | Resultado |
|---|---|
| Respuestas correctas | **30/30** |
| Precio equivocado al cliente | **0/30** |
| Veces que la guardia tuvo que intervenir | **0/30**: el modelo usó las referencias desde el primer intento |

Principios de la clase aplicados: *"lo que ve la persona sale del sistema, no del modelo"* y *"la acción peligrosa no existe como opción"* (el modelo no tiene cómo escribir un precio).

## Costo del experimento

Unas 110 llamadas a Sonnet, alrededor de 0,15 USD.
