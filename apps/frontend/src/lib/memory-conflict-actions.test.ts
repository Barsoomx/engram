import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import type { MemoryReviewItem } from './admin-api';
import {
  actionRequiresTarget,
  buildConflictResolvePayload,
  cursorFromNextUrl,
  isConflictResolveFormValid,
  isPreconditionRequiredStatus,
  isStaleConflictStatus,
  type ConflictResolveForm,
} from './memory-conflict-actions';

const backendConflictListItem = {
  id: '11111111-1111-4111-8111-111111111111',
  type: 'conflict',
  state: 'open',
  conflict_ids: ['22222222-2222-4222-8222-222222222222'],
  project_id: '33333333-3333-4333-8333-333333333333',
  team_id: null,
  visibility_scope: 'project',
  reason_code: 'same_scope_contradiction',
  opened_at: '2026-07-17T00:00:00Z',
  candidate_claim: {
    title: 'Candidate claim',
    kind: 'decision',
    body_hash: '0'.repeat(64),
  },
  existing_claims: [
    {
      memory_id: '44444444-4444-4444-8444-444444444444',
      version_id: '55555555-5555-4555-8555-555555555555',
      title: 'Existing claim',
      kind: 'decision',
      body_hash: '1'.repeat(64),
    },
  ],
} satisfies MemoryReviewItem;

function baseForm(overrides: Partial<ConflictResolveForm> = {}): ConflictResolveForm {
  return {
    action: 'publish_candidate',
    reason: 'operator decision',
    targetMemoryId: '',
    mergedTitle: '',
    mergedBody: '',
    ...overrides,
  };
}

describe('CP5 memory-review list contract', () => {
  it('accepts the backend conflict discriminant', () => {
    assert.equal(backendConflictListItem.type, 'conflict');
    assert.equal(backendConflictListItem.existing_claims[0].version_id.length, 36);
  });
});

describe('conflict resolve payloads', () => {
  it('omits target and merged text for publish_candidate', () => {
    const payload = buildConflictResolvePayload(baseForm());

    assert.deepEqual(payload, {
      action: 'publish_candidate',
      reason: 'operator decision',
    });
  });

  it('marks merge and supersede as target actions', () => {
    assert.equal(actionRequiresTarget('merge_candidate'), true);
    assert.equal(actionRequiresTarget('supersede_memory'), true);
    assert.equal(actionRequiresTarget('publish_candidate'), false);
    assert.equal(actionRequiresTarget('reject_candidate'), false);
  });

  it('carries target and merged text for merge_candidate', () => {
    const payload = buildConflictResolvePayload(
      baseForm({
        action: 'merge_candidate',
        targetMemoryId: '44444444-4444-4444-8444-444444444444',
        mergedTitle: 'Merged title',
        mergedBody: 'Merged body',
      }),
    );

    assert.deepEqual(payload, {
      action: 'merge_candidate',
      reason: 'operator decision',
      target_memory_id: '44444444-4444-4444-8444-444444444444',
      merged_title: 'Merged title',
      merged_body: 'Merged body',
    });
  });

  it('carries only the target for supersede_memory', () => {
    const payload = buildConflictResolvePayload(
      baseForm({
        action: 'supersede_memory',
        targetMemoryId: '44444444-4444-4444-8444-444444444444',
        mergedTitle: 'ignored title',
      }),
    );

    assert.deepEqual(payload, {
      action: 'supersede_memory',
      reason: 'operator decision',
      target_memory_id: '44444444-4444-4444-8444-444444444444',
    });
  });

  it('rejects a merge without a target', () => {
    assert.equal(isConflictResolveFormValid(baseForm({ action: 'merge_candidate' })), false);
  });

  it('rejects an empty reason', () => {
    assert.equal(isConflictResolveFormValid(baseForm({ reason: '   ' })), false);
  });

  it('accepts a valid publish form', () => {
    assert.equal(isConflictResolveFormValid(baseForm()), true);
  });
});

describe('conflict etag preconditions', () => {
  it('detects a stale conflict set', () => {
    assert.equal(isStaleConflictStatus(412), true);
    assert.equal(isStaleConflictStatus(200), false);
  });

  it('detects a missing precondition', () => {
    assert.equal(isPreconditionRequiredStatus(428), true);
    assert.equal(isPreconditionRequiredStatus(412), false);
  });
});

describe('conflict list cursor', () => {
  it('reads the cursor from a next url', () => {
    assert.equal(
      cursorFromNextUrl('https://api.example.com/v1/admin/memory-review/?cursor=abc123'),
      'abc123',
    );
  });

  it('returns null when there is no next page', () => {
    assert.equal(cursorFromNextUrl(null), null);
  });
});
