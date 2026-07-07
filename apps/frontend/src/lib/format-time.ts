import { formatRelativeTime } from '@/lib/design';

export function formatAbsolute(value: string | null | undefined): string {
  if (!value) {
    return '—';
  }

  const date = new Date(value);

  if (Number.isNaN(date.getTime())) {
    return value;
  }

  return date.toLocaleString();
}

export function formatDate(value: string | null | undefined): string {
  if (!value) {
    return '—';
  }

  const date = new Date(value);

  if (Number.isNaN(date.getTime())) {
    return value;
  }

  return date.toLocaleDateString();
}

export function formatRelative(value: string | null | undefined): string {
  return formatRelativeTime(value);
}

export function startOfDayIso(date: string | null | undefined): string | undefined {
  if (!date) {
    return undefined;
  }

  const parsed = new Date(`${date}T00:00:00`);

  if (Number.isNaN(parsed.getTime())) {
    return undefined;
  }

  return parsed.toISOString();
}

export function endOfDayExclusiveIso(
  date: string | null | undefined,
): string | undefined {
  if (!date) {
    return undefined;
  }

  const parsed = new Date(`${date}T00:00:00`);

  if (Number.isNaN(parsed.getTime())) {
    return undefined;
  }

  parsed.setDate(parsed.getDate() + 1);

  return parsed.toISOString();
}

export function endOfDayInclusiveIso(
  date: string | null | undefined,
): string | undefined {
  if (!date) {
    return undefined;
  }

  const parsed = new Date(`${date}T23:59:59.999`);

  if (Number.isNaN(parsed.getTime())) {
    return undefined;
  }

  return parsed.toISOString();
}
