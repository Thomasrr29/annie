variable "project" {
  description = "Nombre corto del proyecto: prefijo de recursos y de los parámetros en SSM (/<project>/...)."
  type        = string
  default     = "annie"
}

variable "region" {
  description = "Región de AWS. us-east-1 es la más barata; la latencia no importa (webhook + LLM)."
  type        = string
  default     = "us-east-1"
}

variable "instance_type" {
  description = "Tipo de EC2. t3.small (x86, 2 GB) alcanza para API + Postgres + Caddy + panel en la demo."
  type        = string
  default     = "t3.small"
}

variable "root_volume_gb" {
  description = "Disco del servidor. Aquí viven Docker y los datos de Postgres."
  type        = number
  default     = 30
}

variable "domain" {
  description = <<-EOT
    Dominio público para HTTPS (debe apuntar a la IP elástica). Vacío = se usa
    <ip-con-guiones>.sslip.io, que resuelve solo a la IP y no requiere comprar dominio.
  EOT
  type        = string
  default     = ""
}

variable "owner_email" {
  description = "Correo del dueño de la cuenta: recibe las alertas de presupuesto y es remitente/destinatario de SES en modo sandbox."
  type        = string
}

variable "monthly_budget_usd" {
  description = "Presupuesto mensual de AWS. Se avisa al 50%, 80% y 100% (falla típica 04: gasto descontrolado)."
  type        = number
  default     = 20
}

variable "media_retention_days" {
  description = "Días que se guardan las imágenes de los clientes en S3 antes de borrarse solas (política de retención, Ley 1581)."
  type        = number
  default     = 90
}
