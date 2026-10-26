import { fetchJson } from "./api";

export type ModelConnector = {
  connector_id: string;
  display_name: string;
  auth_mode: "subscription_cli" | "api_key";
  provider_name: string;
  model: string;
  adapter_available: boolean;
  credential_env: string | null;
  credential_present: boolean | null;
  cli_command: string | null;
  login_instructions: string;
  docs_url: string;
  availability_reason: string;
};
export type ModelConnections = {
  revision: number;
  default_connector_id: string;
  effective_connector_id: string;
  environment_override: string | null;
  execution_allowed: false;
  connectors: ModelConnector[];
};
export type ModelConnectionsUpdate = {
  expected_revision: number;
  default_connector_id: string;
  models: Record<string, string>;
};
const root = "/settings/model-connections";
export const getModelConnections = (signal?: AbortSignal) => fetchJson<ModelConnections>(root, { signal });
export const saveModelConnections = (body: ModelConnectionsUpdate) => fetchJson<ModelConnections>(root, { method: "PUT", body: JSON.stringify(body) });

export type ModelCredentialStatus = {
  connector_id: string;
  revision: number;
  storage_available: boolean;
  stored: boolean;
  environment_present: boolean;
  source: "stored" | "environment" | "none";
  execution_allowed: false;
};
function credentialStatus(value: unknown, connectorId: string): ModelCredentialStatus {
  if (!value || typeof value !== "object") throw new Error("invalid_credential_status");
  const data = value as Record<string, unknown>;
  if (data.connector_id !== connectorId || !Number.isSafeInteger(data.revision) || (data.revision as number) < 0
      || typeof data.storage_available !== "boolean" || typeof data.stored !== "boolean"
      || typeof data.environment_present !== "boolean" || data.execution_allowed !== false
      || data.source !== (data.stored ? "stored" : data.environment_present ? "environment" : "none")) {
    throw new Error("invalid_credential_status");
  }
  return { connector_id: connectorId, revision: data.revision as number, storage_available: data.storage_available,
    stored: data.stored, environment_present: data.environment_present, source: data.source as ModelCredentialStatus["source"], execution_allowed: false };
}
const credentialUrl = (connectorId: string) => `${root}/${encodeURIComponent(connectorId)}/credential`;
const credentialHeaders = { "Content-Type": "application/json", "X-XRML-Credential-Action": "1" };
export async function getModelCredential(connectorId: string, signal?: AbortSignal) {
  return credentialStatus(await fetchJson<unknown>(credentialUrl(connectorId), { signal }), connectorId);
}
export async function saveModelCredential(connectorId: string, expectedRevision: number, apiKey: string) {
  return credentialStatus(await fetchJson<unknown>(credentialUrl(connectorId), {
    method: "PUT", headers: credentialHeaders, body: JSON.stringify({ expected_revision: expectedRevision, api_key: apiKey }),
  }), connectorId);
}
export async function deleteModelCredential(connectorId: string, expectedRevision: number) {
  return credentialStatus(await fetchJson<unknown>(credentialUrl(connectorId), {
    method: "DELETE", headers: credentialHeaders, body: JSON.stringify({ expected_revision: expectedRevision }),
  }), connectorId);
}
