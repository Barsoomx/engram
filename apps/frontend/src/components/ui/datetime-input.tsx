'use client';

import { Input, type InputProps } from '@heroui/react';

export type DateTimeInputProps = Omit<InputProps, 'type'>;

export function DateTimeInput({
  labelPlacement = 'outside',
  placeholder = 'Select date and time',
  ...props
}: DateTimeInputProps) {
  return (
    <Input
      {...props}
      type='datetime-local'
      labelPlacement={labelPlacement}
      placeholder={placeholder}
    />
  );
}
