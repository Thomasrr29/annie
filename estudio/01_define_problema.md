# Bloque 1 · Define: ¿el problema existe?

## La hipótesis inicial (supuesto)

> "Hay muchos negocios que se demoran respondiendo preguntas simples."

Era una suposición: sin un negocio concreto y sin "la última vez".

## La evidencia: la última vez

Thomas lo vivió dos veces: hace unos 2 años y hace unos 6 meses.

- **Disparador:** se lanza una **pauta** (campaña de anuncios) y llega una **ráfaga** de mensajes.
- **Qué llega:** "precio", "¿talla?", "precio", "¿dónde están ubicados?", "¿cuánto vale?". Siempre lo mismo.
- **Cómo se resolvía:** a mano, uno por uno. Se iba el día entero.
- **Alternativa que existe:** las respuestas automáticas de WhatsApp Business. **No son una conversación.**
- **El costo real:** a todos se les dedica la misma atención y **no hay embudo**. Se pierden los que de verdad estaban interesados, y esos leads ya se pagaron con la pauta.

## El replanteamiento

El problema no es solo "responder rápido". Es:

> **Atender la ráfaga de una campaña y separar a los interesados de los curiosos, sin que una persona responda "precio" 200 veces.**

## El progreso que buscan (JTBD)

**El dueño (el que paga):**
> Cuando lanzo una campaña y me llegan cientos de mensajes, ayúdame a responder al instante y en conversación las preguntas de siempre y a separar a los interesados, para poder dedicar mi tiempo a cerrar ventas, sin perder leads que ya pagué con la pauta.

**El cliente final:**
> Cuando veo un anuncio y pregunto el precio, ayúdame a obtener la respuesta en el momento, para poder decidir si compro, sin esperar horas.

## Kano aplicado

| Tipo | En annie |
|---|---|
| Esperado | Respuesta **correcta** (precio, talla, ubicación). Si se equivoca, frustra |
| Desempeño | **Velocidad** de respuesta y **filtrar** bien quién está interesado |
| Deleite | Recordar al cliente entre conversaciones (memoria de cliente). No es prioridad del MVP |

## ¿Quién está sufriendo ya? (pregunta 04)

Lo concluyó Thomas: **sin volumen no hay dolor.** Un negocio con pocos mensajes no lo necesita.

El denominador común no es la industria, es:

> **valor del lead × minutos sin respuesta × volumen de mensajes**

| Segmento | Volumen | Valor por lead | El dolor |
|---|---|---|---|
| Campañas que llevan a WhatsApp | Alto, en ráfagas | Bajo o medio | Responder "precio" 300 veces y no filtrar |
| Servicios (agencias, clínicas, consultorios…) | Medio y constante | Alto | Responden tarde y el lead se va con otro |

Es el mismo trabajo ("cada minuto sin respuesta pierde una conversión") con dos discursos de venta distintos.

## El propósito real del proyecto (definir antes de medir)

| Objetivo | ¿Vale la pena ahora? | Qué evidencia necesita |
|---|---|---|
| **Aprender + portafolio + demo del curso** | Sí: puerta de dos vías, barato, mucho aprendizaje | Ninguna validación de mercado |
| **Negocio** | Todavía no se sabe | Un experimento barato antes de invertir más |

## Experimento barato (sin chats propios)

**Cliente incógnito:** escribir por WhatsApp a 20 o 30 negocios que hoy pautan (anuncios de Instagram con botón de WhatsApp) o que venden servicios, y anotar:
- tiempo hasta la primera respuesta,
- si fue una conversación o una respuesta automática,
- si llegaron a preguntar algo para calificar (presupuesto, talla, fecha).

Resultado: la línea base del mercado ("la mediana de respuesta es X horas") y una lista de negocios que ya sufren el problema.

## Decisión: un motor, dos configuraciones

Thomas: "es el mismo lead; en campañas solo cambia la masividad". El núcleo es el mismo (responder, calificar, entregar el lead), y annie ya está construida así ("un motor, N configuraciones").

Lo que **sí** cambia entre segmentos, y vive en configuración o infraestructura, no en código nuevo:

| | Servicios | Campañas |
|---|---|---|
| Criterio de calificación | Necesidad, presupuesto, fecha | Talla, stock, ciudad |
| % resoluble sin humano | Más bajo (cotizar requiere criterio) | Alto (precio, talla, ubicación) |
| Costo de un error | Alto (lead valioso) | Bajo por lead, alto en volumen |
| Exigencia técnica | Calidad de la conversación | **Ráfagas:** concurrencia, topes, costo pico |
| Atribución | — | De qué anuncio vino el lead (`referral` del webhook de Meta) |

Elegir segmento aquí es una **puerta de dos vías**: barata y reversible. No merece más análisis.

### Foco del MVP: servicios (decisión del 2026-11-24)

Psicólogos, abogados, asesores, terapeutas, coaches… y negocios con pocos productos simples, como una discoteca (el producto es el cover).
- **Sin catálogo de productos** (tallas, stock): pasa a v2.
- La conversión es **una cita o una reserva**, no una compra. annie captura la intención; agenda el humano.
- Las campañas masivas quedan como segundo segmento, con el mismo motor.

**Ampliación:** también encajan tiendas con **pocos productos**, como Gooms (gomitas y mentas), Baku (comida saludable a domicilio) o un servicio de almuerzos a domicilio de unos $12.000. Sus preguntas son repetitivas y operativas: cómo llega el pedido, cada cuánto, si devuelven el envase.

El criterio real del segmento no es "servicio vs. producto", es:
> **Todo lo que hay que saber del negocio cabe en una o dos páginas**, y las preguntas se repiten.

Si Thomas conoce a los dueños, son **candidatos a piloto real**: la oportunidad de hacer el "muéstrame tu WhatsApp de ayer" que faltó en el bloque 1.

## Qué falta para que deje de ser n = 1

- [ ] Analizar los chats reales de las campañas: volumen, % de preguntas repetitivas, tiempo de respuesta, cuántos compraron.
- [ ] Ese análisis es también la **línea base** (para demostrar la mejora) y el **set de prueba** del agente (bloque 5).
- [ ] Validar con 2 o 3 negocios más que lancen pauta y vendan por WhatsApp.

## Lo que esto cambia en annie (para revisar después)

- `capture_request` (el flujo ACT) cobra sentido: es **entregarle al dueño el lead calificado**.
- Los límites tienen que soportar **ráfagas**. Hoy el tope diario por negocio es de 1000 turnos (`app/core/limits.py:36`). Una campaña con cientos de leads, cada uno con varios mensajes, lo puede alcanzar justo en el pico.
