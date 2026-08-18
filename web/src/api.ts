let csrf = '';

export async function beginSession(): Promise<void> {
  const response = await fetch('/api/session');
  if (!response.ok) throw new Error(`Loom session failed (${response.status})`);
  csrf = (await response.json()).csrf;
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers);
  if (options.body) headers.set('content-type', 'application/json');
  if (!['GET', 'HEAD'].includes(options.method ?? 'GET')) headers.set('x-loom-csrf', csrf);
  const response = await fetch(path, { ...options, headers });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try { detail = (await response.json()).detail ?? detail; } catch (error: unknown) { console.warn('Loom error response was not JSON:', error); }
    throw new Error(detail);
  }
  return response.json() as Promise<T>;
}

export const post = <T>(path: string, value: unknown) => api<T>(path, { method: 'POST', body: JSON.stringify(value) });
