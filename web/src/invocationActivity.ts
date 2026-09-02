import type { InvocationActivity, InvocationActivityMember, InvocationActivitySnapshot } from './types';

export type ProjectActivityBadge = {
  active: number;
  attention: number;
  unread: boolean;
  failed: boolean;
};

export function activityForInvocation(items: InvocationActivity[], invocationId: string | null | undefined): InvocationActivity | null {
  if (!invocationId) return null;
  return items.find((activity) => activity.members.some((member) => member.invocation_id === invocationId)) ?? null;
}

export function memberForInvocation(activity: InvocationActivity | null, invocationId: string | null | undefined): InvocationActivityMember | null {
  if (!activity || !invocationId) return null;
  return activity.members.find((member) => member.invocation_id === invocationId) ?? null;
}

export function activeAttemptIds(activity: InvocationActivity | null, invocationId: string | null | undefined): string[] {
  const member = memberForInvocation(activity, invocationId);
  if (!member) return [];
  return member.attempts.filter((attempt) => ['leased', 'running'].includes(attempt.state)).map((attempt) => attempt.attempt_id);
}

export function activityFailureForInvocation(activity: InvocationActivity | null, invocationId: string | null | undefined): string {
  const failure = memberForInvocation(activity, invocationId)?.dispatch_failure;
  if (!failure) return '';
  const detail = failure.detail.error;
  return typeof detail === 'string' && detail.trim() ? detail : failure.event_type.replace('invocation-batch.', '').replaceAll('-', ' ');
}

export function mergeActivitySnapshots(current: InvocationActivitySnapshot, incoming: InvocationActivitySnapshot): InvocationActivitySnapshot {
  const projectIds = new Set([...Object.keys(current.project_cursors), ...Object.keys(incoming.project_cursors)]);
  const items: InvocationActivity[] = [];
  const projectCursors: Record<string, number> = {};
  const activityCursors: Record<string, number> = {};
  const recentHasMore: Record<string, boolean> = {};
  for (const projectId of projectIds) {
    const currentCursor = current.project_cursors[projectId] ?? 0;
    const hasIncoming = Object.hasOwn(incoming.project_cursors, projectId);
    const useIncoming = hasIncoming && (incoming.project_cursors[projectId] ?? 0) >= currentCursor;
    const source = useIncoming ? incoming : current;
    items.push(...source.items.filter((activity) => activity.project_id === projectId));
    projectCursors[projectId] = source.project_cursors[projectId] ?? 0;
    activityCursors[projectId] = source.activity_cursors[projectId] ?? 0;
    recentHasMore[projectId] = source.recent_has_more[projectId] ?? false;
  }
  return { items: items.sort((left, right) => right.last_event_seq - left.last_event_seq), project_cursors: projectCursors, activity_cursors: activityCursors, recent_has_more: recentHasMore };
}

export function projectActivityBadges(snapshot: InvocationActivitySnapshot, seenCursors: Record<string, number>, currentProjectId = ''): Record<string, ProjectActivityBadge> {
  const projectIds = new Set([...Object.keys(snapshot.project_cursors), ...snapshot.items.map((activity) => activity.project_id)]);
  return Object.fromEntries([...projectIds].map((projectId) => {
    const activities = snapshot.items.filter((activity) => activity.project_id === projectId);
    const cursor = snapshot.activity_cursors[projectId] ?? 0;
    const seen = currentProjectId === projectId ? cursor : seenCursors[projectId] ?? 0;
    const unreadActivities = activities.filter((activity) => activity.last_event_seq > seen);
    return [projectId, {
      active: activities.filter((activity) => !activity.terminal && !activity.needs_attention).length,
      attention: activities.filter((activity) => activity.needs_attention).length,
      unread: cursor > seen,
      failed: activities.some((activity) => activity.needs_attention) || unreadActivities.some((activity) => ['failed', 'interrupted', 'mixed'].includes(activity.state)),
    }];
  }));
}

export function terminalActivityTransitions(previous: InvocationActivitySnapshot, next: InvocationActivitySnapshot): InvocationActivity[] {
  const prior = new Map(previous.items.map((activity) => [activity.activity_id, activity]));
  return next.items.filter((activity) => activity.terminal && prior.has(activity.activity_id) && !prior.get(activity.activity_id)?.terminal);
}

export function attentionActivityTransitions(previous: InvocationActivitySnapshot, next: InvocationActivitySnapshot): InvocationActivity[] {
  const prior = new Map(previous.items.map((activity) => [activity.activity_id, activity]));
  return next.items.filter((activity) => activity.needs_attention && !prior.get(activity.activity_id)?.needs_attention);
}

export function projectReplayCursor(snapshot: InvocationActivitySnapshot, projectId: string): number {
  const activeStarts = snapshot.items
    .filter((activity) => activity.project_id === projectId && !activity.terminal)
    .flatMap((activity) => {
      const attempts = activity.members
        .flatMap((member) => member.attempts)
        .filter((attempt) => ['leased', 'running'].includes(attempt.state))
        .map((attempt) => attempt.replay_event_cursor);
      return attempts.length ? attempts : [activity.replay_event_cursor];
    })
    .filter((cursor) => cursor > 0);
  return activeStarts.length ? Math.max(0, Math.min(...activeStarts) - 1) : snapshot.project_cursors[projectId] ?? 0;
}
