"""Microsoft Entra token providers for Purview."""

from __future__ import annotations

from typing import Any, Mapping

from ...contracts import TokenProvider

PURVIEW_SCOPE = "https://purview.azure.net/.default"


class AzureIdentityTokenProvider:
    """Adapter from Azure Identity credentials to the utility contract."""

    def __init__(self, credential: Any) -> None:
        self._credential = credential

    def get_token(self, scope: str) -> str:
        from azure.core.exceptions import ClientAuthenticationError

        try:
            return self._credential.get_token(scope).token
        except ClientAuthenticationError as exc:
            raise RuntimeError(
                "Microsoft Entra authentication failed. Configure managed "
                "identity, workload identity, service-principal environment "
                "variables, or authenticate Azure CLI for local development."
            ) from exc


def create_token_provider(config: Mapping[str, Any]) -> TokenProvider:
    """Create a passwordless Azure Identity credential chain."""
    try:
        from azure.identity import (
            AzureCliCredential,
            DefaultAzureCredential,
            ManagedIdentityCredential,
        )
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Purview publishing requires the 'purview' extra. Install with: "
            "pip install 'lineage-utility[purview]'"
        ) from exc

    auth_type = str(config.get("type") or "default").casefold()
    client_id = config.get("managed_identity_client_id")
    if client_id is not None and not isinstance(client_id, str):
        raise ValueError("managed_identity_client_id must be a string.")

    if auth_type == "default":
        credential = DefaultAzureCredential(
            managed_identity_client_id=client_id,
            exclude_interactive_browser_credential=True,
        )
    elif auth_type == "managed_identity":
        credential = ManagedIdentityCredential(client_id=client_id)
    elif auth_type == "azure_cli":
        tenant_id = config.get("tenant_id")
        if tenant_id is not None and not isinstance(tenant_id, str):
            raise ValueError("Azure CLI tenant_id must be a string.")
        credential = AzureCliCredential(tenant_id=tenant_id)
    else:
        raise ValueError(
            "Purview auth type must be default, managed_identity, or azure_cli."
        )
    return AzureIdentityTokenProvider(credential)
