'use client';

import { Button, Card, CardBody, Input } from '@heroui/react';
import { LogIn } from 'lucide-react';
import { useRouter } from 'next/navigation';
import * as React from 'react';

import { extractAuthError, getToken, login } from '@/lib/auth';

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

  return (
    <div className='min-h-screen flex items-center justify-center px-4 auth-bg'>
      <Card className='w-full max-w-sm'>
        <CardBody className='p-8'>
          <div className='flex flex-col items-center mb-6'>
            <div className='w-12 h-12 rounded-xl bg-primary/15 flex items-center justify-center mb-3'>
              <LogIn className='w-6 h-6 text-primary' />
            </div>
            <h1 className='text-xl font-semibold text-foreground'>
              Engram Admin
            </h1>
            <p className='text-sm text-default-500 mt-1'>
              Sign in to the admin console
            </p>
          </div>

          <form className='flex flex-col gap-4' onSubmit={handleSubmit}>
            <Input
              autoComplete='username'
              isClearable
              label='Username'
              labelPlacement='outside'
              placeholder='Enter username'
              value={username}
              variant='bordered'
              onValueChange={setUsername}
            />
            <Input
              autoComplete='current-password'
              label='Password'
              labelPlacement='outside'
              placeholder='Enter password'
              type='password'
              value={password}
              variant='bordered'
              onValueChange={setPassword}
            />

            {error && (
              <p className='text-sm text-danger-500 bg-danger-50 dark:bg-danger-500/10 rounded-medium px-3 py-2'>
                {error}
              </p>
            )}

            <Button
              className='mt-2'
              color='primary'
              isDisabled={isSubmitting}
              isLoading={isSubmitting}
              type='submit'
            >
              Sign in
            </Button>
          </form>
        </CardBody>
      </Card>
    </div>
  );
}
