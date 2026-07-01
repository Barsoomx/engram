export type MemoryKind = 'decision' | 'convention' | 'gotcha' | 'architecture';

export interface KindStyle {
  label: string;
  text: string;
  bg: string;
  dot: string;
}

export const KIND_STYLES: Record<MemoryKind, KindStyle> = {
  decision: {
    label: 'Decision',
    text: '#A78BFF',
    bg: 'rgba(124,92,255,0.16)',
    dot: '#A78BFF',
  },
  convention: {
    label: 'Convention',
    text: '#3DD9AC',
    bg: 'rgba(47,212,167,0.13)',
    dot: '#3DD9AC',
  },
  gotcha: {
    label: 'Gotcha',
    text: '#F2B765',
    bg: 'rgba(242,183,101,0.14)',
    dot: '#F2B765',
  },
  architecture: {
    label: 'Architecture',
    text: '#6BA6FF',
    bg: 'rgba(91,157,255,0.14)',
    dot: '#6BA6FF',
  },
};

export function resolveKind(value: string | null | undefined): MemoryKind {
  const v = (value ?? '').toLowerCase();

  if (v in KIND_STYLES) {
    return v as MemoryKind;
  }

  if (v.startsWith('conv')) {
    return 'convention';
  }

  if (v.startsWith('gotcha') || v.startsWith('pitfall')) {
    return 'gotcha';
  }

  if (v.startsWith('arch')) {
    return 'architecture';
  }

  return 'decision';
}

export const AVATAR_PALETTE = [
  '#7C5CFF',
  '#3DD9AC',
  '#6BA6FF',
  '#F2B765',
  '#FB6E72',
  '#A78BFF',
] as const;

export const AVATAR_GRADIENTS = [
  'linear-gradient(150deg,#8B6BFF,#6A4DFF)',
  'linear-gradient(150deg,#46E3B6,#23B58E)',
  'linear-gradient(150deg,#7FB4FF,#4C82E8)',
  'linear-gradient(150deg,#F7C778,#E09B3F)',
  'linear-gradient(150deg,#FF858A,#E2545C)',
  'linear-gradient(150deg,#B79CFF,#7C5CFF)',
] as const;

export function hashIndex(seed: string, length: number): number {
  let hash = 0;

  for (let i = 0; i < seed.length; i += 1) {
    hash = (hash * 31 + seed.charCodeAt(i)) | 0;
  }

  return Math.abs(hash) % Math.max(length, 1);
}

export function avatarColor(seed: string): string {
  return AVATAR_PALETTE[hashIndex(seed, AVATAR_PALETTE.length)];
}

export function avatarGradient(seed: string): string {
  return AVATAR_GRADIENTS[hashIndex(seed, AVATAR_GRADIENTS.length)];
}

export function initials(value: string, max = 2): string {
  const cleaned = (value ?? '').trim();

  if (!cleaned) {
    return '?';
  }

  const words = cleaned.split(/[\s_\-./]+/).filter(Boolean);

  if (words.length === 1) {
    return words[0].slice(0, max).toUpperCase();
  }

  return words
    .slice(0, max)
    .map((w) => w[0])
    .join('')
    .toUpperCase();
}

export function formatRelativeTime(value: string | null | undefined): string {
  if (!value) {
    return '—';
  }

  const ts = new Date(value).getTime();

  if (!Number.isFinite(ts)) {
    return value;
  }

  const diff = Date.now() - ts;
  const sec = Math.round(diff / 1000);

  if (sec < 45) {
    return 'just now';
  }

  const min = Math.round(sec / 60);

  if (min < 60) {
    return `${min}m ago`;
  }

  const hr = Math.round(min / 60);

  if (hr < 24) {
    return `${hr}h ago`;
  }

  const day = Math.round(hr / 24);

  if (day < 7) {
    return `${day}d ago`;
  }

  const week = Math.round(day / 7);

  if (week < 5) {
    return `${week}w ago`;
  }

  const month = Math.round(day / 30);

  if (month < 12) {
    return `${month}mo ago`;
  }

  return `${Math.round(day / 365)}y ago`;
}

export const AUDIT_RESULT_COLORS: Record<string, string> = {
  success: '#3DD9AC',
  allowed: '#3DD9AC',
  recorded: '#6BA6FF',
  denied: '#FB6E72',
};

export function auditResultColor(value: string | null | undefined): string {
  return AUDIT_RESULT_COLORS[(value ?? '').toLowerCase()] ?? '#8B8D93';
}

export function auditResultChipColor(
  value: string | null | undefined,
): 'success' | 'danger' | 'default' {
  const v = (value ?? '').toLowerCase();

  if (v === 'allowed') {
    return 'success';
  }

  if (v === 'denied' || v === 'failed' || v === 'errored') {
    return 'danger';
  }

  return 'default';
}
