'use client';

import { Button, type ButtonProps } from '@heroui/react';
import clsx from 'clsx';
import * as React from 'react';

export type PrimaryButtonProps = Omit<ButtonProps, 'color' | 'variant'>;

export const PrimaryButton = React.forwardRef<
  HTMLButtonElement,
  PrimaryButtonProps
>(function PrimaryButton({ className, ...props }, ref) {
  return (
    <Button
      ref={ref}
      disableRipple
      className={clsx(
        'btn-premium h-10 rounded-[11px] px-4 text-[13.5px] font-semibold',
        className,
      )}
      {...props}
    />
  );
});
