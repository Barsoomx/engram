import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { describe, it } from 'node:test';

const require = createRequire(import.meta.url);
const { shouldClearTeamOnProjectChange } =
  require('./team-scope.ts') as typeof import('./team-scope');

describe('shouldClearTeamOnProjectChange', () => {
  it('clears the team when switching between two different projects', () => {
    assert.equal(shouldClearTeamOnProjectChange('project-a', 'project-b'), true);
  });

  it('keeps the team when re-selecting the same project', () => {
    assert.equal(shouldClearTeamOnProjectChange('project-a', 'project-a'), false);
  });

  it('keeps the team on initial bootstrap from no active project', () => {
    assert.equal(shouldClearTeamOnProjectChange(null, 'project-a'), false);
  });

  it('clears the team when the active project is cleared', () => {
    assert.equal(shouldClearTeamOnProjectChange('project-a', null), true);
  });
});
