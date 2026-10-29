// Task files use naive UTC; activity/transcript records usually carry an offset.
// Resolve the local zone for each timestamp, never cache today's UTC offset.
export function localTime(at: string | null | undefined, timeZone?: string): string {
  if (!at) return ''
  const normalized = /^\d{4}-\d\d-\d\dT/.test(at) && !/(?:Z|[+-]\d\d:\d\d)$/i.test(at) ? `${at}Z` : at
  const date = new Date(normalized)
  return Number.isFinite(date.getTime()) ? new Intl.DateTimeFormat(undefined, {
    hour: '2-digit', minute: '2-digit', hour12: false, timeZone,
  }).format(date) : ''
}
