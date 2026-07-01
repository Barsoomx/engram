function normalizeServerUrl(serverUrl: string): string {
  return serverUrl.replace(/\/$/, '');
}

export function buildConnectCommand({
  serverUrl,
  apiKey,
  projectId,
}: {
  serverUrl: string;
  apiKey: string;
  projectId: string;
}): string {
  return `uvx --from engram-connect engram install --server ${normalizeServerUrl(serverUrl)} --api-key ${apiKey} --project ${projectId}`;
}

export function buildConnectFallbackCommand({
  serverUrl,
  apiKey,
  projectId,
}: {
  serverUrl: string;
  apiKey: string;
  projectId: string;
}): string {
  return `engram connect --server ${normalizeServerUrl(serverUrl)} --api-key ${apiKey} --project ${projectId}`;
}

export const PLUGIN_INSTALL_COMMAND = 'claude plugin install engram@engram-marketplace';
