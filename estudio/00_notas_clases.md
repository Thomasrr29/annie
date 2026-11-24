# Notas de clase — transcripción ordenada

Transcripción de las notas a mano (9 páginas), ordenadas por clase y por etapa.
Lo que está entre `[?]` no se leía bien en la foto: corrígelo si no es lo que escribiste.

---

## Clase 1 — Product Engineering

### Dos perfiles

| Product Engineer | Forward Deployed Engineer |
|---|---|
| Identifica la idea a construir **con un método** | Perfil técnico para clientes técnicos |
| ¿Qué deberíamos construir? Impacto a través de tu trabajo | Entrega técnica (en el terreno del cliente) |
| Ideas → Impacto | |

### El framework: **Define → Build → Ship**

| | Estrategia | Ejecución | Medición |
|---|---|---|---|
| **Define** | ¿Por qué construir esto? | ¿Qué exactamente? | ¿Cómo sabremos (que funcionó)? |
| **Build** | ¿Cuál es la arquitectura? | ¿Cómo se construye? | ¿Es suficientemente bueno? |
| **Ship** | ¿Cómo llega a los usuarios? | ¿Qué están haciendo? | ¿Qué aprendimos? |

---

### DEFINE

- Visión · Prioridades · **Las métricas definen el éxito**
- Elegir más rápido sigue requiriendo criterio (*judgement*).
- ¿Qué problema deberíamos resolver?
- Construir ya no es la parte difícil: la IA expande rápido lo que podemos hacer `[?]`.
- ¿Lo hizo mejor? *(Did it make things better?)*

**Claridad:** Actividad → Output → Resultado → **Impacto**

**Las 5 preguntas**
1. ¿El problema existe?
2. ¿Qué progreso quiere lograr la persona?
3. ¿Qué tipo de valor crea esto?
4. ¿Vale la pena construirlo ahora?
5. ¿Qué podría salir mal?

> El cliente a veces no sabe cuál es su necesidad: pide demasiado porque no conoce la necesidad real.

**01 · ¿El problema existe? → Pregunta por la última vez**
- Preguntar por *la última vez* revela las herramientas, la información y el impacto **reales**.
- No preguntes "¿qué usarías?" o "¿qué te gustaría ver?": eso es suposición.
- "Muéstrame el último reporte" → muéstrame los pasos · ¿cada cuánto? · ¿cuánto costó?

**02 · Describe el progreso que necesitan (antes de construir la solución)**
> Cuando **[situación]**, ayúdame a **[lograr un progreso]** para poder **[estado deseado]** sin **[obstáculo]**.
- No quieren reconocimiento facial: quieren **entrar más rápido**.

**03 · ¿Qué tipo de valor? Un dashboard → una alerta a tiempo para actuar en los casos que importan**

Curva de satisfacción vs. cumplimiento de la necesidad (modelo Kano):

| Tipo | Ejemplo | Comportamiento |
|---|---|---|
| **Esperado** | La comida correcta | Si falta, frustra. La gente lo da por hecho |
| **Desempeño** | Una espera más corta | Mejor = más satisfacción (lineal) |
| **Deleite** | Un postre que amas | Beneficio inesperado. Nadie lo extrañaría si falta |

**04 · ¿Vale la pena ahora?**
- ¿Quién está sufriendo **ya**?
- ¿Qué cuestan 90 días más (de esperar)?
- ¿Qué podemos aprender o experimentar de forma segura?

**05 · ¿Qué podría salir mal? → Las dos puertas**

| Sin reversa (puerta de una vía) | Con reversa (puerta de dos vías) |
|---|---|
| Experto · lento · análisis · buen criterio | Rápido · más gente puede decidir |
| Resolver las incógnitas críticas | Probar la reversa |
| Traer a los revisores correctos | **Acotar la exposición** |

**Dale a liderazgo decisiones que puedan tomar:** Problema → Pedido → Retorno, con claridad.

---

### BUILD — ¿a dónde va el esfuerzo humano?

Curva: **antes** el esfuerzo pico estaba en *escribir* el código. **Con IA** el esfuerzo se va a los extremos: *planear/diseñar* y *probar/entregar*; escribir cae al mínimo.

- **Decidir antes — verificar después.** Lo que se decide antes: confiabilidad · seguridad · arquitectura correcta.
- **Entrega el arnés (*ship the harness*):** contexto · herramientas · validación · memoria · loop de control.
- **Encuentra las vigas maestras (*tent poles*):** los bloques secuenciales que requieren criterio humano. **Paraleliza el resto.**
- **Funcional ANTES QUE BONITO.**

1. **Parte del estándar:** estandariza los patrones comunes, define estándares. Plantillas = contexto que codifica el patrón.
2. **Modela la amenaza antes del código:** ¿qué tengo que proteger? Activo → amenaza → control · verificación · mínimo privilegio.
3. **Atrapa los errores antes de que lleguen al usuario.**

> El sistema tiene que estar construido para que la gente tome la práctica correcta: creamos los caminos de buenas prácticas para que se sigan (*paved road*).

---

### SHIP — entender el comportamiento del cliente

| Ser dueño de un resultado | Habilitar el loop | Hacer el caso |
|---|---|---|
| Conectar el código con el progreso del cliente | Darle al equipo contexto y espacio de decisión para hacer trabajo completo | Mostrar resultados antes de pedir grandes cambios |

---

## Clase 2 — Agentes

### El espectro: ¿automatización, cognitiva o agente?

| Automatización | Cognitiva | Agente |
|---|---|---|
| Plataforma | Plataforma | Plataforma |
| Infraestructura mínima | API del modelo | Observabilidad |
| | Pago por tokens | Sesiones · Memoria |
| | | Auditoría · Aprobación humana · Monitoreo |

Regla: **bajar escalones** (usa el nivel más simple que resuelva) · topes a los bucles · uso de caché.

### Ficha del agente — definir alcance (qué hace y qué no)

1. Propósito
2. Usuario y disparador
3. Entradas
4. Instrucciones
5. Herramientas y permisos
6. Memoria
7. Criterio de parada
8. Criterios de éxito
9. Fallas y mitigaciones
10. Fuera de alcance

### Definir quién es y qué hace

- **Rol · Objetivo · Reglas · Tono → Prompt.** El prompt no protege al 100%: el modelo puede alucinar.
- **Herramientas = funciones:** qué hace → qué recibe → qué devuelve → **permisos** (lectura, escritura…).
- Puede / Nunca puede.
- **Mínimo privilegio:** no darle la herramienta al agente es la mejor limitación y la mejor regla anti-alucinación.

### Memoria

- **De tarea** (lo de este trabajo) vs. **de conversaciones** (perdura).
- **Se guarda en el sistema, no en el agente.**

### ¿Qué debe aprobar una persona?

¿Mueve dinero? · ¿Se puede deshacer? · ¿Sale de la política? · ¿Afecta a terceros?

### Fallas típicas de agentes

| Falla | Mitigación |
|---|---|
| 01 Bucle sin fin | Topes y detectar repetición |
| 02 Mal uso de herramientas | Validar argumentos antes de ejecutar |
| 03 Inyección de instrucciones | La acción peligrosa **no existe** como opción |
| 04 Gasto descontrolado | Tope, caché y alertas de consumo |

- **El texto de terceros son datos, NUNCA órdenes.**
- La acción no existe → no hay compra sin aprobación.
- El resumen o lo que ve la persona sale **del sistema**, no del modelo.
- Los datos tienen que haberse visto / ya procesado → un número de pedido **ya validado**.

### Evaluación

- **En un agente se evalúa el camino → TODO** (no solo la respuesta final).
- Datos de prueba: normales → límites → adversarios.
- **Verificar invariantes** = reglas que nunca se pueden romper. Córrelo varias veces → verifica invariantes → **si una invariante se rompe, no se despliega.**

### Patrones de diseño de agentes

| Patrón | Idea |
|---|---|
| Encadenamiento | Pasos en secuencia; la salida de uno es la entrada del siguiente |
| Enrutamiento | Un clasificador manda cada caso al camino especializado |
| Paralelización | Varias llamadas al mismo tiempo; se combinan en una respuesta |
| Orquestador y trabajadores | El orquestador reparte subtareas y luego integra |
| Evaluador y optimizador | Verifica si cumple criterios; si no, retroalimenta y reintenta; si sí, retorna |
| Agente | El modelo decide sus pasos y herramientas en un loop hasta cumplir el objetivo |
