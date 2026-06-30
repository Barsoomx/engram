'use client';

import {
  addToast,
  Button,
  Input,
  Modal,
  ModalBody,
  ModalContent,
  ModalFooter,
  ModalHeader,
  Select,
  SelectItem,
} from '@heroui/react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import axios from 'axios';
import { AlertTriangle, Ban, Check, ChevronLeft, Clock, Link2, Plus } from 'lucide-react';
import Link from 'next/link';
import { useParams } from 'next/navigation';
import * as React from 'react';

import { ConfidenceTrack } from '@/components/ui/confidence-track';
import { KindBadge, KindDot } from '@/components/ui/kind-badge';
import { PrimaryButton } from '@/components/ui/primary-button';
import { apiClient, fetchMe, hasCapability, type MeResponse } from '@/lib/auth';
import {
  addMemoryLink,
  genRequestId,
  listMemoryLinks,
  recordMemoryFeedback,
  type MemoryFeedbackAction,
  type MemoryLink,
  type MemoryLinkType,
  type ScopeParams,
} from '@/lib/console-api';
import { KIND_STYLES, formatRelativeTime, resolveKind } from '@/lib/design';
import { useProjectStore } from '@/lib/project-store';
import { useTeamStore } from '@/lib/team-store';

type MemoryVersion = {
  version: number;
  body: string | null;
  created_at: string | null;
};

type RetrievalDocument = {
  id: string;
  memory_id: string;
  content_hash: string | null;
  token_count: number | null;
  created_at: string | null;
};

type BackendRelated = {
  id: string;
  title: string;
  link_type: string;
};

type MemoryDetail = {
  id: string;
  project_id: string;
  team_id: string | null;
  title: string;
  body: string;
  status: string;
  visibility_scope: string;
  current_version: number;
  confidence: string | null;
  confidence_percent?: number | null;
  stale: boolean;
  refuted: boolean;
  metadata: Record<string, unknown> | null;
  created_at: string | null;
  updated_at: string | null;
  versions: MemoryVersion[];
  retrieval_documents: RetrievalDocument[];
  kind?: string | null;
  tags?: string[];
  file_paths?: string[];
  captured_by?: unknown;
  project_name?: string;
  project_slug?: string;
  authorized_for_injection?: boolean;
  related?: BackendRelated[];
};

type RelatedItem = {
  key: string;
  title: string;
  mono: boolean;
};

const LINK_TYPES: MemoryLinkType[] = ['file', 'symbol', 'commit', 'issue'];

const LINK_TYPE_PILL: Record<MemoryLinkType, string> = {
  file: 'bg-[rgba(107,166,255,0.13)] text-info',
  symbol: 'bg-primary-soft text-primary-300',
  commit: 'bg-[rgba(61,217,172,0.13)] text-success',
  issue: 'bg-[rgba(242,183,101,0.14)] text-warning',
};

function linkTypeLabel(type: MemoryLinkType): string {
  return type[0].toUpperCase() + type.slice(1);
}

function extractDetail(error: unknown, fallback: string): string {
  if (axios.isAxiosError(error)) {
    const data = error.response?.data as { detail?: string } | undefined;

    if (data?.detail) {

      return data.detail;
    }
  }

  return fallback;
}

function metaString(meta: Record<string, unknown> | null, key: string): string | null {
  const value = meta?.[key];

  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

function parseConfidence(value: string | null): number | null {
  if (!value) {
    return null;
  }

  const parsed = Number.parseFloat(value);

  if (!Number.isFinite(parsed)) {
    return null;
  }

  const scaled = parsed <= 1 ? parsed * 100 : parsed;

  return Math.max(0, Math.min(100, Math.round(scaled)));
}

function buildRelated(data: MemoryDetail): RelatedItem[] {
  if (data.related && data.related.length > 0) {
    return data.related.map((item) => ({
      key: item.id,
      title: item.title,
      mono: false,
    }));
  }

  if (data.retrieval_documents.length > 0) {
    return data.retrieval_documents.map((doc) => ({
      key: doc.id,
      title: doc.content_hash ? doc.content_hash.slice(0, 16) : doc.id.slice(0, 8),
      mono: true,
    }));
  }

  return data.versions.map((version) => ({
    key: `v${version.version}`,
    title: `Version ${version.version}`,
    mono: false,
  }));
}

function BackLink() {
  return (
    <Link
      href='/memories'
      className='inline-flex items-center gap-1 text-[13px] font-medium text-default-500 transition-colors hover:text-foreground'
    >
      <ChevronLeft size={16} strokeWidth={2} />
      All memories
    </Link>
  );
}

function ProvenanceRow({
  label,
  value,
  mono,
  accent,
}: {
  label: string;
  value: string;
  mono?: boolean;
  accent?: boolean;
}) {
  return (
    <div className='flex items-baseline justify-between gap-4'>
      <span className='shrink-0 text-[12px] text-default-500'>{label}</span>
      <span
        className={`min-w-0 truncate text-right text-[12.5px] font-semibold ${
          accent ? 'text-primary-300' : 'text-foreground'
        }${mono ? ' font-mono text-[11.5px]' : ''}`}
      >
        {value}
      </span>
    </div>
  );
}

interface AddLinkInput {
  link_type: MemoryLinkType;
  target: string;
  label: string;
}

function AddLinkModal({
  isOpen,
  isPending,
  error,
  onClose,
  onSubmit,
}: {
  isOpen: boolean;
  isPending: boolean;
  error: string | null;
  onClose: () => void;
  onSubmit: (input: AddLinkInput) => Promise<boolean>;
}) {
  const [linkType, setLinkType] = React.useState<MemoryLinkType>('file');
  const [target, setTarget] = React.useState('');
  const [label, setLabel] = React.useState('');

  React.useEffect(() => {
    if (!isOpen) {
      setLinkType('file');
      setTarget('');
      setLabel('');
    }
  }, [isOpen]);

  const canSubmit = target.trim().length > 0 && !isPending;

  async function handleSubmit() {
    if (!canSubmit) {

      return;
    }

    const ok = await onSubmit({
      link_type: linkType,
      target: target.trim(),
      label: label.trim(),
    });

    if (ok) {
      onClose();
    }
  }

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      placement='center'
      isDismissable={!isPending}
      hideCloseButton={isPending}
    >
      <ModalContent>
        {() => (
          <>
            <ModalHeader className='flex flex-col gap-1 text-foreground'>
              Add link
            </ModalHeader>
            <ModalBody>
              <div className='space-y-4'>
                <Select
                  label='Type'
                  labelPlacement='outside'
                  placeholder='Select a link type'
                  selectedKeys={new Set([linkType])}
                  isDisabled={isPending}
                  onSelectionChange={(keys) => {
                    const next = Array.from(keys)[0];

                    if (typeof next === 'string') {
                      setLinkType(next as MemoryLinkType);
                    }
                  }}
                >
                  {LINK_TYPES.map((value) => (
                    <SelectItem key={value}>{linkTypeLabel(value)}</SelectItem>
                  ))}
                </Select>
                <Input
                  label='Target'
                  labelPlacement='outside'
                  placeholder='src/app/page.tsx · resolveKind · a1b2c3d · #142'
                  value={target}
                  onValueChange={setTarget}
                  maxLength={1024}
                  isDisabled={isPending}
                />
                <Input
                  label='Label'
                  labelPlacement='outside'
                  placeholder='Optional human-readable label'
                  value={label}
                  onValueChange={setLabel}
                  maxLength={255}
                  isDisabled={isPending}
                />
                {error && (
                  <div className='flex items-start gap-2.5 rounded-[12px] border border-danger/30 bg-danger/5 px-3.5 py-3'>
                    <AlertTriangle className='mt-0.5 h-4 w-4 shrink-0 text-danger' />
                    <p className='text-[13px] leading-relaxed text-danger'>{error}</p>
                  </div>
                )}
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
                color='primary'
                onPress={handleSubmit}
                isDisabled={!canSubmit}
                isLoading={isPending}
              >
                Add link
              </Button>
            </ModalFooter>
          </>
        )}
      </ModalContent>
    </Modal>
  );
}

function LinksCard({
  memoryId,
  scope,
  canReview,
}: {
  memoryId: string;
  scope: ScopeParams;
  canReview: boolean;
}) {
  const queryClient = useQueryClient();

  const linksQuery = useQuery<MemoryLink[]>({
    queryKey: ['memory-links', memoryId, scope.projectId, scope.teamId],
    enabled: Boolean(scope.projectId) && Boolean(memoryId),
    queryFn: async () => {
      try {

        return await listMemoryLinks(memoryId, scope);
      } catch (error) {
        if (axios.isAxiosError(error) && error.response?.status === 404) {

          return [];
        }

        throw error;
      }
    },
  });

  const [addOpen, setAddOpen] = React.useState(false);
  const [addError, setAddError] = React.useState<string | null>(null);

  const addMutation = useMutation({
    mutationFn: (input: AddLinkInput) =>
      addMemoryLink(memoryId, {
        project_id: scope.projectId,
        team_id: scope.teamId,
        link_type: input.link_type,
        target: input.target,
        label: input.label,
        request_id: genRequestId(),
      }),
  });

  async function handleAdd(input: AddLinkInput): Promise<boolean> {
    setAddError(null);

    try {
      await addMutation.mutateAsync(input);
      queryClient.invalidateQueries({ queryKey: ['memory-links', memoryId] });
      addToast({ title: 'Link added', color: 'success' });

      return true;
    } catch (error) {
      setAddError(extractDetail(error, 'Failed to add link.'));

      return false;
    }
  }

  function openAdd() {
    setAddError(null);
    setAddOpen(true);
  }

  const links = linksQuery.data ?? [];

  return (
    <div className='space-y-3'>
      <div className='flex items-center justify-between'>
        <h2 className='flex items-center gap-2 text-[14.5px] font-semibold text-foreground'>
          <Link2 size={15} strokeWidth={1.8} className='text-default-400' />
          Links
          {links.length > 0 && (
            <span className='tnum text-[12px] font-normal text-default-400'>{links.length}</span>
          )}
        </h2>
        {canReview && (
          <Button
            size='sm'
            variant='flat'
            startContent={<Plus className='h-3.5 w-3.5' />}
            onPress={openAdd}
          >
            Add link
          </Button>
        )}
      </div>

      {linksQuery.isLoading ? (
        <div className='space-y-2'>
          {Array.from({ length: 2 }).map((_, index) => (
            <div key={index} className='surface-card h-[46px] animate-pulse bg-content1' />
          ))}
        </div>
      ) : linksQuery.isError ? (
        <div className='flex items-start gap-2.5 rounded-[12px] border border-danger/30 bg-danger/5 px-3.5 py-3'>
          <AlertTriangle className='mt-0.5 h-4 w-4 shrink-0 text-danger' />
          <p className='text-[13px] leading-relaxed text-danger'>
            {linksQuery.error instanceof Error ? linksQuery.error.message : 'Failed to load links.'}
          </p>
        </div>
      ) : links.length === 0 ? (
        <p className='text-[13px] text-default-500'>No links yet.</p>
      ) : (
        <div className='space-y-2'>
          {links.map((link) => (
            <div
              key={link.link_id}
              className='surface-card flex items-center gap-3 px-4 py-3 transition-colors hover:bg-content2'
            >
              <span
                className={`shrink-0 rounded-[7px] px-2 py-0.5 text-[11px] font-medium ${LINK_TYPE_PILL[link.link_type]}`}
              >
                {linkTypeLabel(link.link_type)}
              </span>
              <span className='min-w-0 flex-1 truncate font-mono text-[12px] text-default-500'>
                {link.target}
              </span>
              {link.label && (
                <span className='max-w-[42%] shrink-0 truncate text-[12px] text-default-400'>
                  {link.label}
                </span>
              )}
            </div>
          ))}
        </div>
      )}

      <AddLinkModal
        isOpen={addOpen}
        isPending={addMutation.isPending}
        error={addError}
        onClose={() => setAddOpen(false)}
        onSubmit={handleAdd}
      />
    </div>
  );
}

function FeedbackActions({
  memoryId,
  scope,
  stale,
  refuted,
}: {
  memoryId: string;
  scope: ScopeParams;
  stale: boolean;
  refuted: boolean;
}) {
  const queryClient = useQueryClient();
  const [action, setAction] = React.useState<MemoryFeedbackAction | null>(null);
  const [reason, setReason] = React.useState('');
  const [error, setError] = React.useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: (payload: { action: MemoryFeedbackAction; reason: string }) =>
      recordMemoryFeedback(memoryId, {
        project_id: scope.projectId,
        team_id: scope.teamId,
        action: payload.action,
        reason: payload.reason,
        request_id: genRequestId(),
      }),
  });

  function open(next: MemoryFeedbackAction) {
    setAction(next);
    setReason('');
    setError(null);
  }

  function close() {
    if (mutation.isPending) {

      return;
    }

    setAction(null);
  }

  async function handleSubmit() {
    if (!action || reason.trim().length === 0 || mutation.isPending) {

      return;
    }

    setError(null);

    try {
      const result = await mutation.mutateAsync({ action, reason: reason.trim() });
      queryClient.invalidateQueries({ queryKey: ['inspection', 'memories'] });
      addToast({
        title: result.already_applied
          ? 'Already applied'
          : action === 'stale'
            ? 'Marked stale'
            : 'Marked refuted',
        color: 'success',
      });
      setAction(null);
    } catch (err) {
      setError(extractDetail(err, 'Failed to record feedback.'));
    }
  }

  return (
    <div className='surface-card space-y-3 p-[22px]'>
      <span className='block text-[10.5px] font-semibold uppercase tracking-[0.12em] text-default-400'>
        Review
      </span>
      <div className='grid grid-cols-2 gap-2'>
        <Button
          size='sm'
          variant='flat'
          color='warning'
          startContent={<Clock className='h-3.5 w-3.5' />}
          onPress={() => open('stale')}
          isDisabled={stale}
        >
          {stale ? 'Stale' : 'Mark stale'}
        </Button>
        <Button
          size='sm'
          variant='flat'
          color='danger'
          startContent={<Ban className='h-3.5 w-3.5' />}
          onPress={() => open('refuted')}
          isDisabled={refuted}
        >
          {refuted ? 'Refuted' : 'Mark refuted'}
        </Button>
      </div>

      <Modal
        isOpen={action !== null}
        onClose={close}
        placement='center'
        isDismissable={!mutation.isPending}
        hideCloseButton={mutation.isPending}
      >
        <ModalContent>
          {() => (
            <>
              <ModalHeader className='flex flex-col gap-1 text-foreground'>
                {action === 'stale' ? 'Mark memory stale' : 'Mark memory refuted'}
              </ModalHeader>
              <ModalBody>
                <div className='space-y-4'>
                  <p className='text-[13px] leading-relaxed text-default-500'>
                    {action === 'stale'
                      ? 'Flag this memory as out of date. It will be deprioritized for injection.'
                      : 'Flag this memory as refuted. It will be withheld from injection.'}
                  </p>
                  <Input
                    label='Reason'
                    labelPlacement='outside'
                    placeholder='Why is this memory no longer valid?'
                    value={reason}
                    onValueChange={setReason}
                    maxLength={1024}
                    isDisabled={mutation.isPending}
                    isRequired
                  />
                  {error && (
                    <div className='flex items-start gap-2.5 rounded-[12px] border border-danger/30 bg-danger/5 px-3.5 py-3'>
                      <AlertTriangle className='mt-0.5 h-4 w-4 shrink-0 text-danger' />
                      <p className='text-[13px] leading-relaxed text-danger'>{error}</p>
                    </div>
                  )}
                </div>
              </ModalBody>
              <ModalFooter>
                <Button
                  color='default'
                  variant='light'
                  onPress={close}
                  isDisabled={mutation.isPending}
                >
                  Cancel
                </Button>
                <Button
                  color={action === 'stale' ? 'warning' : 'danger'}
                  onPress={handleSubmit}
                  isDisabled={reason.trim().length === 0 || mutation.isPending}
                  isLoading={mutation.isPending}
                >
                  {action === 'stale' ? 'Mark stale' : 'Mark refuted'}
                </Button>
              </ModalFooter>
            </>
          )}
        </ModalContent>
      </Modal>
    </div>
  );
}

function MemoryDetailContent({
  data,
  scope,
  canReview,
}: {
  data: MemoryDetail;
  scope: ScopeParams;
  canReview: boolean;
}) {
  const meta = data.metadata;
  const kind = resolveKind(data.kind ?? metaString(meta, 'kind'));
  const source = data.file_paths?.[0] ?? metaString(meta, 'source') ?? '—';
  const capturedBy =
    (typeof data.captured_by === 'string' && data.captured_by.trim() ? data.captured_by.trim() : null) ??
    metaString(meta, 'captured_by') ??
    metaString(meta, 'agent') ??
    metaString(meta, 'author') ??
    '—';
  const projectName =
    data.project_name ?? data.project_slug ?? metaString(meta, 'project') ?? metaString(meta, 'project_slug') ?? data.project_id;
  const confidencePct = data.confidence_percent ?? parseConfidence(data.confidence);
  const authorized = data.authorized_for_injection ?? (data.status === 'active' && !data.refuted && !data.stale);
  const chip = authorized
    ? { text: 'Authorized for injection', color: 'text-success', bg: 'rgba(61,217,172,0.13)', icon: true }
    : data.refuted
      ? { text: 'Refuted', color: 'text-danger', bg: 'rgba(251,110,114,0.13)', icon: false }
      : data.stale
        ? { text: 'Stale', color: 'text-warning', bg: 'rgba(242,183,101,0.14)', icon: false }
        : { text: 'Not authorized', color: 'text-default-500', bg: 'rgba(255,255,255,0.05)', icon: false };
  const versionBody =
    data.versions.find((v) => v.version === data.current_version)?.body ??
    data.versions[0]?.body ??
    null;
  const showVersionBody = Boolean(
    versionBody && versionBody.trim() && versionBody.trim() !== data.body.trim(),
  );
  const related = buildRelated(data);

  return (
    <div className='grid grid-cols-1 gap-6 lg:grid-cols-[1.7fr_1fr]'>
      <div className='space-y-5'>
        <div className='space-y-3'>
          <div className='flex items-center gap-3'>
            <KindBadge kind={kind} />
            <span className='min-w-0 truncate font-mono text-[12px] text-default-500'>{source}</span>
          </div>
          <h1 className='text-[26px] font-semibold leading-[1.25] tracking-[-0.02em] text-foreground'>
            {data.title || '(untitled)'}
          </h1>
        </div>

        <div className='surface-card space-y-4 p-[22px]'>
          {data.body ? (
            <p className='whitespace-pre-wrap text-[15px] leading-[1.7] text-default-700'>{data.body}</p>
          ) : (
            <p className='text-[15px] leading-[1.7] text-default-500'>No body recorded.</p>
          )}
          {showVersionBody && versionBody && (
            <p className='whitespace-pre-wrap text-[14px] leading-[1.7] text-default-500'>{versionBody}</p>
          )}
        </div>

        {related.length > 0 && (
          <div className='space-y-3'>
            <h2 className='text-[14.5px] font-semibold text-foreground'>
              Related memories
              <span className='tnum ml-2 text-[12px] font-normal text-default-400'>{related.length}</span>
            </h2>
            <div className='space-y-2'>
              {related.map((item) => (
                <div
                  key={item.key}
                  className='surface-card flex items-center gap-3 px-4 py-3 transition-colors hover:bg-content2'
                >
                  <KindDot kind={kind} size={9} />
                  <span
                    className={`min-w-0 flex-1 truncate text-[13.5px] text-default-700${item.mono ? ' font-mono text-[12px]' : ''}`}
                  >
                    {item.title}
                  </span>
                  <span className='shrink-0 text-[11.5px] text-default-400'>{KIND_STYLES[kind].label}</span>
                </div>
              ))}
            </div>
          </div>
        )}

        <LinksCard memoryId={data.id} scope={scope} canReview={canReview} />
      </div>

      <div className='space-y-4'>
        <div className='surface-card space-y-3 p-[22px]'>
          <span className='block text-[10.5px] font-semibold uppercase tracking-[0.12em] text-default-400'>
            Provenance
          </span>
          <div className='space-y-3'>
            <ProvenanceRow label='Project' value={projectName} />
            <ProvenanceRow label='Captured by' value={capturedBy} />
            <ProvenanceRow label='Source' value={source} mono accent />
            <ProvenanceRow label='Scope' value={data.visibility_scope} />
            <ProvenanceRow label='Version' value={`v${data.current_version}`} mono />
            {data.team_id && <ProvenanceRow label='Team' value={data.team_id} mono />}
            <ProvenanceRow label='Updated' value={formatRelativeTime(data.updated_at)} />
          </div>
        </div>

        <div className='surface-card space-y-4 p-[22px]'>
          <div className='flex items-center justify-between'>
            <span className='text-[10.5px] font-semibold uppercase tracking-[0.12em] text-default-400'>
              Confidence
            </span>
            <span className='tnum text-[22px] font-semibold tracking-[-0.02em] text-foreground'>
              {confidencePct != null ? `${confidencePct}%` : '—'}
            </span>
          </div>
          <ConfidenceTrack value={confidencePct ?? 0} height={7} className='!w-full' />
          <span
            className={`inline-flex items-center gap-1.5 rounded-[7px] px-2.5 py-1 text-[12px] font-medium ${chip.color}`}
            style={{ backgroundColor: chip.bg }}
          >
            {chip.icon && <Check size={14} strokeWidth={2.5} />}
            {chip.text}
          </span>
        </div>

        {canReview && (
          <FeedbackActions
            memoryId={data.id}
            scope={scope}
            stale={data.stale}
            refuted={data.refuted}
          />
        )}

        <PrimaryButton className='w-full'>Add to context bundle</PrimaryButton>
      </div>
    </div>
  );
}

export default function MemoryDetailPage() {
  const params = useParams<{ id: string }>();
  const id = params?.id ?? '';

  const activeProjectId = useProjectStore((s) => s.activeProjectId);
  const activeTeamId = useTeamStore((s) => s.activeTeamId);

  const meQuery = useQuery<MeResponse>({
    queryKey: ['auth', 'me'],
    queryFn: fetchMe,
  });

  const capabilities = React.useMemo(
    () => meQuery.data?.capabilities ?? [],
    [meQuery.data?.capabilities],
  );

  const canReview = hasCapability(capabilities, 'memories:review');

  const scope = React.useMemo<ScopeParams>(
    () => ({ projectId: activeProjectId ?? '', teamId: activeTeamId }),
    [activeProjectId, activeTeamId],
  );

  const query = useQuery<MemoryDetail>({
    queryKey: ['inspection', 'memories', id, activeProjectId, activeTeamId],
    enabled: Boolean(activeProjectId) && Boolean(id),
    queryFn: async () => {
      const client = apiClient();
      const queryParams: Record<string, string> = { project_id: activeProjectId ?? '' };

      if (activeTeamId) {
        queryParams.team_id = activeTeamId;
      }

      const response = await client.get<MemoryDetail>(`/v1/inspection/memories/${id}/`, { params: queryParams });

      return response.data;
    },
  });

  if (!activeProjectId) {
    return (
      <section className='animate-fade-up space-y-6'>
        <BackLink />
        <div className='surface-card p-8'>
          <h1 className='text-[18px] font-semibold text-foreground'>Memory detail</h1>
          <p className='mt-2 text-[13.5px] text-default-500'>
            Select a project to view memory details.
          </p>
        </div>
      </section>
    );
  }

  return (
    <section className='animate-fade-up space-y-6'>
      <BackLink />

      {query.isLoading && <p className='text-[13.5px] text-default-500'>Loading memory…</p>}

      {query.isError && (
        <div className='surface-card p-5 text-[13.5px] text-danger'>
          {query.error instanceof Error ? query.error.message : 'Failed to load memory.'}
        </div>
      )}

      {query.data && (
        <MemoryDetailContent data={query.data} scope={scope} canReview={canReview} />
      )}
    </section>
  );
}
