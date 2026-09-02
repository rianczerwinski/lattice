export const DEFAULT_WINDOW_CHARS = 80_000;

export type WindowedText = {
  text: string;
  omittedBefore: number;
  omittedAfter: number;
};

export function windowedText(value: string, maxChars = DEFAULT_WINDOW_CHARS): WindowedText {
  if (value.length <= maxChars) return { text: value, omittedBefore: 0, omittedAfter: 0 };
  const head = Math.max(1, Math.floor(maxChars * 0.8));
  const tail = Math.max(1, maxChars - head);
  return {
    text: `${value.slice(0, head)}\n…\n${value.slice(-tail)}`,
    omittedBefore: 0,
    omittedAfter: value.length - head - tail,
  };
}

export function windowedTail(value: string, maxChars = DEFAULT_WINDOW_CHARS): WindowedText {
  if (value.length <= maxChars) return { text: value, omittedBefore: 0, omittedAfter: 0 };
  return { text: value.slice(-maxChars), omittedBefore: value.length - maxChars, omittedAfter: 0 };
}
