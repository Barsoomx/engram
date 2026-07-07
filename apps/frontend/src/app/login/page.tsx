'use client';

import { Input } from '@heroui/react';
import { Eye, EyeOff, Lock, User } from 'lucide-react';
import { useRouter, useSearchParams } from 'next/navigation';
import * as React from 'react';

import { BrandMark } from '@/components/brand/brand-logo';
import { PrimaryButton } from '@/components/ui/primary-button';
import { extractAuthError, getToken, login } from '@/lib/auth';

function safeNext(next: string | null): string {
  if (!next || !next.startsWith('/') || next.startsWith('//')) {
    return '/';
  }

  return next;
}

function LoginForm() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const next = safeNext(searchParams.get('next'));
  const [username, setUsername] = React.useState('');
  const [password, setPassword] = React.useState('');
  const [showPassword, setShowPassword] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = React.useState(false);

  React.useEffect(() => {
    if (getToken()) {
      router.replace(next);
    }
  }, [router, next]);

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
      router.replace(next);
    } catch (err) {
      setError(extractAuthError(err));
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className='auth-bg flex min-h-screen items-center justify-center px-4'>
      <div className='relative z-10 w-full max-w-sm animate-fade-up'>
        <div className='flex flex-col items-center text-center'>
          <BrandMark size={52} />
          <h1 className='mt-5 text-xl font-semibold tracking-tight text-foreground'>
            Welcome back
          </h1>
          <p className='mt-1.5 text-sm text-default-500'>
            Sign in to the Engram console
          </p>
        </div>

        <div className='surface-card mt-7 rounded-2xl p-7 shadow-login-card'>
          <form className='flex flex-col gap-4' onSubmit={handleSubmit}>
            <Input
              autoComplete='username'
              autoFocus
              label='Username'
              labelPlacement='outside'
              placeholder='Enter username'
              startContent={
                <User
                  className='shrink-0 text-default-400'
                  size={16}
                  strokeWidth={1.8}
                />
              }
              value={username}
              variant='bordered'
              onValueChange={setUsername}
            />
            <Input
              autoComplete='current-password'
              label='Password'
              labelPlacement='outside'
              placeholder='Enter password'
              startContent={
                <Lock
                  className='shrink-0 text-default-400'
                  size={16}
                  strokeWidth={1.8}
                />
              }
              endContent={
                <button
                  aria-label={showPassword ? 'Hide password' : 'Show password'}
                  className='text-default-400 outline-hidden hover:text-default-600'
                  type='button'
                  onClick={() => setShowPassword((value) => !value)}
                >
                  {showPassword ? (
                    <EyeOff size={16} strokeWidth={1.8} />
                  ) : (
                    <Eye size={16} strokeWidth={1.8} />
                  )}
                </button>
              }
              type={showPassword ? 'text' : 'password'}
              value={password}
              variant='bordered'
              onValueChange={setPassword}
            />

            {error && (
              <p className='rounded-medium border border-danger-500/25 bg-danger-500/10 px-3 py-2 text-xs text-danger-500'>
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
        </div>

        <p className='mt-6 text-center text-xs text-default-400'>
          Engram · engineering memory for AI agents
        </p>
      </div>
    </div>
  );
}

export default function LoginPage() {
  return (
    <React.Suspense fallback={null}>
      <LoginForm />
    </React.Suspense>
  );
}
