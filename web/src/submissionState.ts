export type ComposerSubmissionState = {
  prompt: string;
  explicitSelections: Array<Record<string, unknown>>;
  selectedParentIds: string[];
};

function exactSame(left: unknown, right: unknown): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

export function reconcileSubmittedComposer(
  current: ComposerSubmissionState,
  submitted: ComposerSubmissionState,
  foregroundInvocationId: string | undefined,
): ComposerSubmissionState {
  return {
    prompt: current.prompt === submitted.prompt ? '' : current.prompt,
    explicitSelections: exactSame(current.explicitSelections, submitted.explicitSelections) ? [] : current.explicitSelections,
    selectedParentIds: exactSame(current.selectedParentIds, submitted.selectedParentIds) && foregroundInvocationId
      ? [foregroundInvocationId]
      : current.selectedParentIds,
  };
}
