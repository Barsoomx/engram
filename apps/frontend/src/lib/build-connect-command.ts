function normalizeServerUrl(serverUrl: string): string {
  return serverUrl.replace(/\/$/, '');
}

export function buildConnectCommand({
  serverUrl,
  apiKey,
}: {
  serverUrl: string;
  apiKey: string;
}): string {
  return `uvx engram-connect install --server ${normalizeServerUrl(serverUrl)} --api-key ${apiKey}`;
}

export function buildConnectFallbackCommand({
  serverUrl,
  apiKey,
}: {
  serverUrl: string;
  apiKey: string;
}): string {
  return `engram connect --server ${normalizeServerUrl(serverUrl)} --api-key ${apiKey}`;
}

export const PLUGIN_INSTALL_COMMAND = 'claude plugin install engram@engram-marketplace';
