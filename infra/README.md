# Infraestructura de annie en AWS (Terraform)

Toda la infraestructura de la demo en código: se crea con un `apply` y se borra con un `destroy`.

```
Internet ──► IP elástica ──► EC2 t3.small (Docker Compose)
                               ├─ caddy    HTTPS automático (Let's Encrypt)
                               ├─ api      FastAPI: /webhooks/whatsapp, /admin, /health
                               ├─ panel    Streamlit en /panel
                               ├─ db       Postgres 16 + pgvector
                               └─ migrate  alembic upgrade head
S3 (imágenes, borrado a los 90 días) · SES (aviso al asesor) · ECR (imágenes Docker)
SSM Parameter Store (secretos) · AWS Budgets (alerta de gasto)
```

**Costo aproximado:** unos 17–20 USD al mes (EC2 + disco + IP pública; S3, SES, ECR y SSM cuestan centavos). Sin NAT Gateway ni balanceador, a propósito. **Los tokens de Claude se cobran aparte, en la cuenta de Anthropic.**

| Archivo | Qué crea |
|---|---|
| `main.tf` | VPC, subred pública, security group (80/443, sin SSH), rol IAM de mínimo privilegio, EC2, IP elástica |
| `storage.tf` | Bucket S3 privado y cifrado con regla de retención · repositorios ECR (se conservan las últimas 5 imágenes) |
| `notifications.tf` | Identidad de SES · presupuesto mensual con alertas al 50/80/100% |
| `secrets.tf` | Contraseñas de la base y verify token del webhook en SSM |
| `templates/` | Lo que se instala en el servidor: compose, Caddyfile, `refresh.sh`, arranque |

## Requisitos (una vez)

```powershell
winget install Hashicorp.Terraform
```

```powershell
aws configure
```

## Paso a paso

**1. Variables**

```powershell
Copy-Item infra\terraform.tfvars.example infra\terraform.tfvars
```

Completar `owner_email` en `infra\terraform.tfvars`.

**2. Crear la infraestructura**

```powershell
terraform -chdir=infra init
```

```powershell
terraform -chdir=infra apply
```

Después del `apply`:
- Llega un correo de **AWS** para verificar la dirección en SES: abrirlo y hacer clic.
- Llega un correo de **AWS Budgets** cuando el presupuesto quede activo.

**3. Cargar los secretos de terceros** (no pasan por Terraform, para que no queden en su estado)

```powershell
aws ssm put-parameter --name /annie/ANTHROPIC_API_KEY --type SecureString --value "<api key>"
```

```powershell
aws ssm put-parameter --name /annie/WHATSAPP_PHONE_NUMBER_ID --type SecureString --value "<phone number id>"
```

```powershell
aws ssm put-parameter --name /annie/WHATSAPP_WABA_ID --type SecureString --value "<waba id>"
```

```powershell
aws ssm put-parameter --name /annie/WHATSAPP_ACCESS_TOKEN --type SecureString --value "<access token>"
```

```powershell
aws ssm put-parameter --name /annie/WHATSAPP_APP_SECRET --type SecureString --value "<app secret>"
```

Para cambiar un valor: el mismo comando con `--overwrite`.

**4. Desplegar** (construye, sube a ECR y reinicia el servidor vía SSM)

```powershell
.\deploy\deploy.ps1
```

**5. Configurar el webhook en Meta for Developers** → WhatsApp → Configuración:

```powershell
terraform -chdir=infra output webhook_url
```

```powershell
terraform -chdir=infra output -raw whatsapp_verify_token
```

Pegar los dos valores, verificar y suscribirse al campo `messages`.

**6. Comprobar**

```powershell
curl.exe -s "$(terraform -chdir=infra output -raw public_url)/health"
```

## Operación

Entrar al servidor sin SSH:

```powershell
aws ssm start-session --target (terraform -chdir=infra output -raw instance_id)
```

(Requiere el plugin de Session Manager para la AWS CLI.)

Ver los logs de la API, ya dentro del servidor:

```bash
cd /opt/annie && sudo docker compose logs -f api
```

## Puertas sin reversa (leer antes de `destroy`)

- **La base de datos vive en el disco de la EC2.** `terraform destroy`, o cualquier cambio que reemplace la instancia, **borra todas las conversaciones**. Por eso la AMI está en `ignore_changes`. Pendiente: backups (snapshot diario del disco con Data Lifecycle Manager, o `pg_dump` a S3).
- **El bucket S3 no se borra si tiene archivos:** hay que vaciarlo antes del `destroy`.
- **El estado de Terraform (`infra/terraform.tfstate`) contiene las contraseñas de la base.** Está en `.gitignore`: no subirlo nunca.

## Lo que la infraestructura ya tiene y el código todavía no usa

- **S3:** la app sigue guardando las imágenes en Postgres (`message_media`). Falta el cambio de código para usar el bucket (`MEDIA_BUCKET`), con su test de borrado por Ley 1581.
- **SES: ya conectado.** `notify_owner` envía el correo con `SES_SENDER` como remitente al `notify_config.email` del negocio (canal `email`). Mientras SES esté en modo sandbox, el destinatario también debe estar verificado (para la demo, el mismo `owner_email`).
