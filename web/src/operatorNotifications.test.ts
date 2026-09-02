import { describe, expect, it } from 'vitest';
import { notifyOperator, type NotificationPort } from './operatorNotifications';

function port(permission: NotificationPermission, granted: NotificationPermission = 'granted'): { port: NotificationPort; titles: string[] } {
  const titles: string[] = [];
  return {
    titles,
    port: {
      permission,
      requestPermission: async () => granted,
      notify: (title) => { titles.push(title); },
    },
  };
}

describe('operator notifications contract', () => {
  it('does not notify while the cockpit is visible', async () => {
    const fixture = port('granted');
    expect(await notifyOperator('done', 'work finished', { visible: true, port: fixture.port })).toBe(false);
    expect(fixture.titles).toEqual([]);
  });

  it('notifies background completion after permission is granted', async () => {
    const fixture = port('default', 'granted');
    expect(await notifyOperator('Lattice', 'Codex finished', { visible: false, port: fixture.port })).toBe(true);
    expect(fixture.titles).toEqual(['Lattice']);
  });

  it('stays silent when permission is denied', async () => {
    const fixture = port('denied');
    expect(await notifyOperator('Lattice', 'needs attention', { visible: false, port: fixture.port })).toBe(false);
    expect(fixture.titles).toEqual([]);
  });
});
