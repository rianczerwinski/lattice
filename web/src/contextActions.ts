export type ConversationRoute = 'anthropic' | 'openai' | 'both';
export type ContextAction = 'side-quest' | 'ask-claude' | 'ask-codex' | 'ask-both' | 'bring-here' | 'compare';

export type ContextActionIntent = {
  sideQuest: boolean;
  route: ConversationRoute;
  promptSeed: string;
};

export function contextActionIntent(action: ContextAction, currentRoute: ConversationRoute, subject: string): ContextActionIntent {
  if (action === 'bring-here') return { sideQuest: false, route: currentRoute, promptSeed: '' };
  if (action === 'side-quest') return { sideQuest: true, route: currentRoute, promptSeed: '' };
  if (action === 'ask-claude') return { sideQuest: true, route: 'anthropic', promptSeed: '' };
  if (action === 'ask-codex') return { sideQuest: true, route: 'openai', promptSeed: '' };
  if (action === 'ask-both') return { sideQuest: true, route: 'both', promptSeed: '' };
  return {
    sideQuest: true,
    route: 'both',
    promptSeed: `Analyze this ${subject} independently so the two responses can be compared.`,
  };
}
