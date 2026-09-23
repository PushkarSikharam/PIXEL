export interface SpeechInput {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  onresult: ((event: { results: ArrayLike<ArrayLike<{ transcript: string }>> }) => void) | null;
  onerror: ((event: { error: string }) => void) | null;
  onend: (() => void) | null;
  start(): void;
  abort(): void;
}

export function speechInputConstructor(): (new () => SpeechInput) | null {
  if (typeof window === "undefined") return null;
  const host = window as typeof window & {
    SpeechRecognition?: new () => SpeechInput;
    webkitSpeechRecognition?: new () => SpeechInput;
  };
  return host.SpeechRecognition ?? host.webkitSpeechRecognition ?? null;
}
