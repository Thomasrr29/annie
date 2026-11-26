# Cómo una sola annie atiende a muchos negocios (multi-tenant)

Lo dedujo Thomas: **no es un número para todos, es un backend para todos.** Cada negocio tiene **su** número de WhatsApp; todos los números le avisan al **mismo** backend; y el backend decide de qué negocio es cada mensaje **por el número que lo recibió**.

## El camino de un mensaje

```
Cliente de Baku  ──► número de Baku  ─┐
                                      ├──► Meta ──► POST /webhooks/whatsapp (UN solo backend)
Cliente de Gooms ──► número de Gooms ─┘                    │
                                                           ▼
                       1. Leer metadata.phone_number_id del mensaje
                       2. Buscar ese número en whatsapp_number_map ──► business_id
                       3. Abrir la sesión de BD fijando ese negocio
                          (SET LOCAL app.current_business_id)
                       4. Desde aquí, RLS en Postgres solo deja ver filas de ESE negocio
                       5. Cargar SU conocimiento, SUS agentes y SU tono
                       6. Responder desde SU número
```

En el código:
- Pasos 1 y 2: `app/api/webhooks.py:96` y `app/api/webhooks.py:102` (`get_id_by_whatsapp_number`).
- La vista `whatsapp_number_map` (migración `0008`) expone **solo** "este número es de este negocio". Ningún otro dato se puede leer antes de saber el negocio.
- Paso 3: `tenant_session(business_id)` en `app/db/session.py`.

## "annie es la misma para todos": ¿entonces se puede mezclar la información?

- **El código es uno solo.** Lo que cambia por negocio es la **configuración**: conocimiento, agentes, tono y número. "Un motor, N configuraciones."
- **El modelo no recuerda nada entre llamadas.** Cada llamada a Claude recibe **solo** el prompt que armamos: el conocimiento de **ese** negocio y la conversación de **ese** cliente. El modelo no puede filtrar lo que nunca se le dio. Es la nota: *"se guarda en el sistema, no en el agente"*.
- **Dos capas de protección** (`CLAUDE.md` decisión 8):
  1. Toda consulta filtra por `business_id` en el código.
  2. Si alguien se equivoca en el código, RLS en Postgres bloquea igual.
- **La caché también es por negocio:** cada negocio tiene su propio prefijo cacheado, y los prefijos nunca se mezclan.

## ¿Hay que crear una app de Meta por cliente? No.

- **Una sola app de Meta: la tuya.** Tú eres el proveedor de tecnología.
- Cada cliente tiene **su** cuenta de WhatsApp Business (WABA) y **su** número, y la **conecta a tu app**. No crea una app propia.
- Todos los mensajes de todos los números llegan a **tu único webhook**, y el `phone_number_id` dice de quién es cada uno.
- Meta le cobra los mensajes a la cuenta de cada cliente.

| Etapa | Cómo se conecta un cliente | Puerta |
|---|---|---|
| **Piloto (1 a 3 clientes)** | A mano: tú lo acompañas a registrar el número y lo conectas a tu app ("concierge") | Dos vías: barato y reversible |
| **Comercial** | **Embedded Signup:** un botón en tu onboarding abre el flujo de Meta; el cliente inicia sesión, elige o crea su WABA, verifica el número y queda conectado | Requiere verificar tu empresa ante Meta y ser **Tech Provider** |
| **Alternativa** | Un BSP (360dialog, Twilio…) hace la conexión por ti, a cambio de una comisión | Decisión abierta del roadmap (fase 3) |

**Verificar:** Meta anunció la **coexistencia**: usar la app de WhatsApp Business del celular y la Cloud API en el mismo número. Antes, pasar un número a la API lo sacaba de la app. Si está disponible en Colombia, la asesora podría seguir respondiendo desde el celular. Clave para pymes.

## ¿Dashboard o correo? Los dos, para cosas distintas

Nota de la clase (Define, 03): *"Un dashboard → una alerta a tiempo para actuar en los casos que importan."*

- **Dashboard (pull):** guarda todo y hay que ir a mirarlo. annie ya tiene uno: el panel de Streamlit, con las páginas **Bandejas** (leads y escalamientos) y **Conversaciones**.
- **Alerta (push):** llega sola cuando hay que actuar. Es **Amazon SES** (correo) para la demo.

El lead calificado vive en el dashboard; la alerta avisa que llegó. Un lead que nadie ve a tiempo es un lead perdido.

Límite conocido: el panel de Streamlit es la herramienta del **piloto** (sin multiusuario ni sesiones reales). Un dashboard comercial es otro proyecto.

## ¿Cómo se conecta el número de un negocio nuevo?

- Una sola app de Meta (la nuestra) y **un solo webhook** reciben los mensajes de **todos** los números conectados a ella.
- Para el MVP: se registra el número del negocio en Meta y se guarda su `phone_number_id` en `businesses.whatsapp_phone_number_id` (es único: un número nunca puede ser de dos negocios).
- A escala, Meta tiene un flujo para que cada negocio conecte su propio número desde un botón (*Embedded Signup*, para proveedores de tecnología). Eso es v2.
