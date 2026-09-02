export const MAX_PROMPT_HISTORY = 40;

export function recordPrompt(history: string[], prompt: string): string[] {
  const trimmed = prompt.trim();
  if (!trimmed) return history;
  return [trimmed, ...history.filter((item) => item !== trimmed)].slice(0, MAX_PROMPT_HISTORY);
}

export function previousPrompt(history: string[], cursor: number | null): { prompt: string; cursor: number } | null {
  if (!history.length) return null;
  const next = cursor === null ? 0 : Math.min(history.length - 1, cursor + 1);
  return { prompt: history[next], cursor: next };
}

export function nextPrompt(history: string[], cursor: number | null, liveDraft: string): { prompt: string; cursor: number | null } {
  if (cursor === null || cursor <= 0) return { prompt: liveDraft, cursor: null };
  const next = cursor - 1;
  return { prompt: history[next], cursor: next };
}
