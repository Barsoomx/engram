'use client';

import {
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
import { useQuery } from '@tanstack/react-query';
import axios from 'axios';
import {
  ArrowRight,
  Check,
  ChevronDown,
  ChevronRight,
  Copy,
  ShieldAlert,
} from 'lucide-react';
import NextLink from 'next/link';
import * as React from 'react';

import { useIssueApiKey } from '@/hooks/use-api-keys';
import { listProjects, type Paginated, type Project } from '@/lib/admin-api';
import { fetchMe, hasCapability, type MeResponse } from '@/lib/auth';
import {
  buildConnectCommand,
  buildConnectFallbackCommand,
  PLUGIN_INSTALL_COMMAND,
} from '@/lib/build-connect-command';
import { useOrgStore } from '@/lib/org-store';
import { useProjectStore } from '@/lib/project-store';
import { adminQueryKeys } from '@/lib/query-keys';

const CONNECT_CAPABILITIES = [
  'memories:read',
  'observations:write',
  'search:query',
];

function defaultServerUrl(): string {
  return (
    process.env.NEXT_PUBLIC_ENGRAM_API_URL ||
    (typeof window !== 'undefined' ? window.location.origin : '')
  );
}

interface IssuedCommand {
  command: string;
  fallbackCommand: string;
  keyPrefix: string;
  keyFingerprint: string;
}

interface ConnectAgentModalProps {
  isOpen: boolean;
  onClose: () => void;
}

export function ConnectAgentModal({ isOpen, onClose }: ConnectAgentModalProps) {
  const activeOrgId = useOrgStore((state) => state.activeOrgId);
  const activeProjectId = useProjectStore((state) => state.activeProjectId);

  const meQuery = useQuery<MeResponse>({
    queryKey: ['auth', 'me'],
    queryFn: fetchMe,
  });

  const projectsParams = React.useMemo(() => ({ page: 1, pageSize: 100 }), []);
  const projectsQuery = useQuery<Paginated<Project>>({
    queryKey: adminQueryKeys.projects(activeOrgId, projectsParams),
    queryFn: () => listProjects(projectsParams),
    enabled: Boolean(activeOrgId) && isOpen,
  });

  const issueMutation = useIssueApiKey(activeOrgId);

  const [serverUrl, setServerUrl] = React.useState<string>(defaultServerUrl);
  const [selectedProjectId, setSelectedProjectId] = React.useState<string | null>(null);
  const [issued, setIssued] = React.useState<IssuedCommand | null>(null);
  const [issueError, setIssueError] = React.useState<string | null>(null);
  const [fallbackOpen, setFallbackOpen] = React.useState(false);
  const [copied, setCopied] = React.useState<string | null>(null);

  React.useEffect(() => {
    if (!isOpen) {
      setServerUrl(defaultServerUrl());
      setSelectedProjectId(null);
      setIssued(null);
      setIssueError(null);
      setFallbackOpen(false);
      setCopied(null);
    }
  }, [isOpen]);

  const capabilities = React.useMemo(
    () => meQuery.data?.capabilities ?? [],
    [meQuery.data?.capabilities],
  );
  const canIssue = hasCapability(capabilities, 'api_keys:issue');
  const meLoaded = meQuery.data !== undefined;

  const projects = React.useMemo(
    () => projectsQuery.data?.results ?? [],
    [projectsQuery.data?.results],
  );
  const effectiveProjectId =
    selectedProjectId ?? activeProjectId ?? projects[0]?.id ?? null;
  const selectedProject = projects.find(
    (project) => project.id === effectiveProjectId,
  );

  async function copyText(id: string, text: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(id);
    } catch {
      setCopied(null);
    }
  }

  async function handleGenerate() {
    if (!effectiveProjectId) {

      return;
    }

    setIssueError(null);

    const projectSlug = selectedProject?.slug ?? effectiveProjectId;

    try {
      const result = await issueMutation.mutateAsync({
        name: `claude-code · ${projectSlug}`,
        capabilities: CONNECT_CAPABILITIES,
      });

      setIssued({
        command: buildConnectCommand({
          serverUrl,
          apiKey: result.plaintext,
          projectId: effectiveProjectId,
        }),
        fallbackCommand: buildConnectFallbackCommand({
          serverUrl,
          apiKey: result.plaintext,
          projectId: effectiveProjectId,
        }),
        keyPrefix: result.key_prefix,
        keyFingerprint: result.key_fingerprint,
      });
    } catch (error) {
      let detail: string | undefined;

      if (axios.isAxiosError(error)) {
        const data = error.response?.data as { detail?: string } | undefined;

        detail = data?.detail;
      }

      setIssueError(detail ?? 'Failed to issue API key.');
    }
  }

  const isIssuing = issueMutation.isPending;
  const canGenerate =
    canIssue && Boolean(effectiveProjectId) && serverUrl.trim().length > 0 && !isIssuing;

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      placement='center'
      size='lg'
      scrollBehavior='inside'
      isDismissable={!isIssuing}
      isKeyboardDismissDisabled={isIssuing}
      hideCloseButton={isIssuing}
    >
      <ModalContent>
        {() => (
          <>
            <ModalHeader className='flex flex-col gap-1 text-foreground'>
              {issued ? 'Connect agent' : 'Connect a Claude Code agent'}
            </ModalHeader>
            <ModalBody>
              {meQuery.isError ? (
                <p className='text-sm text-danger-600'>
                  Could not load your permissions. Close and try again.
                </p>
              ) : !meLoaded ? (
                <p className='text-sm text-default-500'>Loading…</p>
              ) : !canIssue ? (
                <div className='space-y-3'>
                  <p className='text-sm text-default-500'>
                    You do not have permission to issue API keys. Ask an
                    organization admin to grant{' '}
                    <span className='font-mono text-xs'>api_keys:issue</span>, or
                    manage keys from the API Keys page.
                  </p>
                  <NextLink
                    href='/api-keys'
                    onClick={onClose}
                    className='inline-flex items-center gap-1 text-sm font-medium text-primary-300 transition-colors hover:text-foreground'
                  >
                    Go to API Keys
                    <ArrowRight className='w-4 h-4' />
                  </NextLink>
                </div>
              ) : issued ? (
                <div className='space-y-4'>
                  <div className='flex items-start gap-3 rounded-medium bg-warning-50 dark:bg-warning-500/10 border border-warning-200 dark:border-warning-500/30 p-3'>
                    <ShieldAlert className='w-5 h-5 text-warning-600 shrink-0 mt-0.5' />
                    <div className='space-y-1'>
                      <p className='text-sm font-medium text-warning-700 dark:text-warning-300'>
                        Copy it now. You will not see this again.
                      </p>
                      <p className='text-xs text-default-500'>
                        Engram stores only a hashed fingerprint. The key embedded
                        in this command is shown once and discarded when you close
                        this dialog.
                      </p>
                    </div>
                  </div>
                  <Input
                    isReadOnly
                    label='Install command'
                    labelPlacement='outside'
                    value={issued.command}
                    description={`Prefix ${issued.keyPrefix}… · fingerprint ${issued.keyFingerprint}`}
                    classNames={{
                      input: 'font-mono text-xs break-all',
                    }}
                  />
                  <Button
                    color='primary'
                    variant='flat'
                    startContent={
                      copied === 'command' ? (
                        <Check className='w-4 h-4' />
                      ) : (
                        <Copy className='w-4 h-4' />
                      )
                    }
                    onPress={() => copyText('command', issued.command)}
                  >
                    {copied === 'command' ? 'Copied' : 'Copy command'}
                  </Button>
                  <div>
                    <button
                      type='button'
                      onClick={() => setFallbackOpen((open) => !open)}
                      className='flex items-center gap-1 text-xs font-medium text-default-500 transition-colors hover:text-foreground'
                    >
                      {fallbackOpen ? (
                        <ChevronDown className='w-3.5 h-3.5' />
                      ) : (
                        <ChevronRight className='w-3.5 h-3.5' />
                      )}
                      Alternative install methods
                    </button>
                    {fallbackOpen && (
                      <div className='mt-2 space-y-2'>
                        {[
                          { id: 'plugin', text: PLUGIN_INSTALL_COMMAND },
                          { id: 'connect', text: issued.fallbackCommand },
                        ].map((entry) => (
                          <div
                            key={entry.id}
                            className='flex items-center gap-2 rounded-medium border border-divider bg-content2 px-3 py-2'
                          >
                            <code className='min-w-0 flex-1 truncate font-mono text-[11.5px] text-default-500'>
                              {entry.text}
                            </code>
                            <Button
                              isIconOnly
                              size='sm'
                              variant='light'
                              aria-label='Copy command'
                              onPress={() => copyText(entry.id, entry.text)}
                            >
                              {copied === entry.id ? (
                                <Check className='w-3.5 h-3.5' />
                              ) : (
                                <Copy className='w-3.5 h-3.5' />
                              )}
                            </Button>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              ) : (
                <div className='space-y-4'>
                  <p className='text-sm text-default-500'>
                    Issue a scoped key and get a one-line command to install the
                    Engram plugin into a Claude Code harness.
                  </p>
                  <Select
                    label='Project'
                    labelPlacement='outside'
                    placeholder={
                      projectsQuery.isLoading ? 'Loading projects…' : 'Select a project'
                    }
                    selectedKeys={
                      new Set<string>(effectiveProjectId ? [effectiveProjectId] : [])
                    }
                    onSelectionChange={(keys) => {
                      const next = Array.from(keys)[0];

                      setSelectedProjectId(typeof next === 'string' ? next : null);
                    }}
                    isDisabled={isIssuing || projects.length === 0}
                  >
                    {projects.map((project) => (
                      <SelectItem key={project.id}>{project.name}</SelectItem>
                    ))}
                  </Select>
                  {!projectsQuery.isLoading && projects.length === 0 && (
                    <p className='text-xs text-default-500'>
                      No projects available for this organization yet.
                    </p>
                  )}
                  <Input
                    label='Server URL'
                    labelPlacement='outside'
                    placeholder='https://engram.example.com'
                    value={serverUrl}
                    onValueChange={setServerUrl}
                    description='Where the agent should reach this Engram backend.'
                    isDisabled={isIssuing}
                    classNames={{
                      input: 'font-mono text-xs',
                    }}
                  />
                  {issueError && (
                    <div className='rounded-medium bg-danger-50 dark:bg-danger-500/10 border border-danger-200 dark:border-danger-500/30 p-3'>
                      <p className='text-sm text-danger-600'>{issueError}</p>
                    </div>
                  )}
                </div>
              )}
            </ModalBody>
            <ModalFooter>
              {issued ? (
                <Button color='primary' onPress={onClose}>
                  Done
                </Button>
              ) : meQuery.isError || !meLoaded || !canIssue ? (
                <Button color='default' variant='light' onPress={onClose}>
                  Close
                </Button>
              ) : (
                <>
                  <Button
                    color='default'
                    variant='light'
                    onPress={onClose}
                    isDisabled={isIssuing}
                  >
                    Cancel
                  </Button>
                  <Button
                    color='primary'
                    onPress={handleGenerate}
                    isDisabled={!canGenerate}
                    isLoading={isIssuing}
                  >
                    Generate command
                  </Button>
                </>
              )}
            </ModalFooter>
          </>
        )}
      </ModalContent>
    </Modal>
  );
}
