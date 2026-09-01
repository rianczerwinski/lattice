export type PaletteCommand = {
  id: string;
  label: string;
  detail: string;
  keywords?: string[];
  shortcut?: string;
  disabled?: boolean;
};

export function filterPaletteCommands(commands: PaletteCommand[], query: string): PaletteCommand[] {
  const terms = query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
  if (!terms.length) return commands;
  return commands
    .map((command, index) => {
      const label = command.label.toLocaleLowerCase();
      const haystack = `${label} ${command.detail.toLocaleLowerCase()} ${(command.keywords ?? []).join(' ').toLocaleLowerCase()}`;
      const matches = terms.every((term) => haystack.includes(term));
      const prefixRank = label.startsWith(terms.join(' ')) ? 0 : label.startsWith(terms[0]) ? 1 : 2;
      return { command, index, matches, prefixRank };
    })
    .filter((item) => item.matches)
    .sort((left, right) => left.prefixRank - right.prefixRank || left.index - right.index)
    .map((item) => item.command);
}
