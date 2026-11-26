"""
Configuración centralizada de la aplicación.
Lee del .env y valida tipos/valores en startup.
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Variables de entorno tipadas. Lanzar excepción si falta algo crítico."""

    # --- Base de Datos ---
    database_url: str = Field(..., description="PostgreSQL connection string")
    database_pool_size: int = Field(default=10)
    database_max_overflow: int = Field(default=20)

    # --- Anthropic API ---
    anthropic_api_key: str = Field(..., description="API key de Anthropic")
    anthropic_model_classify: str = Field(
        default="claude-haiku-4-5",
        description="Modelo Haiku para clasificación de intención",
    )
    anthropic_model_respond: str = Field(
        default="claude-sonnet-5-5",
        description="Modelo Sonnet para componer respuestas",
    )

    # --- WhatsApp Cloud API (Meta) ---
    whatsapp_phone_number_id: str = Field(
        ..., description="Phone Number ID del número registrado en Meta"
    )
    whatsapp_waba_id: str = Field(..., description="WhatsApp Business Account ID")
    whatsapp_access_token: str = Field(
        ..., description="Access token permanente de System User (token de portador)"
    )
    whatsapp_app_secret: str = Field(
        ..., description="App secret para verificar firma HMAC del webhook"
    )
    whatsapp_verify_token: str = Field(
        ..., description="Verify token arbitrario configurado en Meta for Developers"
    )

    # --- FastAPI ---
    env: str = Field(default="development")
    debug: bool = Field(default=False)
    log_level: str = Field(default="INFO")
    api_base_url: str = Field(
        default="https://localhost:8000",
        description="URL pública para el callback del webhook",
    )

    # --- Ráfagas (invariante I2) ---
    turn_debounce_seconds: float = Field(
        default=10.0,
        ge=0,
        description=(
            "Espera antes de responder, por si el cliente sigue escribiendo: los "
            "mensajes que lleguen en ese lapso se responden juntos, una sola vez."
        ),
    )

    # --- Transcripción de audios (app/core/transcription.py) ---
    # "none" por ahora (decisión de Thomas, 2026-11-25): Amazon Transcribe no está
    # en el plan gratuito de AWS. Con "none", las notas de voz se responden con
    # el aviso fijo de audio no procesado. Para activar un proveedor: "amazon"
    # (requiere el plan pago de AWS) o uno nuevo registrado en transcription.py.
    transcription_provider: str = Field(
        default="none",
        description="Proveedor de transcripción: 'none' (desactivado) o 'amazon' (Transcribe).",
    )
    transcription_language: str = Field(
        default="es", description="Idioma de las notas de voz (código base: es, en, pt…)."
    )

    # --- AWS (avisos al asesor) ---
    aws_region: str = Field(default="us-east-1", description="Región de SES y S3")
    ses_sender: str | None = Field(
        default=None,
        description="Remitente verificado en SES. Sin esto, los avisos por correo no se envían.",
    )

    # --- General ---
    timezone: str = Field(default="America/Bogota")
    language: str = Field(default="es")

    # --- Observabilidad ---
    sentry_dsn: str | None = Field(default=None)
    enable_metrics: bool = Field(default=False)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


# Instancia global (singleton)
settings = Settings()
