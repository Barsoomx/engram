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
import { fetchMe, hasCapability, type MeResponse } from '@/lib/auth';
import {
  AGENT_TARGET_OPTIONS,
  type AgentTarget,
  buildConnectCommand,
  buildConnectFallbackCommand,
  buildManualInstallCommands,
  validateConnectServerUrl,
} from '@/lib/build-connect-command';
import { CONNECT_CAPABILITIES } from '@/lib/connect-capabilities';
import { useOrgStore } from '@/lib/org-store';

const DEFAULT_AGENT_TARGET: AgentTarget = 'claude-code';

function defaultServerUrl(): string {
  return (
    process.env.NEXT_PUBLIC_ENGRAM_API_URL ||
    (typeof window !== 'undefined' ? window.location.origin : '')
  );
}

interface IssuedCommand {
  agent: AgentTarget;
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

  const meQuery = useQuery<MeResponse>({
    queryKey: ['auth', 'me'],
    queryFn: fetchMe,
  });

  const issueMutation = useIssueApiKey(activeOrgId);
  const resetIssueMutation = issueMutation.reset;

  const [agent, setAgent] = React.useState<AgentTarget>(DEFAULT_AGENT_TARGET);
  const [serverUrl, setServerUrl] = React.useState<string>(defaultServerUrl);
  const [issued, setIssued] = React.useState<IssuedCommand | null>(null);
  const [issueError, setIssueError] = React.useState<string | null>(null);
  const [fallbackOpen, setFallbackOpen] = React.useState(false);
  const [copied, setCopied] = React.useState<string | null>(null);

  React.useEffect(() => {
    if (!isOpen) {
      resetIssueMutation();
      setAgent(DEFAULT_AGENT_TARGET);
      setServerUrl(defaultServerUrl());
      setIssued(null);
      setIssueError(null);
      setFallbackOpen(false);
      setCopied(null);
    }
  }, [isOpen, resetIssueMutation]);

  const capabilities = React.useMemo(
    () => meQuery.data?.capabilities ?? [],
    [meQuery.data?.capabilities],
  );
  const canIssue = hasCapability(capabilities, 'api_keys:issue');
  const meLoaded = meQuery.data !== undefined;
  const agentOption = AGENT_TARGET_OPTIONS.find(
    (option) => option.value === agent,
  );
  const serverUrlError = serverUrl.trim()
    ? validateConnectServerUrl(serverUrl)
    : null;

  async function copyText(id: string, text: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(id);
    } catch {
      setCopied(null);
    }
  }

  async function handleGenerate() {
    setIssueError(null);

    try {
      const result = await issueMutation.mutateAsync({
        name:
          agent === 'both'
            ? 'Claude Code + Codex agent'
            : `${agentOption?.label ?? agent} agent`,
        capabilities: CONNECT_CAPABILITIES,
      });

      setIssued({
        agent,
        command: buildConnectCommand({
          agent,
          serverUrl,
          apiKey: result.plaintext,
        }),
        fallbackCommand: buildConnectFallbackCommand({
          agent,
          serverUrl,
          apiKey: result.plaintext,
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
    canIssue &&
    serverUrl.trim().length > 0 &&
    serverUrlError === null &&
    !isIssuing;

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
              Connect an agent
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
                        One key works across all your repositories — Engram routes
                        memory to the right project by git remote. The key is shown
                        once and discarded when you close this dialog.
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
                  <div className='rounded-medium border border-divider bg-content2 px-3 py-2.5 text-xs text-default-500'>
                    {issued.agent === 'claude-code' ? (
                      <p>
                        Start a new Claude Code session after the command
                        completes.
                      </p>
                    ) : issued.agent === 'codex' ? (
                      <p>
                        In Codex, open <code className='font-mono'>/hooks</code>,
                        review and approve the Engram commands, then start a new
                        thread.
                      </p>
                    ) : (
                      <p>
                        Start a new Claude Code session. In Codex, open{' '}
                        <code className='font-mono'>/hooks</code>, review and
                        approve the Engram commands, then start a new thread.
                      </p>
                    )}
                  </div>
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
                          ...buildManualInstallCommands(issued.agent),
                          {
                            id: 'connect',
                            label: 'Configure Engram credentials only',
                            command: issued.fallbackCommand,
                          },
                        ].map((entry) => (
                          <div
                            key={entry.id}
                            className='flex items-center gap-2 rounded-medium border border-divider bg-content2 px-3 py-2'
                          >
                            <div className='min-w-0 flex-1'>
                              <p className='text-[11px] font-medium text-default-600'>
                                {entry.label}
                              </p>
                              <code className='block truncate font-mono text-[11.5px] text-default-500'>
                                {entry.command}
                              </code>
                            </div>
                            <Button
                              isIconOnly
                              size='sm'
                              variant='light'
                              aria-label={`Copy ${entry.label}`}
                              onPress={() =>
                                copyText(entry.id, entry.command)
                              }
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
                    Issue an org-wide agent key and get a one-line command to
                    install the Engram plugin into Claude Code, Codex, or both.
                    One key covers every repository; memory is routed per
                    project by git remote.
                  </p>
                  <div className='space-y-1.5'>
                    <Select
                      label='Agent runtime'
                      labelPlacement='outside'
                      selectedKeys={new Set([agent])}
                      disallowEmptySelection
                      isDisabled={isIssuing}
                      onSelectionChange={(selection) => {
                        const next = Array.from(selection)[0];

                        if (typeof next === 'string') {
                          setAgent(next as AgentTarget);
                        }
                      }}
                    >
                      {AGENT_TARGET_OPTIONS.map((option) => (
                        <SelectItem key={option.value}>
                          {option.label}
                        </SelectItem>
                      ))}
                    </Select>
                    <p className='text-xs text-default-500'>
                      {agentOption?.description}
                    </p>
                  </div>
                  <Input
                    label='Server URL'
                    labelPlacement='outside'
                    placeholder='https://engram.example.com'
                    value={serverUrl}
                    onValueChange={setServerUrl}
                    description='Where the agent should reach this Engram backend.'
                    isInvalid={serverUrlError !== null}
                    errorMessage={serverUrlError ?? undefined}
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
