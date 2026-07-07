'use client';

import { usePathname, useRouter, useSearchParams } from 'next/navigation';
import * as React from 'react';

export type UrlFilterValue = string | number | boolean | null | undefined;

export type UrlFilterState = Record<string, UrlFilterValue>;

export function useUrlFilters<T extends UrlFilterState>(
  defaults: T,
): [T, (next: Partial<T>) => void, () => void] {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  const state = React.useMemo(() => {
    const result = { ...defaults };

    for (const key of Object.keys(defaults) as (keyof T)[]) {
      const raw = searchParams.get(String(key));

      if (raw === null) {
        continue;
      }

      const fallback = defaults[key];

      if (typeof fallback === 'number') {
        const parsed = Number(raw);
        result[key] = (Number.isFinite(parsed) ? parsed : fallback) as T[keyof T];
      } else if (typeof fallback === 'boolean') {
        result[key] = (raw === 'true') as T[keyof T];
      } else {
        result[key] = raw as T[keyof T];
      }
    }

    return result;
  }, [searchParams, defaults]);

  const setFilters = React.useCallback(
    (next: Partial<T>) => {
      const merged = { ...state, ...next };
      const params = new URLSearchParams();

      for (const key of Object.keys(merged) as (keyof T)[]) {
        const value = merged[key];

        if (value === null || value === undefined || value === '') {
          continue;
        }

        if (value === defaults[key]) {
          continue;
        }

        params.set(String(key), String(value));
      }

      const queryString = params.toString();

      router.replace(queryString ? `${pathname}?${queryString}` : pathname, {
        scroll: false,
      });
    },
    [state, defaults, pathname, router],
  );

  const reset = React.useCallback(() => {
    router.replace(pathname, { scroll: false });
  }, [pathname, router]);

  return [state, setFilters, reset];
}
