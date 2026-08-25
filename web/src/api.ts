let csrf = '';

export async function beginSession(): Promise<void> {
  const fragment = new URLSearchParams(window.location.hash.slice(1));
  const bootstrap = fragment.get('operator-bootstrap');
  const response = bootstrap
    ? await fetch('/api/session/redeem', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ bootstrap_token: bootstrap }),
      })
    : await fetch('/api/session');
  if (!response.ok) throw new Error(`Lattice session failed (${response.status})`);
  csrf = (await response.json()).csrf;
  if (bootstrap) window.history.replaceState(null, '', `${window.location.pathname}${window.location.search}`);
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers);
  if (options.body) headers.set('content-type', 'application/json');
  if (!['GET', 'HEAD'].includes(options.method ?? 'GET')) headers.set('x-lattice-csrf', csrf);
  const response = await fetch(path, { ...options, headers });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try { detail = (await response.json()).detail ?? detail; } catch (error: unknown) { console.warn('Lattice error response was not JSON:', error); }
    throw new Error(detail);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export const post = <T>(path: string, value: unknown) => api<T>(path, { method: 'POST', body: JSON.stringify(value) });

export const extractWorkflow = <T>(projectId: string, invocationIds: string[], title: string) => post<T>('/api/v1/workflows/extract', {
  project_id: projectId,
  invocation_ids: invocationIds,
  title,
});
