import type {
  ConflictResolutionAction,
  ConflictResolvePayload,
} from './admin-api';

export const CONFLICT_RESOLUTION_ACTIONS: readonly ConflictResolutionAction[] = [
  'publish_candidate',
  'merge_candidate',
  'supersede_memory',
  'reject_candidate',
];

const TARGET_ACTIONS: ReadonlySet<ConflictResolutionAction> = new Set([
  'merge_candidate',
  'supersede_memory',
]);

const MERGE_ACTION: ConflictResolutionAction = 'merge_candidate';

export const MERGED_TITLE_MAX_LENGTH = 255;

export const MERGED_BODY_MAX_LENGTH = 32768;

export const REASON_MAX_LENGTH = 1024;

export type ConflictResolveForm = {
  action: ConflictResolutionAction;
  reason: string;
  targetMemoryId: string;
  mergedTitle: string;
  mergedBody: string;
};

export function actionRequiresTarget(action: ConflictResolutionAction): boolean {
  return TARGET_ACTIONS.has(action);
}

export function actionAllowsMergedText(action: ConflictResolutionAction): boolean {
  return action === MERGE_ACTION;
}

export function isConflictResolveFormValid(form: ConflictResolveForm): boolean {
  const reason = form.reason.trim();

  if (reason.length === 0 || reason.length > REASON_MAX_LENGTH) {
    return false;
  }

  if (actionRequiresTarget(form.action) && form.targetMemoryId.trim().length === 0) {
    return false;
  }

  if (actionAllowsMergedText(form.action)) {
    if (form.mergedTitle.trim().length > MERGED_TITLE_MAX_LENGTH) {
      return false;
    }

    if (form.mergedBody.trim().length > MERGED_BODY_MAX_LENGTH) {
      return false;
    }
  }

  return true;
}

export function buildConflictResolvePayload(form: ConflictResolveForm): ConflictResolvePayload {
  const payload: ConflictResolvePayload = {
    action: form.action,
    reason: form.reason.trim(),
  };

  if (actionRequiresTarget(form.action)) {
    payload.target_memory_id = form.targetMemoryId.trim();
  }

  if (actionAllowsMergedText(form.action)) {
    const mergedTitle = form.mergedTitle.trim();
    const mergedBody = form.mergedBody.trim();

    if (mergedTitle.length > 0) {
      payload.merged_title = mergedTitle;
    }

    if (mergedBody.length > 0) {
      payload.merged_body = mergedBody;
    }
  }

  return payload;
}

export function isStaleConflictStatus(status: number | undefined): boolean {
  return status === 412;
}

export function isPreconditionRequiredStatus(status: number | undefined): boolean {
  return status === 428;
}

export function isConflictGoneStatus(status: number | undefined): boolean {
  return status === 404;
}

export function cursorFromNextUrl(next: string | null): string | null {
  if (!next) {
    return null;
  }

  try {
    return new URL(next).searchParams.get('cursor');
  } catch {
    return null;
  }
}
