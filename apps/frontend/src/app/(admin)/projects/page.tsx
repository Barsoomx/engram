'use client';

import {
  Button,
  Input,
  Modal,
  ModalBody,
  ModalContent,
  ModalFooter,
  ModalHeader,
} from '@heroui/react';
import { useQuery } from '@tanstack/react-query';
import { extractApiError } from '@/lib/api-error';
import {
  Archive,
  GitBranch,
  Pencil,
  Plus,
  ShieldCheck,
} from 'lucide-react';
import * as React from 'react';

import { ConfirmDialog } from '@/components/ui/confirm-dialog';
import { EmptyState } from '@/components/ui/empty-state';
import { CapabilityGate } from '@/components/ui/capability-gate';
import { PageHeader } from '@/components/ui/page-header';
import { PrimaryButton } from '@/components/ui/primary-button';
import {
  useArchiveProject,
  useCreateProject,
  useProjects,
  useUpdateProject,
} from '@/hooks/use-projects';
import { fetchMe, hasCapability, type MeResponse } from '@/lib/auth';
import type { Project, ProjectWriteInput } from '@/lib/admin-api';
import { formatRelativeTime } from '@/lib/design';
import { useOrgStore } from '@/lib/org-store';

function gridColumns(canAdmin: boolean): string {
  return canAdmin
    ? 'minmax(0,1.4fr) minmax(0,1fr) minmax(0,1.7fr) minmax(0,0.8fr) minmax(0,0.8fr) auto'
    : 'minmax(0,1.4fr) minmax(0,1fr) minmax(0,1.7fr) minmax(0,0.8fr) minmax(0,0.8fr)';
}

function ColumnHeader({ canAdmin }: { canAdmin: boolean }) {
  return (
    <div
      className='grid items-center gap-4 border-b border-divider px-5 py-3 text-[10.5px] font-semibold uppercase tracking-[0.1em] text-default-400'
      style={{ gridTemplateColumns: gridColumns(canAdmin) }}
    >
      <span>Project</span>
      <span>Slug</span>
      <span>Repository</span>
      <span>Memories</span>
      <span>Updated</span>
      {canAdmin && <span className='sr-only'>Actions</span>}
    </div>
  );
}

function ProjectsTable({
  items,
  canAdmin,
  onEdit,
  onArchive,
}: {
  items: Project[];
  canAdmin: boolean;
  onEdit: (project: Project) => void;
  onArchive: (project: Project) => void;
}) {
  return (
    <div className='surface-card overflow-hidden'>
      <div className='overflow-x-auto'>
        <div className='min-w-[680px]'>
          <ColumnHeader canAdmin={canAdmin} />
          {items.map((project) => (
            <div
              key={project.id}
              className='grid items-center gap-4 border-b border-divider px-5 py-3.5 transition-colors last:border-b-0 hover:bg-content2/60'
              style={{ gridTemplateColumns: gridColumns(canAdmin) }}
            >
              <div className='flex min-w-0 items-center gap-3'>
                <span className='inline-flex h-[30px] w-[30px] shrink-0 items-center justify-center rounded-[9px] bg-content3 text-primary-300'>
                  <GitBranch className='h-[15px] w-[15px]' strokeWidth={1.8} />
                </span>
                <span className='truncate text-[13.5px] font-semibold text-foreground'>
                  {project.name}
                </span>
              </div>
              <span className='truncate font-mono text-[12px] text-default-500'>
                {project.slug}
              </span>
              <span className='truncate font-mono text-[12px] text-default-500'>
                {project.repository_url || '—'}
              </span>
              <span className='tnum font-mono text-[12px] text-default-400'>
                {project.memory_count != null ? project.memory_count.toLocaleString() : '—'}
              </span>
              <span className='whitespace-nowrap text-[12px] text-default-400'>
                {formatRelativeTime(project.updated_at)}
              </span>
              {canAdmin && (
                <div className='flex items-center justify-end gap-2'>
                  <Button
                    size='sm'
                    variant='flat'
                    startContent={<Pencil className='w-3.5 h-3.5' />}
                    onPress={() => onEdit(project)}
                  >
                    Edit
                  </Button>
                  <Button
                    size='sm'
                    color='danger'
                    variant='flat'
                    startContent={<Archive className='w-3.5 h-3.5' />}
                    onPress={() => onArchive(project)}
                    isDisabled={project.archived_at !== null}
                  >
                    Archive
                  </Button>
                </div>
              )}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function ProjectsTableSkeleton({ canAdmin }: { canAdmin: boolean }) {
  return (
    <div className='surface-card overflow-hidden'>
      <div className='overflow-x-auto'>
        <div className='min-w-[680px]'>
          <ColumnHeader canAdmin={canAdmin} />
          {Array.from({ length: 6 }).map((_, index) => (
            <div
              key={index}
              className='grid items-center gap-4 border-b border-divider px-5 py-3.5 last:border-b-0'
              style={{ gridTemplateColumns: gridColumns(canAdmin) }}
            >
              <div className='flex min-w-0 items-center gap-3'>
                <span className='h-[30px] w-[30px] shrink-0 rounded-[9px] bg-content2' />
                <span className='h-3.5 w-28 rounded-medium bg-content2' />
              </div>
              <span className='h-3 w-20 rounded-medium bg-content2' />
              <span className='h-3 w-40 rounded-medium bg-content2' />
              <span className='h-3 w-8 rounded-medium bg-content2' />
              <span className='h-3 w-12 rounded-medium bg-content2' />
              {canAdmin && (
                <div className='flex items-center justify-end gap-2'>
                  <span className='h-8 w-14 rounded-medium bg-content2' />
                  <span className='h-8 w-20 rounded-medium bg-content2' />
                </div>
              )}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

type ProjectModalMode = 'create' | 'edit';

interface ProjectModalProps {
  isOpen: boolean;
  mode: ProjectModalMode;
  initialProject: Project | null;
  isPending: boolean;
  error: string | null;
  onClose: () => void;
  onSubmit: (input: ProjectWriteInput) => Promise<boolean>;
}

function ProjectModal({
  isOpen,
  mode,
  initialProject,
  isPending,
  error,
  onClose,
  onSubmit,
}: ProjectModalProps) {
  const [name, setName] = React.useState('');
  const [slug, setSlug] = React.useState('');
  const [repositoryUrl, setRepositoryUrl] = React.useState('');
  const [defaultBranch, setDefaultBranch] = React.useState('');

  React.useEffect(() => {
    if (!isOpen) {
      setName('');
      setSlug('');
      setRepositoryUrl('');
      setDefaultBranch('');

      return;
    }

    if (mode === 'edit' && initialProject) {
      setName(initialProject.name);
      setSlug(initialProject.slug);
      setRepositoryUrl(initialProject.repository_url);
      setDefaultBranch(initialProject.default_branch);
    } else {
      setName('');
      setSlug('');
      setRepositoryUrl('');
      setDefaultBranch('');
    }
  }, [isOpen, mode, initialProject]);

  const canSubmit =
    name.trim().length > 0 && slug.trim().length > 0 && !isPending;

  async function handleSubmit() {
    if (!canSubmit) {

      return;
    }

    const ok = await onSubmit({
      name: name.trim(),
      slug: slug.trim(),
      repository_url: repositoryUrl.trim(),
      default_branch: defaultBranch.trim(),
    });

    if (ok) {
      onClose();
    }
  }

  const title = mode === 'create' ? 'Create project' : 'Edit project';
  const confirmLabel = mode === 'create' ? 'Create' : 'Save';

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
              {title}
            </ModalHeader>
            <ModalBody>
              <div className='space-y-4'>
                <Input
                  label='Name'
                  labelPlacement='outside'
                  placeholder='Engram Core'
                  value={name}
                  onValueChange={setName}
                  maxLength={255}
                  isDisabled={isPending}
                />
                <Input
                  label='Slug'
                  labelPlacement='outside'
                  placeholder='engram-core'
                  value={slug}
                  onValueChange={setSlug}
                  description='Lowercase, unique within the organization.'
                  isDisabled={isPending}
                />
                <Input
                  label='Repository URL'
                  labelPlacement='outside'
                  placeholder='git@github.com:org/repo.git'
                  value={repositoryUrl}
                  onValueChange={setRepositoryUrl}
                  isDisabled={isPending}
                />
                <Input
                  label='Default branch'
                  labelPlacement='outside'
                  placeholder='main'
                  value={defaultBranch}
                  onValueChange={setDefaultBranch}
                  startContent={
                    <GitBranch className='w-3.5 h-3.5 text-default-500' />
                  }
                  isDisabled={isPending}
                />
                {error && (
                  <div className='rounded-medium bg-danger-50 dark:bg-danger-500/10 border border-danger-200 dark:border-danger-500/30 p-3'>
                    <p className='text-sm text-danger-600'>{error}</p>
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
                {confirmLabel}
              </Button>
            </ModalFooter>
          </>
        )}
      </ModalContent>
    </Modal>
  );
}

export default function ProjectsPage() {
  const activeOrgId = useOrgStore((state) => state.activeOrgId);
  const meQuery = useQuery<MeResponse>({
    queryKey: ['auth', 'me'],
    queryFn: fetchMe,
  });

  const capabilities = React.useMemo(
    () => meQuery.data?.capabilities ?? [],
    [meQuery.data?.capabilities],
  );

  const params = React.useMemo(() => ({ page: 1, pageSize: 50 }), []);
  const projectsQuery = useProjects(activeOrgId, params);

  const createMutation = useCreateProject(activeOrgId);
  const updateMutation = useUpdateProject(activeOrgId);
  const archiveMutation = useArchiveProject(activeOrgId);

  const [modalMode, setModalMode] = React.useState<ProjectModalMode>('create');
  const [modalOpen, setModalOpen] = React.useState(false);
  const [editTarget, setEditTarget] = React.useState<Project | null>(null);
  const [modalError, setModalError] = React.useState<string | null>(null);
  const [archiveTarget, setArchiveTarget] = React.useState<Project | null>(null);

  const canAdmin = hasCapability(capabilities, 'projects:admin');

  function openCreate() {
    setModalMode('create');
    setEditTarget(null);
    setModalError(null);
    setModalOpen(true);
  }

  function openEdit(project: Project) {
    setModalMode('edit');
    setEditTarget(project);
    setModalError(null);
    setModalOpen(true);
  }

  async function handleSubmit(input: ProjectWriteInput): Promise<boolean> {
    setModalError(null);

    try {
      if (modalMode === 'edit' && editTarget) {
        await updateMutation.mutateAsync({ id: editTarget.id, input });
      } else {
        await createMutation.mutateAsync(input);
      }

      return true;
    } catch (error) {
      setModalError(extractApiError(error, 'Failed to save project.'));

      return false;
    }
  }

  async function handleArchive() {
    if (!archiveTarget) {

      return;
    }

    try {
      await archiveMutation.mutateAsync(archiveTarget.id);
      setArchiveTarget(null);
    } catch {
      setArchiveTarget(null);
    }
  }

  const mutationPending =
    modalMode === 'edit'
      ? updateMutation.isPending
      : createMutation.isPending;

  const isLoading = meQuery.isLoading || projectsQuery.isLoading;
  const items = projectsQuery.data?.results ?? [];
  const meLoaded = meQuery.data !== undefined;

  return (
    <CapabilityGate capabilities={capabilities} required='projects:read'>
      <section className='space-y-6'>
        <PageHeader
          title='Projects'
          subtitle='Scopes for memory ingestion and retrieval.'
          actions={
            canAdmin ? (
              <PrimaryButton
                startContent={<Plus className='w-4 h-4' />}
                onPress={openCreate}
                isDisabled={!meLoaded}
              >
                New project
              </PrimaryButton>
            ) : null
          }
        />

        {isLoading ? (
          <ProjectsTableSkeleton canAdmin={canAdmin} />
        ) : items.length === 0 ? (
          <EmptyState
            title='No projects yet'
            description='Create a project to scope memory ingestion and retrieval within this organization.'
            icon={<GitBranch className='w-6 h-6' />}
            action={
              canAdmin ? (
                <PrimaryButton
                  startContent={<Plus className='w-4 h-4' />}
                  onPress={openCreate}
                >
                  New project
                </PrimaryButton>
              ) : undefined
            }
          />
        ) : (
          <ProjectsTable
            items={items}
            canAdmin={canAdmin}
            onEdit={openEdit}
            onArchive={setArchiveTarget}
          />
        )}

        {items.length > 0 && (
          <div className='flex items-center justify-between text-[12px] text-default-400'>
            <p>
              Showing {items.length} project{items.length === 1 ? '' : 's'}.
            </p>
            {canAdmin && (
              <p className='flex items-center gap-1.5'>
                <ShieldCheck className='w-3.5 h-3.5' />
                Archiving is reversible by an administrator.
              </p>
            )}
          </div>
        )}

        {projectsQuery.isError && (
          <pre className='rounded-[10px] border border-danger-500/30 bg-danger-500/10 p-3 text-sm text-danger-500'>
            {projectsQuery.error instanceof Error
              ? projectsQuery.error.message
              : 'Failed to load projects.'}
          </pre>
        )}

        <ProjectModal
          isOpen={modalOpen}
          mode={modalMode}
          initialProject={editTarget}
          isPending={mutationPending}
          error={modalError}
          onClose={() => setModalOpen(false)}
          onSubmit={handleSubmit}
        />

        <ConfirmDialog
          isOpen={archiveTarget !== null}
          title='Archive project'
          description={
            archiveTarget
              ? `Archive "${archiveTarget.name}" (${archiveTarget.slug})? Memory within this project will be retained but hidden from active views.`
              : undefined
          }
          confirmLabel='Archive'
          confirmColor='danger'
          isLoading={archiveMutation.isPending}
          onClose={() => setArchiveTarget(null)}
          onConfirm={handleArchive}
        />
      </section>
    </CapabilityGate>
  );
}
