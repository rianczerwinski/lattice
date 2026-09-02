const FILE_LINK = /(?:^|[\s`'(])((?:[A-Za-z0-9._-]+\/)+[A-Za-z0-9._-]+\.[A-Za-z0-9]+)(?::(\d+))?/g;

export type FileLink = { path: string; line: number | null };

export function extractFileLinks(value: string): FileLink[] {
  const seen = new Set<string>();
  const hits: FileLink[] = [];
  for (const match of value.matchAll(FILE_LINK)) {
    const path = match[1];
    const line = match[2] ? Number(match[2]) : null;
    const key = `${path}:${line ?? ''}`;
    if (seen.has(key)) continue;
    seen.add(key);
    hits.push({ path, line: Number.isInteger(line) ? line : null });
  }
  return hits;
}
