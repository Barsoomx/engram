export type AgentTarget = 'claude-code' | 'codex' | 'both';

export const AGENT_TARGET_OPTIONS: ReadonlyArray<{
  value: AgentTarget;
  label: string;
  description: string;
}> = [
  {
    value: 'claude-code',
    label: 'Claude Code',
    description: 'Install the native Claude Code plugin.',
  },
  {
    value: 'codex',
    label: 'Codex',
    description: 'Install the native Codex plugin and MCP tools.',
  },
  {
    value: 'both',
    label: 'Both',
    description: 'Connect Claude Code and Codex with the same Engram key.',
  },
];

export interface ManualInstallCommand {
  id: string;
  label: string;
  command: string;
}

const CLAUDE_MANUAL_INSTALL_COMMANDS: ReadonlyArray<ManualInstallCommand> = [
  {
    id: 'claude-marketplace',
    label: 'Add Claude Code marketplace',
    command: 'claude plugin marketplace add Barsoomx/engram',
  },
  {
    id: 'claude-plugin',
    label: 'Install Claude Code plugin',
    command: 'claude plugin install engram@engram-marketplace',
  },
];

const CODEX_MANUAL_INSTALL_COMMANDS: ReadonlyArray<ManualInstallCommand> = [
  {
    id: 'codex-marketplace',
    label: 'Add Codex marketplace',
    command: 'codex plugin marketplace add Barsoomx/engram --json',
  },
  {
    id: 'codex-plugin',
    label: 'Install Codex plugin',
    command: 'codex plugin add engram@engram-marketplace --json',
  },
];

function normalizeServerUrl(serverUrl: string): string {
  const value = serverUrl.trim();
  const parsed = new URL(value);

  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    throw new Error('Server URL must start with http:// or https://.');
  }

  if (parsed.username || parsed.password) {
    throw new Error('Server URL must not include credentials.');
  }

  if (parsed.search || parsed.hash) {
    throw new Error('Server URL must not include a query string or fragment.');
  }

  parsed.pathname = parsed.pathname.replace(/\/+$/, '');

  return Array.from(parsed.toString().replace(/\/$/, ''))
    .map((character) =>
      /^[A-Za-z0-9]$/.test(character) || '._~:/%@+-[]'.includes(character)
        ? character
        : encodeURIComponent(character).replace(/[!'()*]/g, (reserved) =>
            `%${reserved.charCodeAt(0).toString(16).toUpperCase()}`,
          ),
    )
    .join('');
}

function validateApiKey(apiKey: string): string {
  if (!/^[A-Za-z0-9_-]+$/.test(apiKey)) {
    throw new Error('API key contains unsupported characters.');
  }

  return apiKey;
}

export function validateConnectServerUrl(serverUrl: string): string | null {
  try {
    normalizeServerUrl(serverUrl);

    return null;
  } catch (error) {
    return error instanceof Error ? error.message : 'Enter a valid server URL.';
  }
}

export function buildManualInstallCommands(
  agent: AgentTarget,
): ManualInstallCommand[] {
  if (agent === 'claude-code') {
    return [...CLAUDE_MANUAL_INSTALL_COMMANDS];
  }

  if (agent === 'codex') {
    return [...CODEX_MANUAL_INSTALL_COMMANDS];
  }

  return [
    ...CLAUDE_MANUAL_INSTALL_COMMANDS,
    ...CODEX_MANUAL_INSTALL_COMMANDS,
  ];
}

export function buildConnectCommand({
  agent,
  serverUrl,
  apiKey,
}: {
  agent: AgentTarget;
  serverUrl: string;
  apiKey: string;
}): string {
  return `uvx engram-connect install --agent ${agent} --server ${normalizeServerUrl(serverUrl)} --api-key ${validateApiKey(apiKey)}`;
}

export function buildConnectFallbackCommand({
  agent,
  serverUrl,
  apiKey,
}: {
  agent: AgentTarget;
  serverUrl: string;
  apiKey: string;
}): string {
  return `uvx engram-connect connect --agent ${agent} --server ${normalizeServerUrl(serverUrl)} --api-key ${validateApiKey(apiKey)}`;
}
