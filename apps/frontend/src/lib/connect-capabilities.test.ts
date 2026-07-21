import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { describe, it } from 'node:test';

const require = createRequire(import.meta.url);
const { CONNECT_CAPABILITIES } = require('./connect-capabilities.ts') as typeof import('./connect-capabilities');

describe('CONNECT_CAPABILITIES', () => {
  it('grants the agent-key capabilities the connect flow issues', () => {
    assert.deepEqual(CONNECT_CAPABILITIES, [
      'memories:read',
      'memories:review',
      'memories:propose',
      'observations:write',
      'observations:read',
      'search:query',
      'audit:read',
      'projects:agent',
    ]);
  });

  it('includes the MCP-parity propose and audit capabilities', () => {
    assert.ok(CONNECT_CAPABILITIES.includes('memories:propose'));
    assert.ok(CONNECT_CAPABILITIES.includes('audit:read'));
  });
});
