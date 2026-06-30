'use client';

import clsx from 'clsx';
import { Check, ChevronDown } from 'lucide-react';
import * as React from 'react';

export function SwitcherBackdrop({ onClose }: { onClose: () => void }) {
  return <div className='fixed inset-0 z-40' onClick={onClose} aria-hidden='true' />;
}

export function SwitcherTrigger({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type='button'
      onClick={onClick}
      className={clsx(
        'flex h-9 max-w-[220px] items-center gap-2 rounded-[9px] px-2 transition-colors',
        active ? 'bg-content2 text-foreground' : 'text-foreground hover:bg-content2/60',
      )}
    >
      {children}
      <ChevronDown
        size={14}
        strokeWidth={2}
        className={clsx(
          'shrink-0 text-default-400 transition-transform',
          active && 'rotate-180',
        )}
      />
    </button>
  );
}

export function DropdownPanel({
  width = 288,
  className,
  children,
}: {
  width?: number;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <div
      className={clsx(
        'absolute left-0 top-[calc(100%+8px)] z-50 rounded-[14px] border border-divider-strong bg-content1 p-[7px] shadow-dropdown',
        className,
      )}
      style={{ width }}
    >
      {children}
    </div>
  );
}

export function DropdownEyebrow({ children }: { children: React.ReactNode }) {
  return (
    <p className='px-2.5 pb-1.5 pt-1 text-[10px] font-semibold uppercase tracking-[0.12em] text-default-400'>
      {children}
    </p>
  );
}

export function DropdownDivider() {
  return <div className='my-[6px] h-px bg-divider' />;
}

export function MenuRow({
  active,
  onClick,
  children,
}: {
  active?: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type='button'
      onClick={onClick}
      className='flex w-full items-center gap-2.5 rounded-[9px] px-2.5 py-2 text-left transition-colors hover:bg-content2'
    >
      {children}
      {active && (
        <Check size={15} strokeWidth={2.4} className='shrink-0 text-primary' />
      )}
    </button>
  );
}

export function MenuActionRow({
  icon: Icon,
  label,
  onClick,
}: {
  icon: typeof Check;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      type='button'
      onClick={onClick}
      className='flex w-full items-center gap-2.5 rounded-[9px] px-2.5 py-2 text-[13px] text-default-500 transition-colors hover:bg-content2 hover:text-foreground'
    >
      <Icon size={15} strokeWidth={1.8} className='shrink-0' />
      {label}
    </button>
  );
}
