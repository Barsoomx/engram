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
import axios from 'axios';
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
import { TableRowSkeleton } from '@/components/ui/table-row-skeleton';
import {
  useArchiveProject,
  useCreateProject,
  useProjects,
  useUpdateProject,
} from '@/hooks/use-projects';
import { fetchMe, hasCapability, type MeResponse } from '@/lib/auth';
import type { Project, ProjectWriteInput } from '@/lib/admin-api';
import { useOrgStore } from '@/lib/org-store';

function formatDateTime(value: string | null): string {
  if (!value) {

    return '—';
  }

  try {

    return new Date(value).toLocaleString();
  } catch {

    return value;
  }
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
    <div className='overflow-x-auto'>
      <table className='w-full border-collapse text-left text-sm'>
        <thead>
          <tr className='border-b border-divider'>
            <th className='py-2 px-3 text-default-500 font-medium'>Name</th>
            <th className='py-2 px-3 text-default-500 font-medium'>Slug</th>
            <th className='py-2 px-3 text-default-500 font-medium'>
              Repository
            </th>
            <th className='py-2 px-3 text-default-500 font-medium'>
              Default branch
            </th>
            <th className='py-2 px-3 text-default-500 font-medium'>Created</th>
            {canAdmin && (
              <th className='py-2 px-3 text-default-500 font-medium text-right'>
                Actions
              </th>
            )}
          </tr>
        </thead>
        <tbody>
          {items.map((project) => (
            <tr key={project.id} className='border-b border-divider/50'>
              <td className='py-2 px-3 text-foreground'>{project.name}</td>
              <td className='py-2 px-3 font-mono text-xs text-default-700'>
                {project.slug}
              </td>
              <td className='py-2 px-3 font-mono text-xs text-default-700 break-all max-w-[20rem]'>
                {project.repository_url || '—'}
              </td>
              <td className='py-2 px-3 font-mono text-xs text-default-700 whitespace-nowrap'>
                {project.default_branch || '—'}
              </td>
              <td className='py-2 px-3 text-default-700 whitespace-nowrap'>
                {formatDateTime(project.created_at)}
              </td>
              {canAdmin && (
                <td className='py-2 px-3'>
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
                </td>
              )}
            </tr>
          ))}
        </tbody>
      </table>
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
      let detail: string | undefined;

      if (axios.isAxiosError(error)) {
        const data = error.response?.data as { detail?: string } | undefined;

        detail = data?.detail;
      }

      setModalError(detail ?? 'Failed to save project.');

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
          subtitle='Create, edit, and archive projects within this organization.'
          actions={
            canAdmin ? (
              <Button
                color='primary'
                startContent={<Plus className='w-4 h-4' />}
                onPress={openCreate}
                isDisabled={!meLoaded}
              >
                Create project
              </Button>
            ) : null
          }
        />

        <div className='surface-card p-2'>
          {isLoading ? (
            <table className='w-full border-collapse text-left text-sm'>
              <thead>
                <tr className='border-b border-divider'>
                  {Array.from({ length: canAdmin ? 6 : 5 }).map((_, index) => (
                    <th
                      key={index}
                      className='py-2 px-3 text-default-500 font-medium'
                    >
                      <span className='inline-block w-16 h-3 rounded-medium bg-content2/60' />
                    </th>
                  ))}
                </tr>
              </thead>
              <TableRowSkeleton columns={canAdmin ? 6 : 5} />
            </table>
          ) : items.length === 0 ? (
            <EmptyState
              title='No projects yet'
              description='Create a project to scope memory ingestion and retrieval within this organization.'
              icon={<GitBranch className='w-6 h-6' />}
              action={
                canAdmin ? (
                  <Button
                    color='primary'
                    startContent={<Plus className='w-4 h-4' />}
                    onPress={openCreate}
                  >
                    Create project
                  </Button>
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
        </div>

        {items.length > 0 && (
          <div className='flex items-center justify-between text-xs text-default-500'>
            <p>
              Showing {items.length} project{items.length === 1 ? '' : 's'}.
            </p>
            {canAdmin && (
              <p className='flex items-center gap-1'>
                <ShieldCheck className='w-3.5 h-3.5' />
                Archiving is reversible by an administrator.
              </p>
            )}
          </div>
        )}

        {projectsQuery.isError && (
          <pre className='text-sm text-danger-500 bg-danger-50 dark:bg-danger-500/10 rounded-medium p-3'>
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
