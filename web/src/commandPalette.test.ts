import { describe, expect, it } from 'vitest';
import { filterPaletteCommands, type PaletteCommand } from './commandPalette';

const commands: PaletteCommand[] = [
  { id: 'composer', label: 'Focus composer', detail: 'Write the next invocation', keywords: ['prompt'] },
  { id: 'project-a', label: 'Open Alpha', detail: 'Switch project', keywords: ['recent'] },
  { id: 'run-a', label: 'Open active run', detail: 'Relata evaluation', keywords: ['progress'] },
];

describe('command palette navigation property', () => {
  it('matches every query term across labels, details, and keywords while preserving stable ties', () => {
    expect(filterPaletteCommands(commands, 'relata progress').map((item) => item.id)).toEqual(['run-a']);
    expect(filterPaletteCommands(commands, 'open').map((item) => item.id)).toEqual(['project-a', 'run-a']);
    expect(filterPaletteCommands(commands, '').map((item) => item.id)).toEqual(['composer', 'project-a', 'run-a']);
  });
});
