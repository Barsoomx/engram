import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { describe, it } from 'node:test';

const require = createRequire(import.meta.url);
const {
  AGENT_TARGET_OPTIONS,
  buildConnectCommand,
  buildConnectFallbackCommand,
  buildManualInstallCommands,
  validateConnectServerUrl,
} = require('./build-connect-command.ts') as typeof import('./build-connect-command');

describe('buildConnectCommand', () => {
  it('includes the selected runtime and normalizes a trailing slash', () => {
    assert.equal(
      buildConnectCommand({
        agent: 'codex',
        serverUrl: 'https://engram.example.com/',
        apiKey: 'egk_test-key',
      }),
      'uvx engram-connect install --agent codex --server https://engram.example.com --api-key egk_test-key',
    );
  });

  it('supports every runtime exposed by the selector', () => {
    for (const { value } of AGENT_TARGET_OPTIONS) {
      assert.match(
        buildConnectCommand({
          agent: value,
          serverUrl: 'http://localhost:8000',
          apiKey: 'egk_test',
        }),
        new RegExp(`--agent ${value}(?: |$)`),
      );
    }
  });

  it('percent-encodes shell metacharacters in a URL path', () => {
    const command = buildConnectCommand({
      agent: 'claude-code',
      serverUrl: 'https://engram.example.com/$(touch bad)',
      apiKey: 'egk_test',
    });

    assert.equal(
      command,
      'uvx engram-connect install --agent claude-code --server https://engram.example.com/%24%28touch%20bad%29 --api-key egk_test',
    );
    assert.doesNotMatch(command, /\$\(/);
  });

  it('rejects an API key that cannot be pasted as one shell argument', () => {
    assert.throws(
      () =>
        buildConnectCommand({
          agent: 'claude-code',
          serverUrl: 'https://engram.example.com',
          apiKey: 'egk_test; whoami',
        }),
      /API key contains unsupported characters/,
    );
  });
});

describe('buildConnectFallbackCommand', () => {
  it('keeps the selected runtime explicit', () => {
    assert.equal(
      buildConnectFallbackCommand({
        agent: 'both',
        serverUrl: 'http://localhost:8000/',
        apiKey: 'egk_test',
      }),
      'uvx engram-connect connect --agent both --server http://localhost:8000 --api-key egk_test',
    );
  });
});

describe('manual install guidance', () => {
  it('uses the existing Claude Code marketplace commands', () => {
    assert.deepEqual(buildManualInstallCommands('claude-code'), [
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
    ]);
  });

  it('uses Codex native marketplace and plugin commands', () => {
    assert.deepEqual(buildManualInstallCommands('codex'), [
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
    ]);
  });

  it('returns both runtimes in installation order', () => {
    assert.deepEqual(
      buildManualInstallCommands('both').map(({ id }) => id),
      [
        'claude-marketplace',
        'claude-plugin',
        'codex-marketplace',
        'codex-plugin',
      ],
    );
  });
});

describe('validateConnectServerUrl', () => {
  it('accepts HTTP and HTTPS base URLs', () => {
    assert.equal(validateConnectServerUrl('http://localhost:8000'), null);
    assert.equal(validateConnectServerUrl('https://engram.example.com/api/'), null);
  });

  it('rejects credentials, query strings, fragments, and other protocols', () => {
    for (const serverUrl of [
      'ftp://engram.example.com',
      'https://user:password@engram.example.com',
      'https://engram.example.com?tenant=one',
      'https://engram.example.com#fragment',
    ]) {
      assert.notEqual(validateConnectServerUrl(serverUrl), null);
    }
  });
});
