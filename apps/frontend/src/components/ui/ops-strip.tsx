'use client';

import { ArrowUpRight } from 'lucide-react';
import Link from 'next/link';
import * as React from 'react';

import { ErrorState } from '@/components/ui/error-state';
import { TONE_STYLES, type StatusTone } from '@/lib/design';
import type { OpsOverview } from '@/lib/metrics-api';

interface OpsTile {
  key: string;
  label: string;
  value: number;
  tone: StatusTone;
  sub?: string;
  href?: string;
}

function formatAge(seconds: number | null | undefined): string | undefined {
  if (seconds === null || seconds === undefined) {
    return undefined;
  }

  if (seconds < 60) {
    return `${seconds}s old`;
  }

  const minutes = Math.round(seconds / 60);

  if (minutes < 60) {
    return `${minutes}m old`;
  }

  const hours = Math.round(minutes / 60);

  if (hours < 24) {
    return `${hours}h old`;
  }

  return `${Math.round(hours / 24)}d old`;
}

function toneFor(value: number, warn: number, danger: number): StatusTone {
  if (value >= danger) {
    return 'danger';
  }

  if (value >= warn) {
    return 'warning';
  }

  return 'neutral';
}

function buildTiles(data: OpsOverview): OpsTile[] {
  const tiles: OpsTile[] = [];

  if (data.outbox_backlog_count !== undefined) {
    const age = data.outbox_oldest_age_seconds ?? null;
    const agedOut = age !== null && age > 300;
    tiles.push({
      key: 'outbox',
      label: 'Outbox backlog',
      value: data.outbox_backlog_count,
      tone: agedOut ? 'danger' : toneFor(data.outbox_backlog_count, 1, 100),
      sub: formatAge(age),
    });
  }

  if (data.dead_letter_count !== undefined) {
    tiles.push({
      key: 'dead-letters',
      label: 'Dead letters',
      value: data.dead_letter_count,
      tone: data.dead_letter_count > 0 ? 'danger' : 'neutral',
    });
  }

  tiles.push({
    key: 'failed-runs',
    label: 'Failed workflow runs',
    value: data.failed_workflow_runs,
    tone: toneFor(data.failed_workflow_runs, 1, 10),
    href: '/workflow-runs',
  });

  tiles.push({
    key: 'pending-embeddings',
    label: 'Pending embeddings',
    value: data.pending_embedding_count,
    tone: toneFor(data.pending_embedding_count, 1, 100),
  });

  tiles.push({
    key: 'review-backlog',
    label: 'Review backlog',
    value: data.review_backlog_count,
    tone: toneFor(data.review_backlog_count, 50, 500),
    sub: formatAge(data.oldest_proposed_age_seconds),
    href: '/memory-review',
  });

  tiles.push({
    key: 'provider-errors',
    label: 'Provider errors · 24h',
    value: data.provider_errors_24h,
    tone: data.provider_errors_24h > 0 ? 'danger' : 'neutral',
  });

  return tiles;
}

function TileBody({ tile }: { tile: OpsTile }) {
  const style = TONE_STYLES[tile.tone];
  const alert = tile.tone === 'danger' || tile.tone === 'warning';

  return (
    <>
      <div className='flex items-center justify-between gap-2'>
        <span className='flex min-w-0 items-center gap-1.5'>
          {alert && (
            <span
              className='h-1.5 w-1.5 shrink-0 rounded-full'
              style={{ backgroundColor: style.dot }}
            />
          )}
          <span className='truncate text-[11px] leading-tight text-default-500'>{tile.label}</span>
        </span>
        {tile.href && (
          <ArrowUpRight size={13} strokeWidth={2.2} className='shrink-0 text-default-400' />
        )}
      </div>
      <span
        className='tnum text-[22px] font-semibold leading-none tracking-[-0.02em]'
        style={{ color: alert ? style.text : undefined }}
      >
        {tile.value.toLocaleString()}
      </span>
      <span className='min-h-[13px] text-[10.5px] text-default-400'>{tile.sub ?? ''}</span>
    </>
  );
}

export interface OpsStripProps {
  data: OpsOverview | undefined;
  isLoading: boolean;
  isError: boolean;
  onRetry?: () => void;
  className?: string;
}

export function OpsStrip({ data, isLoading, isError, onRetry, className }: OpsStripProps) {
  if (isError) {
    return (
      <ErrorState
        title='Pipeline health unavailable'
        message='Could not load operational counters.'
        onRetry={onRetry}
      />
    );
  }

  if (isLoading || !data) {
    return (
      <div className={`grid grid-cols-2 gap-2.5 sm:grid-cols-3 lg:grid-cols-6 ${className ?? ''}`}>
        {Array.from({ length: 6 }).map((_, index) => (
          <div
            key={index}
            className='h-[74px] animate-pulse rounded-[12px] border border-divider bg-content2'
          />
        ))}
      </div>
    );
  }

  const tiles = buildTiles(data);

  return (
    <div className={`grid grid-cols-2 gap-2.5 sm:grid-cols-3 lg:grid-cols-6 ${className ?? ''}`}>
      {tiles.map((tile) =>
        tile.href ? (
          <Link
            key={tile.key}
            href={tile.href}
            className='surface-card flex flex-col gap-2 p-3.5 transition-colors hover:border-default-300'
          >
            <TileBody tile={tile} />
          </Link>
        ) : (
          <div key={tile.key} className='surface-card flex flex-col gap-2 p-3.5'>
            <TileBody tile={tile} />
          </div>
        ),
      )}
    </div>
  );
}
