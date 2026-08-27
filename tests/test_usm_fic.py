"""Focused tests for the blobmount-only Workload Identity adapter."""

from __future__ import annotations

import pytest

from usm_azure import SasError
from usm_fic import workload_identity_from_env


def test_workload_identity_reads_projected_token_contract(tmp_path, monkeypatch):
    token = tmp_path / "token"
    token.write_text("projected-jwt")
    monkeypatch.setenv("AZURE_CLIENT_ID", "client-id")
    monkeypatch.setenv("AZURE_TENANT_ID", "tenant-id")
    monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", str(token))

    identity = workload_identity_from_env()

    assert identity.client_id == "client-id"
    assert identity.tenant_id == "tenant-id"
    assert identity.token_file == str(token)


@pytest.mark.parametrize(
    "missing",
    [
        "AZURE_CLIENT_ID",
        "AZURE_TENANT_ID",
        "AZURE_FEDERATED_TOKEN_FILE",
    ],
)
def test_workload_identity_requires_every_webhook_input(tmp_path, monkeypatch, missing):
    token = tmp_path / "token"
    token.write_text("projected-jwt")
    values = {
        "AZURE_CLIENT_ID": "client-id",
        "AZURE_TENANT_ID": "tenant-id",
        "AZURE_FEDERATED_TOKEN_FILE": str(token),
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv(missing)

    with pytest.raises(SasError, match=missing):
        workload_identity_from_env()


def test_workload_identity_rejects_missing_token_file(tmp_path, monkeypatch):
    monkeypatch.setenv("AZURE_CLIENT_ID", "client-id")
    monkeypatch.setenv("AZURE_TENANT_ID", "tenant-id")
    monkeypatch.setenv(
        "AZURE_FEDERATED_TOKEN_FILE",
        str(tmp_path / "missing"),
    )

    with pytest.raises(SasError, match="does not exist"):
        workload_identity_from_env()
