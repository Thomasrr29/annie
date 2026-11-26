# Bloque 3 · ¿Agente, tarea cognitiva o automatización?

Notas: **bajar escalones** · topes a los bucles · uso de caché · los 6 patrones de diseño.

## La conclusión de Thomas

> No es un loop que se queda revisando y puliendo hasta llegar a un outcome: es un **proceso secuencial** (obtengo la información → redacto → entrego). Es una **automatización con tareas cognitivas**.

Es correcta. Con un ajuste a la definición:

> Lo que define a un agente no es cuánto dura ni que repita: es **quién decide el siguiente paso**.
> - **Workflow:** el **código** decide el camino; el modelo hace tareas dentro de pasos fijos.
> - **Agente:** el **modelo** decide qué hacer después, qué herramienta usar y cuándo terminó.
>
> (Es la distinción de Anthropic en *Building effective agents*: workflows vs. agents.)

## Pieza por pieza

| # | Pieza | Escalón | Por qué |
|---|---|---|---|
| 1 | Recibir el webhook y responder 200 | **Automatización** | Regla fija, sin juicio |
| 2 | Responder precio, ubicación, pagos | **Cognitiva** (redactar) | Lenguaje natural + conocimiento del negocio; un solo paso |
| 3 | Ubicar la etapa del embudo | **Cognitiva** (clasificar) + reglas | Haiku etiqueta la intención; el código cuenta las etapas |
| 4 | Pasar el lead al asesor | **Cognitiva** (extraer campos) + **automatización** (notificar) | El modelo extrae; el **sistema valida** y arma el resumen |
| 5 | Detectar frustración y escalar | **Reglas primero**, cognitiva después | Repetición, "asesor", FALLBACK repetidos son reglas baratas |
| 6 | (v2) Recontactar | **Automatización** (programador) + plantilla fija | Disparador por tiempo; el texto ni siquiera necesita modelo |

**Ninguna pieza necesita un agente autónomo.**

## Dónde está annie en el espectro

annie = **Enrutamiento + Encadenamiento**, con un **LLM aumentado** (con herramientas) en un paso.

```
webhook → límites → cargar contexto → [Haiku: clasificar/enrutar] → [Sonnet: responder (+ herramientas)] → guardar → enviar
           código       código              cognitivo                      cognitivo                    código    código
```

Matiz: dentro del paso de Sonnet hay un **loop mínimo** (`MAX_TOOL_ITERATIONS = 5`, `app/core/llm.py:56`): el modelo decide si llama `capture_request` o `notify_owner`. Es el único punto donde el modelo decide algo del flujo, y por eso tiene tope (falla típica 01: bucle sin fin).

Pregunta abierta (bajar otro escalón): ¿la captura del lead la debería disparar **el código** al llegar a la etapa 3, en vez de que el modelo decida llamar la herramienta? Menos autonomía = menos fallas de tipo 02 (mal uso de herramientas).

## ¿Cuándo sí haría falta un agente?

Por contraste, una tarea abierta del lado del **dueño**:

> "Analiza las conversaciones de esta semana, encuentra las objeciones más comunes y propón qué agregar a la base de conocimiento."

No hay un camino fijo: hay que leer, agrupar, decidir qué buscar después y cuándo es suficiente. Eso es **orquestador y trabajadores** o un agente, y pide lo que dice la nota: observabilidad, auditoría y aprobación humana (que el dueño apruebe antes de cambiar el conocimiento).

## Por qué importa bajar escalones aquí

| Si annie fuera un agente | Como workflow |
|---|---|
| Costo por conversación variable | Costo predecible (requisito del MVP) |
| Latencia de varios segundos o minutos | Respuesta en segundos (el valor central) |
| Más fallas: bucles, herramientas mal usadas | El código controla el camino |
| Evaluar todo el camino, que cambia cada vez | Evaluar pasos fijos |
