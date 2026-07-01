'use client';

import {
  addToast,
  Button,
  Checkbox,
  Chip,
  Dropdown,
  DropdownItem,
  DropdownMenu,
  DropdownTrigger,
  Input,
  Modal,
  ModalBody,
  ModalContent,
  ModalFooter,
  ModalHeader,
  Pagination,
  Select,
  SelectItem,
  Textarea,
} from '@heroui/react';
import { useQuery } from '@tanstack/react-query';
import axios from 'axios';
import {
  Archive,
  GitCompareArrows,
  MoreHorizontal,
  Pencil,
  ShieldCheck,
  Target,
  ThumbsDown,
  ThumbsUp,
} from 'lucide-react';
import * as React from 'react';

import { EmptyState } from '@/components/ui/empty-state';
import { CapabilityGate } from '@/components/ui/capability-gate';
import { PageHeader } from '@/components/ui/page-header';
import { TableRowSkeleton } from '@/components/ui/table-row-skeleton';
import {
  useBulkArchiveMemoryReview,
  useMemoryReview,
  useMemoryReviewAction,
  useMemoryReviewDiff,
} from '@/hooks/use-memory-review';
import { fetchMe, hasCapability, type MeResponse } from '@/lib/auth';
import type {
  MemoryReviewActionName,
  MemoryReviewActionPayload,
  MemoryReviewDiffSlice,
  MemoryReviewItem,
  MemoryReviewItemType,
} from '@/lib/admin-api';
import { useOrgStore } from '@/lib/org-store';

const VISIBILITY_SCOPES = ['global', 'team', 'project', 'user'] as const;

const STATUS_OPTIONS = ['proposed', 'conflict', 'refuted'] as const;

const SOURCE_TYPES = ['file', 'git', 'web', 'tool'] as const;

const PAGE_SIZE = 50;

const ACTION_REQUIRES_TARGET: ReadonlySet<MemoryReviewActionName> = new Set([
  'narrow',
  'supersede',
]);

const ACTION_REQUIRES_BODY: ReadonlySet<MemoryReviewActionName> = new Set([
  'edit',
]);

const ACTION_META: Record<
  MemoryReviewActionName,
  { label: string; color: 'success' | 'primary' | 'warning' | 'danger'; icon: typeof ThumbsUp }
> = {
  approve: { label: 'Approve', color: 'success', icon: ThumbsUp },
  edit: { label: 'Edit body', color: 'primary', icon: Pencil },
  narrow: { label: 'Narrow', color: 'primary', icon: Target },
  supersede: { label: 'Supersede', color: 'warning', icon: GitCompareArrows },
  reject: { label: 'Reject', color: 'danger', icon: ThumbsDown },
  archive: { label: 'Archive', color: 'danger', icon: Archive },
};

const ACTIONS_FOR_TYPE: Record<MemoryReviewItemType, MemoryReviewActionName[]> = {
  candidate: ['approve', 'edit', 'narrow', 'supersede', 'reject'],
  memory: ['archive', 'reject'],
};

function formatDateTime(value: string | null | undefined): string {
  if (!value) {

    return '—';
  }

  try {

    return new Date(value).toLocaleString();
  } catch {

    return value;
  }
}

function formatAgeDays(value: string): string {
  const createdAt = new Date(value).getTime();

  if (!Number.isFinite(createdAt)) {

    return '—';
  }

  const deltaMs = Date.now() - createdAt;

  if (deltaMs < 0) {

    return '0d';
  }

  const days = Math.floor(deltaMs / (1000 * 60 * 60 * 24));

  if (days >= 1) {

    return `${days}d`;
  }

  const hours = Math.floor(deltaMs / (1000 * 60 * 60));

  return `${hours}h`;
}

function confidenceColor(
  value: string | null,
): 'default' | 'success' | 'warning' | 'danger' {
  if (value === null) {

    return 'default';
  }

  const numeric = Number.parseFloat(value);

  if (!Number.isFinite(numeric)) {

    return 'default';
  }

  if (numeric >= 0.8) {

    return 'success';
  }

  if (numeric >= 0.4) {

    return 'warning';
  }

  return 'danger';
}

function extractErrorDetail(error: unknown): string {
  if (axios.isAxiosError(error)) {
    const data = error.response?.data as
      | { detail?: string; code?: string }
      | undefined;

    if (data?.detail) {

      return data.detail;
    }

    if (data?.code) {

      return data.code;
    }
  }

  if (error instanceof Error) {

    return error.message;
  }

  return 'Unexpected error.';
}

type FilterState = {
  search: string;
  team_id: string;
  visibility_scope: string;
  confidence_gte: string;
  confidence_lte: string;
  status: string;
  age_days_gte: string;
  source_type: string;
};

const EMPTY_FILTERS: FilterState = {
  search: '',
  team_id: '',
  visibility_scope: '',
  confidence_gte: '',
  confidence_lte: '',
  status: '',
  age_days_gte: '',
  source_type: '',
};

function buildParams(
  filters: FilterState,
  page: number,
): Record<string, string | number> {
  const params: Record<string, string | number> = { page, pageSize: PAGE_SIZE };

  if (filters.search.trim()) {
    params.search = filters.search.trim();
  }

  if (filters.team_id.trim()) {
    params.team_id = filters.team_id.trim();
  }

  if (filters.visibility_scope) {
    params.visibility_scope = filters.visibility_scope;
  }

  if (filters.confidence_gte.trim()) {
    params.confidence__gte = filters.confidence_gte.trim();
  }

  if (filters.confidence_lte.trim()) {
    params.confidence__lte = filters.confidence_lte.trim();
  }

  if (filters.status) {
    params.status = filters.status;
  }

  if (filters.age_days_gte.trim()) {
    const days = Number.parseInt(filters.age_days_gte.trim(), 10);

    if (Number.isFinite(days) && days > 0) {
      params.age_days__gte = days;
    }
  }

  if (filters.source_type) {
    params.source_type = filters.source_type;
  }

  return params;
}

function FilterBar({
  filters,
  onChange,
}: {
  filters: FilterState;
  onChange: (next: FilterState) => void;
}) {
  function update<K extends keyof FilterState>(key: K, value: FilterState[K]) {
    onChange({ ...filters, [key]: value });
  }

  return (
    <div className='surface-card p-4'>
      <div className='grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-4'>
        <Input
          label='Search'
          labelPlacement='outside'
          placeholder='Title or body…'
          value={filters.search}
          onValueChange={(value) => update('search', value)}
          isClearable
          onClear={() => update('search', '')}
        />
        <Input
          label='Team ID'
          labelPlacement='outside'
          placeholder='uuid'
          value={filters.team_id}
          onValueChange={(value) => update('team_id', value)}
          isClearable
          onClear={() => update('team_id', '')}
        />
        <Select
          label='Visibility scope'
          labelPlacement='outside'
          placeholder='Any'
          selectedKeys={
            filters.visibility_scope
              ? new Set([filters.visibility_scope])
              : new Set<string>()
          }
          onSelectionChange={(keys) => {
            const next = Array.from(keys)[0];

            update(
              'visibility_scope',
              typeof next === 'string' ? next : '',
            );
          }}
        >
          {VISIBILITY_SCOPES.map((scope) => (
            <SelectItem key={scope}>{scope}</SelectItem>
          ))}
        </Select>
        <Select
          label='Status'
          labelPlacement='outside'
          placeholder='Any'
          selectedKeys={
            filters.status ? new Set([filters.status]) : new Set<string>()
          }
          onSelectionChange={(keys) => {
            const next = Array.from(keys)[0];

            update('status', typeof next === 'string' ? next : '');
          }}
        >
          {STATUS_OPTIONS.map((status) => (
            <SelectItem key={status}>{status}</SelectItem>
          ))}
        </Select>
        <Input
          label='Confidence ≥'
          labelPlacement='outside'
          placeholder='0.0'
          type='number'
          min={0}
          max={1}
          step={0.05}
          value={filters.confidence_gte}
          onValueChange={(value) => update('confidence_gte', value)}
        />
        <Input
          label='Confidence ≤'
          labelPlacement='outside'
          placeholder='1.0'
          type='number'
          min={0}
          max={1}
          step={0.05}
          value={filters.confidence_lte}
          onValueChange={(value) => update('confidence_lte', value)}
        />
        <Input
          label='Age ≥ (days)'
          labelPlacement='outside'
          placeholder='7'
          type='number'
          min={0}
          step={1}
          value={filters.age_days_gte}
          onValueChange={(value) => update('age_days_gte', value)}
        />
        <Select
          label='Source type'
          labelPlacement='outside'
          placeholder='Any'
          selectedKeys={
            filters.source_type
              ? new Set([filters.source_type])
              : new Set<string>()
          }
          onSelectionChange={(keys) => {
            const next = Array.from(keys)[0];

            update('source_type', typeof next === 'string' ? next : '');
          }}
        >
          {SOURCE_TYPES.map((source) => (
            <SelectItem key={source}>{source}</SelectItem>
          ))}
        </Select>
      </div>
      <div className='mt-3 flex justify-end'>
        <Button
          size='sm'
          variant='light'
          onPress={() => onChange(EMPTY_FILTERS)}
          isDisabled={filters === EMPTY_FILTERS}
        >
          Reset filters
        </Button>
      </div>
    </div>
  );
}

function TypeChip({ item }: { item: MemoryReviewItem }) {
  return (
    <Chip size='sm' variant='flat' className='capitalize'>
      {item.type}
    </Chip>
  );
}

function SourceSummary({ item }: { item: MemoryReviewItem }) {
  const source = item.source_observation;

  if (!source) {

    return <span className='text-default-500'>—</span>;
  }

  const files = [
    ...(source.files_read ?? []),
    ...(source.files_modified ?? []),
  ].slice(0, 2);

  return (
    <div className='space-y-0.5'>
      {source.title ? (
        <p className='text-default-700 truncate max-w-[220px]'>{source.title}</p>
      ) : null}
      {files.length > 0 ? (
        <p className='text-xs text-default-500 font-mono truncate max-w-[220px]'>
          {files.join(', ')}
        </p>
      ) : null}
    </div>
  );
}

type RowAction =
  | { kind: 'view-diff' }
  | { kind: 'action'; action: MemoryReviewActionName };

interface ReviewTableProps {
  items: MemoryReviewItem[];
  selectedIds: Set<string>;
  canAdmin: boolean;
  onToggleRow: (id: string) => void;
  onToggleAll: (ids: string[]) => void;
  onRowAction: (item: MemoryReviewItem, action: RowAction) => void;
}

function ReviewTable({
  items,
  selectedIds,
  canAdmin,
  onToggleRow,
  onToggleAll,
  onRowAction,
}: ReviewTableProps) {
  const allIds = items.map((item) => item.id);
  const allSelected = allIds.length > 0 && allIds.every((id) => selectedIds.has(id));
  const someSelected = allIds.some((id) => selectedIds.has(id));

  return (
    <div className='overflow-x-auto'>
      <table className='w-full border-collapse text-left text-sm'>
        <thead>
          <tr className='border-b border-divider'>
            {canAdmin && (
              <th className='py-2 px-3 w-10'>
                <Checkbox
                  isSelected={allSelected}
                  isIndeterminate={!allSelected && someSelected}
                  onValueChange={() => onToggleAll(allIds)}
                  aria-label='Select all rows'
                />
              </th>
            )}
            <th className='py-2 px-3 text-default-500 font-medium'>Title</th>
            <th className='py-2 px-3 text-default-500 font-medium'>Type</th>
            <th className='py-2 px-3 text-default-500 font-medium'>Status</th>
            <th className='py-2 px-3 text-default-500 font-medium'>Confidence</th>
            <th className='py-2 px-3 text-default-500 font-medium'>Source</th>
            <th className='py-2 px-3 text-default-500 font-medium'>Age</th>
            <th className='py-2 px-3 text-default-500 font-medium text-right'>
              Actions
            </th>
          </tr>
        </thead>
        <tbody>
          {items.map((item) => {
            const isSelected = selectedIds.has(item.id);
            const canDiff = item.type === 'memory';

            return (
              <tr key={`${item.type}-${item.id}`} className='border-b border-divider/50'>
                {canAdmin && (
                  <td className='py-2 px-3'>
                    <Checkbox
                      isSelected={isSelected}
                      onValueChange={() => onToggleRow(item.id)}
                      aria-label={`Select ${item.title}`}
                    />
                  </td>
                )}
                <td className='py-2 px-3'>
                  <p className='text-foreground truncate max-w-[280px]'>
                    {item.title || 'Untitled'}
                  </p>
                  {item.body ? (
                    <p className='text-xs text-default-500 truncate max-w-[280px]'>
                      {item.body}
                    </p>
                  ) : null}
                </td>
                <td className='py-2 px-3'>
                  <TypeChip item={item} />
                </td>
                <td className='py-2 px-3'>
                  <Chip size='sm' variant='flat' className='capitalize'>
                    {item.status}
                  </Chip>
                </td>
                <td className='py-2 px-3'>
                  {item.confidence === null ? (
                    <span className='text-default-500'>—</span>
                  ) : (
                    <Chip
                      size='sm'
                      variant='flat'
                      color={confidenceColor(item.confidence)}
                    >
                      {item.confidence}
                    </Chip>
                  )}
                </td>
                <td className='py-2 px-3'>
                  <SourceSummary item={item} />
                </td>
                <td className='py-2 px-3 text-default-700 whitespace-nowrap'>
                  {formatAgeDays(item.created_at)}
                </td>
                <td className='py-2 px-3 text-right'>
                  <div className='flex items-center justify-end gap-2'>
                    {canDiff ? (
                      <Button
                        size='sm'
                        variant='flat'
                        startContent={<GitCompareArrows className='w-3.5 h-3.5' />}
                        onPress={() => onRowAction(item, { kind: 'view-diff' })}
                      >
                        Diff
                      </Button>
                    ) : null}
                    {canAdmin ? (
                      <Dropdown>
                        <DropdownTrigger>
                          <Button
                            size='sm'
                            variant='light'
                            isIconOnly
                            aria-label='Row actions'
                          >
                            <MoreHorizontal className='w-4 h-4' />
                          </Button>
                        </DropdownTrigger>
                        <DropdownMenu
                          aria-label='Memory review actions'
                          items={ACTIONS_FOR_TYPE[item.type].map((name) => ({
                            id: name,
                          }))}
                        >
                          {(entry) => {
                            const meta = ACTION_META[entry.id];
                            const Icon = meta.icon;

                            return (
                              <DropdownItem
                                key={entry.id}
                                color={meta.color}
                                startContent={<Icon className='w-3.5 h-3.5' />}
                                onAction={() =>
                                  onRowAction(item, {
                                    kind: 'action',
                                    action: entry.id,
                                  })
                                }
                              >
                                {meta.label}
                              </DropdownItem>
                            );
                          }}
                        </DropdownMenu>
                      </Dropdown>
                    ) : null}
                  </div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

interface ActionModalProps {
  isOpen: boolean;
  item: MemoryReviewItem | null;
  action: MemoryReviewActionName | null;
  isPending: boolean;
  error: string | null;
  onClose: () => void;
  onSubmit: (payload: MemoryReviewActionPayload) => Promise<boolean>;
}

function ActionModal({
  isOpen,
  item,
  action,
  isPending,
  error,
  onClose,
  onSubmit,
}: ActionModalProps) {
  const [reason, setReason] = React.useState('');
  const [body, setBody] = React.useState('');
  const [targetMemoryId, setTargetMemoryId] = React.useState('');

  React.useEffect(() => {
    if (!isOpen) {
      setReason('');
      setBody('');
      setTargetMemoryId('');

      return;
    }

    setBody(item?.body ?? '');
  }, [isOpen, item]);

  const requiresTarget =
    action !== null && ACTION_REQUIRES_TARGET.has(action);

  const requiresBody = action !== null && ACTION_REQUIRES_BODY.has(action);

  const meta = action ? ACTION_META[action] : null;

  const canSubmit =
    action !== null &&
    reason.trim().length > 0 &&
    (!requiresTarget || targetMemoryId.trim().length > 0) &&
    (!requiresBody || body.trim().length > 0) &&
    !isPending;

  async function handleSubmit() {
    if (!action || !canSubmit) {

      return;
    }

    const payload: MemoryReviewActionPayload = {
      action,
      reason: reason.trim(),
    };

    if (requiresBody) {
      payload.body = body.trim();
    }

    if (requiresTarget) {
      payload.target_memory_id = targetMemoryId.trim();
    }

    const ok = await onSubmit(payload);

    if (ok) {
      onClose();
    }
  }

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      placement='center'
      size='lg'
      isDismissable={!isPending}
      hideCloseButton={isPending}
    >
      <ModalContent>
        {() => (
          <>
            <ModalHeader className='flex flex-col gap-1 text-foreground'>
              {meta ? `${meta.label}: ${item?.title ?? ''}` : 'Action'}
            </ModalHeader>
            <ModalBody>
              <div className='space-y-4'>
                {item ? (
                  <div className='rounded-medium bg-content2/60 p-3 text-sm'>
                    <div className='flex items-center gap-2'>
                      <TypeChip item={item} />
                      <Chip size='sm' variant='flat' className='capitalize'>
                        {item.status}
                      </Chip>
                    </div>
                    <p className='mt-2 text-foreground font-medium truncate'>
                      {item.title || 'Untitled'}
                    </p>
                  </div>
                ) : null}
                {requiresBody ? (
                  <Textarea
                    label='New body'
                    labelPlacement='outside'
                    placeholder='Edited memory body…'
                    value={body}
                    onValueChange={setBody}
                    minRows={4}
                    maxRows={12}
                    maxLength={32768}
                    isDisabled={isPending}
                  />
                ) : null}
                {requiresTarget ? (
                  <Input
                    label='Target memory ID'
                    labelPlacement='outside'
                    placeholder='uuid'
                    value={targetMemoryId}
                    onValueChange={setTargetMemoryId}
                    isDisabled={isPending}
                  />
                ) : null}
                <Textarea
                  label='Reason'
                  labelPlacement='outside'
                  placeholder='Explain why this action is being taken…'
                  value={reason}
                  onValueChange={setReason}
                  minRows={2}
                  maxRows={6}
                  maxLength={1024}
                  isDisabled={isPending}
                  isRequired
                />
                {error ? (
                  <div className='rounded-medium bg-danger-50 dark:bg-danger-500/10 border border-danger-200 dark:border-danger-500/30 p-3'>
                    <p className='text-sm text-danger-600'>{error}</p>
                  </div>
                ) : null}
              </div>
            </ModalBody>
            <ModalFooter>
              <Button
                color='default'
                variant='light'
                onPress={onClose}
                isDisabled={isPending}
              >
                Cancel
              </Button>
              <Button
                color={meta ? meta.color : 'primary'}
                onPress={handleSubmit}
                isDisabled={!canSubmit}
                isLoading={isPending}
              >
                {meta ? meta.label : 'Apply'}
              </Button>
            </ModalFooter>
          </>
        )}
      </ModalContent>
    </Modal>
  );
}

function DiffVersionCard({
  title,
  slice,
}: {
  title: string;
  slice: MemoryReviewDiffSlice | undefined;
}) {
  return (
    <div className='rounded-medium border border-divider p-4 space-y-2'>
      <div className='flex items-center justify-between'>
        <p className='text-sm font-medium text-foreground'>{title}</p>
        {slice ? (
          <Chip size='sm' variant='flat'>
            v{slice.version}
          </Chip>
        ) : null}
      </div>
      {slice ? (
        <>
          <p className='text-xs text-default-500'>
            {formatDateTime(slice.created_at)}
          </p>
          <pre className='text-xs font-mono whitespace-pre-wrap break-words bg-content2/60 rounded-medium p-3 max-h-80 overflow-auto'>
            {slice.body}
          </pre>
        </>
      ) : (
        <p className='text-sm text-default-500'>Loading…</p>
      )}
    </div>
  );
}

function DiffModal({
  isOpen,
  item,
  fromVersion,
  toVersion,
  onClose,
}: {
  isOpen: boolean;
  item: MemoryReviewItem | null;
  fromVersion: number | null;
  toVersion: number | null;
  onClose: () => void;
}) {
  const activeOrgId = useOrgStore((state) => state.activeOrgId);
  const diffQuery = useMemoryReviewDiff(
    activeOrgId,
    isOpen && item ? item.id : null,
    isOpen ? fromVersion : null,
    isOpen ? toVersion : null,
    { enabled: isOpen && Boolean(item) },
  );

  const fromSlice = diffQuery.data?.from;
  const toSlice = diffQuery.data?.to;

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      placement='center'
      size='5xl'
      scrollBehavior='inside'
    >
      <ModalContent>
        {() => (
          <>
            <ModalHeader className='flex flex-col gap-1 text-foreground'>
              Version diff: {item?.title ?? ''}
            </ModalHeader>
            <ModalBody>
              <div className='grid grid-cols-1 gap-4 md:grid-cols-2'>
                <DiffVersionCard title='From' slice={fromSlice} />
                <DiffVersionCard title='To' slice={toSlice} />
              </div>
              {diffQuery.isError ? (
                <div className='rounded-medium bg-danger-50 dark:bg-danger-500/10 border border-danger-200 dark:border-danger-500/30 p-3'>
                  <p className='text-sm text-danger-600'>
                    {diffQuery.error instanceof Error
                      ? diffQuery.error.message
                      : 'Failed to load diff.'}
                  </p>
                </div>
              ) : null}
            </ModalBody>
            <ModalFooter>
              <Button color='primary' variant='light' onPress={onClose}>
                Close
              </Button>
            </ModalFooter>
          </>
        )}
      </ModalContent>
    </Modal>
  );
}

export default function MemoryReviewPage() {
  const activeOrgId = useOrgStore((state) => state.activeOrgId);
  const meQuery = useQuery<MeResponse>({
    queryKey: ['auth', 'me'],
    queryFn: fetchMe,
  });

  const capabilities = React.useMemo(
    () => meQuery.data?.capabilities ?? [],
    [meQuery.data?.capabilities],
  );

  const [filters, setFilters] = React.useState<FilterState>(EMPTY_FILTERS);
  const [debouncedFilters, setDebouncedFilters] =
    React.useState<FilterState>(EMPTY_FILTERS);
  const [page, setPage] = React.useState(1);

  const [selectedIds, setSelectedIds] = React.useState<Set<string>>(new Set());
  const [bulkReason, setBulkReason] = React.useState('');
  const [bulkOpen, setBulkOpen] = React.useState(false);

  const [actionItem, setActionItem] = React.useState<MemoryReviewItem | null>(
    null,
  );
  const [actionName, setActionName] =
    React.useState<MemoryReviewActionName | null>(null);
  const [actionOpen, setActionOpen] = React.useState(false);
  const [actionError, setActionError] = React.useState<string | null>(null);

  const [diffItem, setDiffItem] = React.useState<MemoryReviewItem | null>(null);
  const [diffFromVersion, setDiffFromVersion] = React.useState<number | null>(
    null,
  );
  const [diffToVersion, setDiffToVersion] = React.useState<number | null>(null);
  const [diffVersionInput, setDiffVersionInput] = React.useState<{
    from: string;
    to: string;
  }>({ from: '', to: '' });
  const [diffVersionPrompt, setDiffVersionPrompt] =
    React.useState<MemoryReviewItem | null>(null);

  React.useEffect(() => {
    const handle = window.setTimeout(() => {
      setDebouncedFilters(filters);
      setPage(1);
    }, 300);

    return () => {
      window.clearTimeout(handle);
    };
  }, [filters]);

  const queryParams = React.useMemo(
    () => buildParams(debouncedFilters, page),
    [debouncedFilters, page],
  );

  const reviewQuery = useMemoryReview(activeOrgId, queryParams);
  const actionMutation = useMemoryReviewAction(activeOrgId);
  const bulkMutation = useBulkArchiveMemoryReview(activeOrgId);

  const canAdmin = hasCapability(capabilities, 'memories:admin');

  const isLoading = meQuery.isLoading || reviewQuery.isLoading;
  const items = reviewQuery.data?.results ?? [];
  const totalCount = reviewQuery.data?.count ?? 0;
  const totalPages = Math.max(1, Math.ceil(totalCount / PAGE_SIZE));

  function handleToggleRow(id: string) {
    setSelectedIds((prev) => {
      const next = new Set(prev);

      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }

      return next;
    });
  }

  function handleToggleAll(ids: string[]) {
    setSelectedIds((prev) => {
      const allSelected =
        ids.length > 0 && ids.every((id) => prev.has(id));
      const next = new Set(prev);

      if (allSelected) {
        ids.forEach((id) => next.delete(id));
      } else {
        ids.forEach((id) => next.add(id));
      }

      return next;
    });
  }

  function openAction(item: MemoryReviewItem, action: MemoryReviewActionName) {
    setActionItem(item);
    setActionName(action);
    setActionError(null);
    setActionOpen(true);
  }

  function openDiffVersionsPrompt(item: MemoryReviewItem) {
    setDiffVersionPrompt(item);
    setDiffVersionInput({ from: '', to: '' });
  }

  function confirmDiffVersions() {
    if (!diffVersionPrompt) {

      return;
    }

    const from = Number.parseInt(diffVersionInput.from.trim(), 10);
    const to = Number.parseInt(diffVersionInput.to.trim(), 10);

    if (!Number.isFinite(from) || !Number.isFinite(to) || from <= 0 || to <= 0) {
      addToast({
        title: 'Invalid versions',
        description: 'Enter positive version numbers.',
        color: 'danger',
      });

      return;
    }

    setDiffItem(diffVersionPrompt);
    setDiffFromVersion(from);
    setDiffToVersion(to);
    setDiffVersionPrompt(null);
  }

  function handleRowAction(item: MemoryReviewItem, rowAction: RowAction) {
    if (rowAction.kind === 'view-diff') {
      openDiffVersionsPrompt(item);

      return;
    }

    openAction(item, rowAction.action);
  }

  async function handleActionSubmit(
    payload: MemoryReviewActionPayload,
  ): Promise<boolean> {
    setActionError(null);

    if (!actionItem || !payload.action) {

      return false;
    }

    try {
      const result = await actionMutation.mutateAsync({
        id: actionItem.id,
        payload,
      });

      addToast({
        title: `${ACTION_META[payload.action].label} applied`,
        description: result.memory_id
          ? `Memory ${result.memory_id}`
          : `Candidate ${result.candidate_id ?? actionItem.id}`,
        color: 'success',
      });

      setSelectedIds((prev) => {
        const next = new Set(prev);
        next.delete(actionItem.id);

        return next;
      });

      return true;
    } catch (error) {
      setActionError(extractErrorDetail(error));

      return false;
    }
  }

  function openBulkArchive() {
    setBulkReason('');
    setBulkOpen(true);
  }

  async function handleBulkArchive() {
    if (selectedIds.size === 0 || bulkReason.trim().length === 0) {

      return;
    }

    try {
      const result = await bulkMutation.mutateAsync({
        ids: Array.from(selectedIds),
        reason: bulkReason.trim(),
      });

      addToast({
        title: 'Memories archived',
        description: `${result.archived_count} item${result.archived_count === 1 ? '' : 's'} archived.`,
        color: 'success',
      });

      setSelectedIds(new Set());
      setBulkOpen(false);
    } catch (error) {
      addToast({
        title: 'Bulk archive failed',
        description: extractErrorDetail(error),
        color: 'danger',
      });
    }
  }

  const meLoaded = meQuery.data !== undefined;

  return (
    <CapabilityGate capabilities={capabilities} required='memories:review'>
      <section className='space-y-6'>
        <PageHeader
          title='Memory Review'
          subtitle='Curate proposed memories, resolve conflicts, and archive low-confidence noise.'
        />

        <FilterBar filters={filters} onChange={setFilters} />

        {selectedIds.size > 0 && canAdmin ? (
          <div className='surface-card flex flex-col gap-3 p-4 sm:flex-row sm:items-center sm:justify-between'>
            <div className='flex items-center gap-2 text-sm text-default-700'>
              <ShieldCheck className='w-4 h-4' />
              <span>
                {selectedIds.size} selected for bulk archive.
              </span>
            </div>
            <div className='flex items-center gap-2'>
              <Button
                color='default'
                variant='light'
                onPress={() => setSelectedIds(new Set())}
                isDisabled={bulkMutation.isPending}
              >
                Clear
              </Button>
              <Button
                color='danger'
                variant='flat'
                startContent={<Archive className='w-4 h-4' />}
                onPress={openBulkArchive}
                isDisabled={bulkMutation.isPending}
              >
                Archive selected
              </Button>
            </div>
          </div>
        ) : null}

        <div className='surface-card p-2'>
          {isLoading ? (
            <table className='w-full border-collapse text-left text-sm'>
              <thead>
                <tr className='border-b border-divider'>
                  {Array.from({
                    length: canAdmin ? 8 : 7,
                  }).map((_, index) => (
                    <th
                      key={index}
                      className='py-2 px-3 text-default-500 font-medium'
                    >
                      <span className='inline-block w-16 h-3 rounded-medium bg-content2/60' />
                    </th>
                  ))}
                </tr>
              </thead>
              <TableRowSkeleton columns={canAdmin ? 8 : 7} />
            </table>
          ) : items.length === 0 ? (
            <EmptyState
              title='No reviewable memories'
              description='Adjust filters or check back later. The queue surfaces proposed candidates, conflicts, and low-confidence memories.'
            />
          ) : (
            <ReviewTable
              items={items}
              selectedIds={selectedIds}
              canAdmin={canAdmin}
              onToggleRow={handleToggleRow}
              onToggleAll={handleToggleAll}
              onRowAction={handleRowAction}
            />
          )}
        </div>

        {totalCount > 0 ? (
          <div className='flex flex-col items-center gap-3'>
            <p className='text-xs text-default-500'>
              {totalCount} item{totalCount === 1 ? '' : 's'} · page {page} of{' '}
              {totalPages}
            </p>
            <Pagination
              total={totalPages}
              page={page}
              onChange={setPage}
              isDisabled={isLoading}
            />
          </div>
        ) : null}

        {reviewQuery.isError ? (
          <pre className='text-sm text-danger-500 bg-danger-50 dark:bg-danger-500/10 rounded-medium p-3'>
            {reviewQuery.error instanceof Error
              ? reviewQuery.error.message
              : 'Failed to load memory review queue.'}
          </pre>
        ) : null}

        <ActionModal
          isOpen={actionOpen}
          item={actionItem}
          action={actionName}
          isPending={actionMutation.isPending}
          error={actionError}
          onClose={() => setActionOpen(false)}
          onSubmit={handleActionSubmit}
        />

        <DiffModal
          isOpen={diffItem !== null}
          item={diffItem}
          fromVersion={diffFromVersion}
          toVersion={diffToVersion}
          onClose={() => {
            setDiffItem(null);
            setDiffFromVersion(null);
            setDiffToVersion(null);
          }}
        />

        <Modal
          isOpen={diffVersionPrompt !== null}
          onClose={() => setDiffVersionPrompt(null)}
          placement='center'
        >
          <ModalContent>
            {() => (
              <>
                <ModalHeader className='flex flex-col gap-1 text-foreground'>
                  Compare versions
                </ModalHeader>
                <ModalBody>
                  <div className='space-y-4'>
                    <p className='text-sm text-default-500'>
                      Enter two version numbers to compare for{' '}
                      <span className='text-foreground font-medium'>
                        {diffVersionPrompt?.title ?? ''}
                      </span>
                      .
                    </p>
                    <div className='grid grid-cols-2 gap-3'>
                      <Input
                        label='From version'
                        labelPlacement='outside'
                        placeholder='1'
                        type='number'
                        min={1}
                        value={diffVersionInput.from}
                        onValueChange={(value) =>
                          setDiffVersionInput((prev) => ({ ...prev, from: value }))
                        }
                      />
                      <Input
                        label='To version'
                        labelPlacement='outside'
                        placeholder='2'
                        type='number'
                        min={1}
                        value={diffVersionInput.to}
                        onValueChange={(value) =>
                          setDiffVersionInput((prev) => ({ ...prev, to: value }))
                        }
                      />
                    </div>
                  </div>
                </ModalBody>
                <ModalFooter>
                  <Button
                    color='default'
                    variant='light'
                    onPress={() => setDiffVersionPrompt(null)}
                  >
                    Cancel
                  </Button>
                  <Button
                    color='primary'
                    onPress={confirmDiffVersions}
                    isDisabled={
                      diffVersionInput.from.trim().length === 0 ||
                      diffVersionInput.to.trim().length === 0
                    }
                  >
                    Compare
                  </Button>
                </ModalFooter>
              </>
            )}
          </ModalContent>
        </Modal>

        <Modal
          isOpen={bulkOpen}
          onClose={() => setBulkOpen(false)}
          placement='center'
          isDismissable={!bulkMutation.isPending}
          hideCloseButton={bulkMutation.isPending}
        >
          <ModalContent>
            {() => (
              <>
                <ModalHeader className='flex flex-col gap-1 text-foreground'>
                  Archive {selectedIds.size} item
                  {selectedIds.size === 1 ? '' : 's'}
                </ModalHeader>
                <ModalBody>
                  <div className='space-y-3'>
                    <p className='text-sm text-default-500'>
                      Provide a reason. This is recorded in the audit log for
                      each archived memory and the action is permanent.
                    </p>
                    <Textarea
                      label='Reason'
                      labelPlacement='outside'
                      placeholder='Low-confidence duplicate memories…'
                      value={bulkReason}
                      onValueChange={setBulkReason}
                      minRows={2}
                      maxRows={6}
                      maxLength={1024}
                      isDisabled={bulkMutation.isPending}
                      isRequired
                    />
                  </div>
                </ModalBody>
                <ModalFooter>
                  <Button
                    color='default'
                    variant='light'
                    onPress={() => setBulkOpen(false)}
                    isDisabled={bulkMutation.isPending}
                  >
                    Cancel
                  </Button>
                  <Button
                    color='danger'
                    startContent={<Archive className='w-4 h-4' />}
                    onPress={handleBulkArchive}
                    isDisabled={
                      bulkReason.trim().length === 0 || bulkMutation.isPending
                    }
                    isLoading={bulkMutation.isPending}
                  >
                    Confirm archive
                  </Button>
                </ModalFooter>
              </>
            )}
          </ModalContent>
        </Modal>
      </section>
    </CapabilityGate>
  );
}
