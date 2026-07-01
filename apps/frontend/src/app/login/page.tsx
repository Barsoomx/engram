'use client';

import { Button, Input } from '@heroui/react';
import { Lock, User } from 'lucide-react';
import { useRouter } from 'next/navigation';
import * as React from 'react';

import { BrandMark } from '@/components/brand/brand-logo';
import { PrimaryButton } from '@/components/ui/primary-button';
import { extractAuthError, getToken, login } from '@/lib/auth';

function GithubMark() {
  return (
    <svg
      width={17}
      height={17}
      viewBox='0 0 24 24'
      fill='currentColor'
      aria-hidden='true'
    >
      <path d='M12 .5C5.37.5 0 5.87 0 12.5c0 5.3 3.44 9.8 8.21 11.39.6.11.82-.26.82-.58v-2.03c-3.34.73-4.04-1.61-4.04-1.61-.55-1.39-1.34-1.76-1.34-1.76-1.09-.75.08-.73.08-.73 1.2.08 1.84 1.24 1.84 1.24 1.07 1.83 2.81 1.3 3.49 1 .11-.78.42-1.31.76-1.61-2.67-.3-5.47-1.33-5.47-5.93 0-1.31.47-2.38 1.24-3.22-.13-.3-.54-1.52.11-3.18 0 0 1.01-.32 3.3 1.23a11.5 11.5 0 0 1 6 0c2.29-1.55 3.3-1.23 3.3-1.23.65 1.66.24 2.88.12 3.18.77.84 1.24 1.91 1.24 3.22 0 4.61-2.81 5.63-5.49 5.92.43.37.81 1.1.81 2.22v3.29c0 .32.22.7.83.58C20.56 22.3 24 17.8 24 12.5 24 5.87 18.63.5 12 .5z' />
    </svg>
  );
}

export default function LoginPage() {
  const router = useRouter();
  const [username, setUsername] = React.useState('');
  const [password, setPassword] = React.useState('');
  const [error, setError] = React.useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = React.useState(false);

  React.useEffect(() => {
    if (getToken()) {
      router.replace('/');
    }
  }, [router]);

  const handleSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError(null);

    if (!username.trim() || !password) {
      setError('Username and password are required.');

      return;
    }

    setIsSubmitting(true);

    try {
      await login(username.trim(), password);
      router.replace('/');
    } catch (err) {
      setError(extractAuthError(err));
    } finally {
      setIsSubmitting(false);
    }
  };

  const fieldClassNames = {
    label: 'text-[12px] font-medium text-default-500 pb-1.5',
    inputWrapper:
      'h-[46px] rounded-[10px] border border-divider-strong bg-content2 shadow-none data-[hover=true]:bg-content2 group-data-[focus=true]:border-primary group-data-[focus=true]:bg-content2',
    innerWrapper: 'gap-2.5',
    input: 'text-[13.5px] text-foreground placeholder:text-default-400',
  } as const;

  return (
    <div className='auth-bg flex min-h-screen items-center justify-center px-4'>
      <div className='relative z-10 w-full max-w-[392px] animate-fade-up'>
        <div className='flex flex-col items-center text-center'>
          <BrandMark size={52} />
          <h1 className='mt-5 text-[21px] font-semibold tracking-[-0.02em] text-foreground'>
            Welcome back
          </h1>
          <p className='mt-1.5 text-[13.5px] text-default-500'>
            Sign in to the Engram console
          </p>
        </div>

        <div className='surface-card mt-7 rounded-[20px] p-7 shadow-login-card'>
          <form className='flex flex-col gap-4' onSubmit={handleSubmit}>
            <Input
              autoComplete='username'
              classNames={fieldClassNames}
              label='Username'
              labelPlacement='outside'
              placeholder='Enter username'
              startContent={
                <User className='shrink-0 text-default-400' size={16} strokeWidth={1.8} />
              }
              value={username}
              variant='bordered'
              onValueChange={setUsername}
            />
            <Input
              autoComplete='current-password'
              classNames={fieldClassNames}
              label='Password'
              labelPlacement='outside'
              placeholder='Enter password'
              startContent={
                <Lock className='shrink-0 text-default-400' size={16} strokeWidth={1.8} />
              }
              type='password'
              value={password}
              variant='bordered'
              onValueChange={setPassword}
            />

            {error && (
              <p className='rounded-[10px] border border-danger-500/25 bg-danger-500/10 px-3 py-2 text-[12.5px] text-danger-500'>
                {error}
              </p>
            )}

            <PrimaryButton
              className='mt-1 w-full'
              fullWidth
              isDisabled={isSubmitting}
              isLoading={isSubmitting}
              type='submit'
            >
              Sign in
            </PrimaryButton>
          </form>

          <div className='my-5 flex items-center gap-3'>
            <span className='h-px flex-1 bg-divider-strong' />
            <span className='text-[11px] font-medium uppercase tracking-[0.12em] text-default-400'>
              OR
            </span>
            <span className='h-px flex-1 bg-divider-strong' />
          </div>

          <div className='flex flex-col items-center gap-1.5'>
            <Button
              className='h-11 w-full rounded-[11px] border-divider-strong bg-content1 text-[13.5px] font-medium text-default-700 data-[hover=true]:bg-content2'
              disableRipple
              fullWidth
              isDisabled
              startContent={<GithubMark />}
              type='button'
              variant='bordered'
            >
              Continue with GitHub
            </Button>
            <span className='text-[11.5px] text-default-400'>SSO coming soon</span>
          </div>
        </div>

        <p className='mt-6 text-center text-[12px] text-default-400'>
          Engram · engineering memory for AI agents
        </p>
      </div>
    </div>
  );
}
