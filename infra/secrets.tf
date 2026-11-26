# ----------------------------------------------------------------------
# Secretos en SSM Parameter Store (SecureString, gratis en el nivel estándar).
#
# Terraform genera SOLO los secretos que no vienen de afuera: contraseñas de la
# base y el verify token del webhook (un texto arbitrario que inventamos).
#
# Los secretos de terceros (API key de Anthropic, tokens de Meta) NO pasan por
# Terraform, para que no queden en el estado: se cargan a mano con
# `aws ssm put-parameter` (ver infra/README.md).
#
# En el servidor, refresh.sh lee todo /<project>/* y arma el .env.
# ----------------------------------------------------------------------
resource "random_password" "postgres" {
  length  = 32
  special = false # va dentro de una URL de conexión
}

resource "random_password" "app_db" {
  length  = 32
  special = false
}

resource "random_password" "verify_token" {
  length  = 40
  special = false
}

resource "aws_ssm_parameter" "postgres_password" {
  name  = "${local.ssm_prefix}/POSTGRES_PASSWORD"
  type  = "SecureString"
  value = random_password.postgres.result
}

resource "aws_ssm_parameter" "app_db_password" {
  name  = "${local.ssm_prefix}/APP_DB_PASSWORD"
  type  = "SecureString"
  value = random_password.app_db.result
}

resource "aws_ssm_parameter" "verify_token" {
  name  = "${local.ssm_prefix}/WHATSAPP_VERIFY_TOKEN"
  type  = "SecureString"
  value = random_password.verify_token.result
}
