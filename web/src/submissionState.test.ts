import { describe, expect, it } from 'vitest';
import { reconcileSubmittedComposer, type ComposerSubmissionState } from './submissionState';

const submitted: ComposerSubmissionState = {
  prompt: 'submitted words',
  explicitSelections: [{ block_id: 'evidence_1' }],
  selectedParentIds: ['parent_1'],
};

describe('submission completion', () => {
  it('clears only submitted material and advances ordinary conversation ancestry', () => {
    expect(reconcileSubmittedComposer(submitted, submitted, 'answer_1')).toEqual({
      prompt: '',
      explicitSelections: [],
      selectedParentIds: ['answer_1'],
    });
  });

  it('preserves text and context selected while the provider was working', () => {
    const current = {
      prompt: 'next unsent thought',
      explicitSelections: [{ block_id: 'evidence_2' }],
      selectedParentIds: ['side_quest_1'],
    };
    expect(reconcileSubmittedComposer(current, submitted, 'answer_1')).toEqual(current);
  });

  it('does not erase a changed parent selection when only text stayed unchanged', () => {
    expect(reconcileSubmittedComposer({ ...submitted, selectedParentIds: ['parent_2'] }, submitted, 'answer_1').selectedParentIds).toEqual(['parent_2']);
  });
});
