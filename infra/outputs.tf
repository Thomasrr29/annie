output "public_url" {
  description = "URL pública de annie."
  value       = "https://${local.domain}"
}

output "webhook_url" {
  description = "Callback URL que se pega en Meta for Developers → WhatsApp → Configuración."
  value       = "https://${local.domain}/webhooks/whatsapp"
}

output "panel_url" {
  description = "Panel de administración (Streamlit)."
  value       = "https://${local.domain}/panel"
}

output "whatsapp_verify_token" {
  description = "Verify token que se pega en Meta junto con la callback URL. Ver con: terraform output -raw whatsapp_verify_token"
  value       = random_password.verify_token.result
  sensitive   = true
}

output "instance_id" {
  description = "Id del servidor (para SSM: entrar o desplegar)."
  value       = aws_instance.server.id
}

output "public_ip" {
  value = aws_eip.this.public_ip
}

output "ecr_api_url" {
  value = aws_ecr_repository.api.repository_url
}

output "ecr_panel_url" {
  value = aws_ecr_repository.panel.repository_url
}

output "media_bucket" {
  value = aws_s3_bucket.media.bucket
}

output "region" {
  value = var.region
}

output "ssm_prefix" {
  value = local.ssm_prefix
}
