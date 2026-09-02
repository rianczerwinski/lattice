export type NotificationPort = {
  permission: NotificationPermission;
  requestPermission: () => Promise<NotificationPermission>;
  notify: (title: string, body: string) => void;
};

export function browserNotificationPort(): NotificationPort | null {
  if (typeof Notification === 'undefined') return null;
  return {
    permission: Notification.permission,
    requestPermission: () => Notification.requestPermission(),
    notify: (title, body) => {
      new Notification(title, { body, tag: 'lattice-activity' });
    },
  };
}

export async function notifyOperator(
  title: string,
  body: string,
  options: { visible?: boolean; port?: NotificationPort | null } = {},
): Promise<boolean> {
  const port = options.port === undefined ? browserNotificationPort() : options.port;
  if (!port || options.visible) return false;
  let permission = port.permission;
  if (permission === 'default') permission = await port.requestPermission();
  if (permission !== 'granted') return false;
  port.notify(title, body);
  return true;
}
