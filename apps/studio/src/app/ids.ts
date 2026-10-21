/** textarea → deduped id list (comma/whitespace/newline separated). */
export function parseIds(raw: string): string[] {
  return [...new Set(raw.split(/[\s,]+/).map(s => s.trim()).filter(s => s))];
}
