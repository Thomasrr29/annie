"""
Cliente HTTP del panel contra la API admin.

REGLA: el panel NUNCA importa `app.*` ni toca la base de datos. Habla solo por
HTTP contra `/admin/*` con el token del negocio. Asi el aislamiento multi-tenant
sigue siendo exactamente el de la API (token -> business_id -> RLS) y no se abre
una segunda puerta a los datos que haya que auditar aparte (RNF-04).
"""

from __future__ import annotations

import base64
import os
from typing import Any

import httpx

# El sandbox espera una respuesta del LLM: necesita mas margen que un CRUD.
TIMEOUT = httpx.Timeout(90.0, connect=10.0)


class ApiError(RuntimeError):
    """Error de la API admin, ya en texto mostrable al operador."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def base_url() -> str:
    """URL de la API. Configurable para apuntar a staging o produccion."""
    return os.getenv("PANEL_API_URL", "http://localhost:8000").rstrip("/")


def _detalle(resp: httpx.Response) -> str:
    """Convierte el cuerpo de error en un mensaje legible."""
    try:
        cuerpo = resp.json()
    except ValueError:
        return resp.text[:300] or f"HTTP {resp.status_code}"

    detalle = cuerpo.get("detail") if isinstance(cuerpo, dict) else None
    if isinstance(detalle, str):
        return detalle
    if isinstance(detalle, list):
        # 422 de Pydantic: lista de errores por campo.
        partes = [
            f"{'.'.join(str(x) for x in e.get('loc', [])[1:])}: {e.get('msg', '')}"
            for e in detalle
            if isinstance(e, dict)
        ]
        return "; ".join(p for p in partes if p.strip(": ")) or str(detalle)
    return str(cuerpo)[:300]


class AdminAPI:
    """Una llamada por metodo, con el token siempre puesto."""

    def __init__(self, token: str, url: str | None = None) -> None:
        self.token = token
        self.url = (url or base_url()).rstrip("/")

    # ------------------------------------------------------------------
    # Transporte
    # ------------------------------------------------------------------
    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            with httpx.Client(timeout=TIMEOUT) as client:
                resp = client.request(
                    method,
                    f"{self.url}/admin{path}",
                    headers={"Authorization": f"Bearer {self.token}"},
                    **kwargs,
                )
        except httpx.RequestError as exc:
            raise ApiError(0, f"No se pudo conectar con la API en {self.url}: {exc}") from exc

        if resp.status_code >= 400:
            raise ApiError(resp.status_code, _detalle(resp))
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    def _lista(self, method: str, path: str, **kwargs: Any) -> list[dict[str, Any]]:
        datos = self._request(method, path, **kwargs)
        return datos if isinstance(datos, list) else []

    def _objeto(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        datos = self._request(method, path, **kwargs)
        return datos if isinstance(datos, dict) else {}

    # ------------------------------------------------------------------
    # Negocio
    # ------------------------------------------------------------------
    def get_business(self) -> dict[str, Any]:
        return self._objeto("GET", "/business")

    def update_business(self, cambios: dict[str, Any]) -> dict[str, Any]:
        return self._objeto("PATCH", "/business", json=cambios)

    # ------------------------------------------------------------------
    # Agentes
    # ------------------------------------------------------------------
    def list_tools(self) -> list[str]:
        datos = self._request("GET", "/tools")
        return [str(t) for t in datos] if isinstance(datos, list) else []

    def list_agents(self, incluir_inactivos: bool = False) -> list[dict[str, Any]]:
        return self._lista(
            "GET", "/agents", params={"include_inactive": str(incluir_inactivos).lower()}
        )

    def create_agent(self, datos: dict[str, Any]) -> dict[str, Any]:
        return self._objeto("POST", "/agents", json=datos)

    def update_agent(self, agent_id: str, cambios: dict[str, Any]) -> dict[str, Any]:
        return self._objeto("PATCH", f"/agents/{agent_id}", json=cambios)

    # ------------------------------------------------------------------
    # Conocimiento
    # ------------------------------------------------------------------
    def list_knowledge(self) -> list[dict[str, Any]]:
        return self._lista("GET", "/knowledge")

    def create_knowledge(
        self, titulo: str, contenido: str, source: str, confirmado_publico: bool
    ) -> dict[str, Any]:
        return self._objeto(
            "POST",
            "/knowledge",
            json={
                "title": titulo,
                "raw_content": contenido,
                "source": source,
                "confirmed_public": confirmado_publico,
            },
        )

    def delete_knowledge(self, document_id: str) -> None:
        self._request("DELETE", f"/knowledge/{document_id}")

    # ------------------------------------------------------------------
    # Bandejas
    # ------------------------------------------------------------------
    def list_requests(self, estado: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if estado:
            params["status"] = estado
        return self._lista("GET", "/requests", params=params)

    def update_request_status(self, request_id: str, estado: str) -> dict[str, Any]:
        return self._objeto("PATCH", f"/requests/{request_id}", json={"status": estado})

    def list_escalations(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._lista("GET", "/escalations", params={"limit": limit})

    def resolve_escalation(self, escalation_id: str, volver_a_auto: bool) -> dict[str, Any]:
        return self._objeto(
            "POST",
            f"/escalations/{escalation_id}/resolve",
            params={"return_to_auto": str(volver_a_auto).lower()},
        )

    def human_reply(
        self, conversation_id: str, texto: str, volver_a_auto: bool
    ) -> dict[str, Any]:
        return self._objeto(
            "POST",
            f"/conversations/{conversation_id}/reply",
            json={"text": texto, "return_to_auto": volver_a_auto},
        )

    # ------------------------------------------------------------------
    # Conversaciones (bandeja)
    # ------------------------------------------------------------------
    def list_conversations(
        self, estado: str | None = None, busqueda: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if estado:
            params["status"] = estado
        if busqueda:
            params["search"] = busqueda
        return self._lista("GET", "/conversations", params=params)

    def get_conversation(
        self, conversation_id: str, despues_de_seq: int | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if despues_de_seq is not None:
            params["after_seq"] = despues_de_seq
        return self._objeto("GET", f"/conversations/{conversation_id}", params=params)

    def mark_read(self, conversation_id: str) -> None:
        self._request("POST", f"/conversations/{conversation_id}/read")

    def set_conversation_status(self, conversation_id: str, estado: str) -> dict[str, Any]:
        return self._objeto(
            "POST", f"/conversations/{conversation_id}/status", json={"status": estado}
        )

    def reply_image(
        self,
        conversation_id: str,
        datos: bytes,
        mime_type: str,
        pie: str = "",
        volver_a_auto: bool = False,
    ) -> dict[str, Any]:
        return self._objeto(
            "POST",
            f"/conversations/{conversation_id}/reply-image",
            json={
                "image_base64": base64.b64encode(datos).decode("ascii"),
                "mime_type": mime_type,
                "caption": pie,
                "return_to_auto": volver_a_auto,
            },
        )

    def get_media(self, media_id: str) -> bytes:
        """Bytes del archivo (no JSON): por eso no pasa por `_request`."""
        try:
            with httpx.Client(timeout=TIMEOUT) as client:
                resp = client.get(
                    f"{self.url}/admin/media/{media_id}",
                    headers={"Authorization": f"Bearer {self.token}"},
                )
        except httpx.RequestError as exc:
            raise ApiError(0, f"No se pudo conectar con la API en {self.url}: {exc}") from exc
        if resp.status_code >= 400:
            raise ApiError(resp.status_code, _detalle(resp))
        return resp.content

    # ------------------------------------------------------------------
    # Metricas
    # ------------------------------------------------------------------
    def metrics(self, dias: int = 7) -> dict[str, Any]:
        return self._objeto("GET", "/metrics", params={"days": dias})

    def cost_dashboard(self, dias: int = 7) -> dict[str, Any]:
        return self._objeto("GET", "/metrics/costs", params={"days": dias})

    # ------------------------------------------------------------------
    # Privacidad
    # ------------------------------------------------------------------
    def delete_customer_data(
        self, channel_identifier: str, solicitado_por: str | None
    ) -> dict[str, Any]:
        return self._objeto(
            "POST",
            "/privacy/delete-customer",
            json={"channel_identifier": channel_identifier, "requested_by": solicitado_por},
        )

    def deletion_log(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._lista("GET", "/privacy/deletion-log", params={"limit": limit})

    # ------------------------------------------------------------------
    # Sandbox
    # ------------------------------------------------------------------
    def sandbox_message(self, texto: str, identificador: str) -> dict[str, Any]:
        return self._objeto(
            "POST",
            "/sandbox/message",
            json={"text": texto, "customer_identifier": identificador},
        )


__all__ = ["AdminAPI", "ApiError", "base_url"]
