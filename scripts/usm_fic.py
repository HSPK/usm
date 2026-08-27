"""Azure Workload Identity inputs used by ``usm blobmount --auth fic``."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from usm_azure import SasError

FIC_CLIENT_ID_ENV = "AZURE_CLIENT_ID"
FIC_TENANT_ID_ENV = "AZURE_TENANT_ID"
FIC_TOKEN_FILE_ENV = "AZURE_FEDERATED_TOKEN_FILE"


@dataclass(frozen=True)
class WorkloadIdentity:
    client_id: str
    tenant_id: str
    token_file: str


def workload_identity_from_env(
    env: dict[str, str] | None = None,
) -> WorkloadIdentity:
    values = os.environ if env is None else env
    client_id = str(values.get(FIC_CLIENT_ID_ENV) or "").strip()
    tenant_id = str(values.get(FIC_TENANT_ID_ENV) or "").strip()
    token_file = str(values.get(FIC_TOKEN_FILE_ENV) or "").strip()
    missing = [
        name
        for name, value in (
            (FIC_CLIENT_ID_ENV, client_id),
            (FIC_TENANT_ID_ENV, tenant_id),
            (FIC_TOKEN_FILE_ENV, token_file),
        )
        if not value
    ]
    if missing:
        raise SasError(
            "fic auth requires Azure Workload Identity webhook variables: "
            + ", ".join(missing)
            + ". Set pod label azure.workload.identity/use=true and use an "
            "annotated ServiceAccount."
        )
    token_path = Path(token_file)
    if not token_path.is_file():
        raise SasError(f"fic token file does not exist or is not a file: {token_file}")
    if not os.access(token_path, os.R_OK):
        raise SasError(f"fic token file is not readable: {token_file}")
    return WorkloadIdentity(client_id, tenant_id, token_file)


__all__ = ["WorkloadIdentity", "workload_identity_from_env"]
