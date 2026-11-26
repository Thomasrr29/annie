# ----------------------------------------------------------------------
# SES: correo con el que annie avisa al asesor que llegó un lead.
# AWS envía un correo de verificación a owner_email: hay que abrirlo y
# hacer clic. En modo sandbox, SES solo envía a direcciones verificadas
# (suficiente para la demo: el asesor es owner_email).
# ----------------------------------------------------------------------
resource "aws_ses_email_identity" "owner" {
  email = var.owner_email
}

# ----------------------------------------------------------------------
# AWS Budgets: alerta de gasto (falla típica 04 aplicada a la cuenta).
# include_credit = false: sin esto, los créditos "pagan" el consumo y el
# presupuesto muestra $0 mientras los créditos se agotan.
# ----------------------------------------------------------------------
resource "aws_budgets_budget" "monthly" {
  name         = "${local.name}-mensual"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  cost_types {
    include_credit = false
    include_refund = false
  }

  dynamic "notification" {
    for_each = [50, 80, 100]
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "PERCENTAGE"
      notification_type          = "ACTUAL"
      subscriber_email_addresses = [var.owner_email]
    }
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.owner_email]
  }
}
