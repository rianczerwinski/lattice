export type ClipboardPort = { writeText: (value: string) => Promise<void> };

export async function copyText(value: string, clipboard: ClipboardPort): Promise<void> {
  await clipboard.writeText(value);
}

export function invocationExportPayload(invocation: {
  invocation_id: string;
  state: string;
  compiled_prompt?: string;
  output_block?: { content?: string };
  manifest?: unknown;
}): string {
  return `${JSON.stringify({
    invocation_id: invocation.invocation_id,
    state: invocation.state,
    compiled_prompt: invocation.compiled_prompt ?? '',
    response: invocation.output_block?.content ?? '',
    manifest: invocation.manifest ?? null,
  }, null, 2)}\n`;
}
