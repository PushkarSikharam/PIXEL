"use client";

/**
 * Edith, wherever somebody is in Pixel.
 *
 * One panel serves every authenticated screen. Which product answers depends on where the person
 * is: the product they have open, or the one that describes Pixel itself when they are not inside
 * one. Nothing about a product is carried into another - a different product means a different
 * conversation, and the panel is remounted rather than reused.
 *
 * The panel decides nothing about what may happen. It sends what was typed, shows what came back,
 * and carries out only a change the backend already authorised and handed it a one-time key for.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { Bot, RotateCcw, SendHorizonal, PanelLeftClose, PanelLeftOpen, Volume2, VolumeX, Mic, Square } from "lucide-react";
import { Button, Input, Panel } from "@pixel-console/components/ui";
import {
  closeConversation, executeProductAction, sendProductTurn, productSpeech,
  type ApiActionShape, type ApiProductShape, type ApiSession, type ApiTurnResponse,
} from "@pixel-console/lib/pixel-api";
import { speechInputConstructor, type SpeechInput } from "@pixel-console/lib/speech-input";
import { CONSOLE_ROUTES } from "@pixel-console/lib/console-routes";

/**
 * Things worth suggesting, by where the assistant is answering from. Each one is a message
 * somebody could have typed, so a chip can never ask for something the assistant cannot do.
 */
const PLATFORM_PROMPTS = ["Add a product", "How many products do I have?", "Show me the demo", "How does Pixel work?"];
const PRODUCT_PROMPTS = ["What can you do?", "Take me back to my products"];

export function EdithPanel({ session, shape, productId, scope = "product", onRecordsChanged, onUiAction }: {
  session: ApiSession;
  shape: ApiProductShape;
  productId: string;
  /** Whether this panel is answering for Pixel itself or for a product inside it. */
  scope?: "platform" | "product";
  onRecordsChanged?: () => Promise<void>;
  onUiAction?: (action: ApiActionShape, payload: Record<string, unknown>) => void;
}) {
  const router = useRouter();
  const [messages, setMessages] = useState<Array<{ role: "visitor" | "agent"; text: string }>>([
    { role: "agent", text: `Welcome to ${shape.product_name}. I'm ${shape.assistant_name}.` },
  ]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const [voice, setVoice] = useState(false);
  const voiceEnabled = useRef(false);
  const [voiceStatus, setVoiceStatus] = useState("Voice off");
  const [listening, setListening] = useState(false);
  const [micAvailable, setMicAvailable] = useState(false);
  const recognition = useRef<SpeechInput | null>(null);
  const alive = useRef(true);
  const sending = useRef(false);
  const request = useRef<AbortController | null>(null);
  const speechRequest = useRef<AbortController | null>(null);
  const playing = useRef<HTMLAudioElement | null>(null);
  const audioUrl = useRef<string | null>(null);
  const log = useRef<HTMLDivElement | null>(null);
  const turn = useRef(0);
  const sessionId = useRef("");
  function stopAudio() {
    speechRequest.current?.abort();
    playing.current?.pause();
    playing.current = null;
    if (audioUrl.current) URL.revokeObjectURL(audioUrl.current);
    audioUrl.current = null;
  }
  useEffect(() => {
    alive.current = true;
    sessionId.current = crypto.randomUUID();
    setMicAvailable(speechInputConstructor() !== null);
    const ending = sessionId.current;
    return () => {
      alive.current = false;
      request.current?.abort();
      recognition.current?.abort();
      stopAudio();
      // Leaving takes the conversation with it, including anything it proposed and nobody did.
      void closeConversation(session, ending);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => { log.current?.scrollTo({ top: log.current.scrollHeight }); }, [messages, busy]);
  const actions = useMemo(() => Object.fromEntries(shape.actions.map((action) => [action.client_type, action])), [shape.actions]);

  async function send(spoken?: string) {
    const message = (spoken ?? input).trim();
    if (!message || sending.current) return;
    sending.current = true;
    stopAudio();
    const controller = new AbortController();
    request.current = controller;
    setInput("");
    setBusy(true);
    setMessages((all) => [...all, { role: "visitor", text: message }]);
    try {
      turn.current += 1;
      const response = await sendProductTurn(session, {
        sessionId: sessionId.current,
        turnId: turn.current,
        productId,
        message,
        signal: controller.signal,
      });
      if (!alive.current || controller.signal.aborted) return;
      if (response.session_id !== sessionId.current || response.turn_id !== turn.current) {
        throw new Error("The reply did not match this conversation. Please retry.");
      }
      if (response.status === "stale" || response.status === "cancelled") return;
      setMessages((all) => [...all, { role: "agent", text: response.speech }]);
      const action = response.status === "completed" && response.validated_action ? actions[response.validated_action.type] : null;
      if (response.validated_action && !response.execution) {
        // A place in the application is somewhere to go; anything else is for the screen showing
        // this product. An action this product does not declare came from Pixel itself, which
        // happens when somebody asks to leave the product they are in.
        const payload = response.validated_action.payload as { view?: string };
        const view = action?.capability === "NAVIGATE_VIEW" ? action.view : payload.view;
        const route = typeof view === "string" ? CONSOLE_ROUTES[view] : undefined;
        if (route && (!action || action.capability === "NAVIGATE_VIEW")) router.push(route);
        else if (action) onUiAction?.(action, response.validated_action.payload);
      }
      const receipt = await maybeExecute(session, productId, response, actions);
      if (!alive.current) return;
      if (receipt) {
        setMessages((all) => [...all, { role: "agent", text: receipt.speech }]);
        await onRecordsChanged?.();
        if (action && receipt.outcome === "executed" && typeof receipt.record?.id === "string") {
          onUiAction?.(action, { record_id: receipt.record.id });
        }
      }
      if (voiceEnabled.current && alive.current) {
        setVoiceStatus("Preparing audio");
        const speechController = new AbortController();
        speechRequest.current = speechController;
        try {
          const result = await productSpeech(session, productId, sessionId.current, receipt?.speech ?? response.speech, speechController.signal);
          if (!alive.current || speechController.signal.aborted) return;
          const url = URL.createObjectURL(result.audio);
          audioUrl.current = url;
          const audio = new Audio(url);
          playing.current = audio;
          audio.onended = () => { stopAudio(); if (alive.current) setVoiceStatus("Voice ready"); };
          await audio.play();
          setVoiceStatus(result.provider);
        } catch (caught) {
          if (alive.current && !speechController.signal.aborted) setVoiceStatus(caught instanceof Error ? caught.message : "Voice unavailable");
        }
      }
    } catch (caught) {
      if (alive.current && !controller.signal.aborted) setMessages((all) => [...all, { role: "agent", text: caught instanceof Error ? caught.message : "That request failed." }]);
    } finally {
      sending.current = false;
      if (alive.current) setBusy(false);
    }
  }

  function microphone() {
    if (listening) { recognition.current?.abort(); setListening(false); return; }
    const Constructor = speechInputConstructor();
    if (!Constructor || busy) return;
    stopAudio();
    const capture = new Constructor();
    recognition.current = capture;
    capture.lang = "en-US"; capture.continuous = false; capture.interimResults = false;
    capture.onresult = (event) => {
      if (!alive.current) return;
      const transcript = event.results[0]?.[0]?.transcript?.trim();
      if (transcript) { voiceEnabled.current = true; setVoice(true); void send(transcript); }
    };
    capture.onerror = (event) => { if (alive.current) setVoiceStatus(event.error === "not-allowed" ? "Microphone permission denied" : "Speech input unavailable. Please type your message."); };
    capture.onend = () => { if (alive.current) setListening(false); };
    try { capture.start(); setListening(true); }
    catch { setVoiceStatus("Microphone unavailable. Please type your message."); }
  }

  return (
    <aside className="px-product-assistant" data-scope={scope} data-collapsed={collapsed}
      aria-label={scope === "platform"
        ? `${shape.assistant_name}, your assistant across Pixel`
        : `${shape.assistant_name}, answering for ${shape.product_name}`}>
    <Panel title={<span className="px-row"><Bot aria-hidden size={16} />{collapsed ? null : shape.assistant_name}</span>}
      actions={<Button aria-label={collapsed ? "Expand assistant" : "Collapse assistant"} title={collapsed ? "Expand assistant" : "Collapse assistant"}
        variant="ghost" size="sm" onClick={() => setCollapsed(!collapsed)}>{collapsed ? <PanelLeftOpen /> : <PanelLeftClose />}</Button>}>
      {!collapsed ? <>
      <div className="px-row px-small px-muted" style={{ justifyContent: "space-between" }}>
        <span>{scope === "platform" ? "Across Pixel" : shape.product_name}</span>
        <span role="status">{busy ? "Thinking" : voice ? voiceStatus : "Ready"}</span>
      </div>
      <div className="px-stack">
        <div ref={log} className="px-chat-log" role="log" aria-live="polite" aria-label="Conversation">
          {messages.map((message, index) => (
            <div key={index} className="px-console-message" data-role={message.role}>
              <strong>{message.role === "visitor" ? "You" : shape.assistant_name}</strong>
              <p>{message.text}</p>
            </div>
          ))}
          {busy ? <p className="px-muted" role="status">Thinking...</p> : null}
        </div>
        {messages.length <= 1 ? (
          <div className="px-row" style={{ flexWrap: "wrap", gap: 6 }}>
            {(scope === "platform" ? PLATFORM_PROMPTS : PRODUCT_PROMPTS).map((prompt) => (
              <Button key={prompt} size="sm" disabled={busy} onClick={() => void send(prompt)}>{prompt}</Button>
            ))}
          </div>
        ) : null}
        <form className="px-row" onSubmit={(event) => { event.preventDefault(); void send(); }}>
          <Input value={input} onChange={(event) => setInput(event.target.value)}
            placeholder={`Ask ${shape.assistant_name}`} aria-label={`Ask ${shape.assistant_name}`} />
          <Button type="submit" variant="primary" loading={busy} disabled={!input.trim()} aria-label="Send message" title="Send message"><SendHorizonal aria-hidden /></Button>
        </form>
        <div className="px-row">
          <Button size="sm" variant="ghost" disabled={busy || !micAvailable} aria-label={listening ? "Stop microphone" : "Start microphone"}
            title={!micAvailable ? "Speech input is not supported in this browser" : listening ? "Stop microphone" : "Start microphone"} onClick={microphone}>
            {listening ? <Square aria-hidden /> : <Mic aria-hidden />}
          </Button>
          <Button size="sm" variant="ghost" aria-label={voice ? "Disable voice" : "Enable voice"} title={voice ? "Disable voice" : "Enable voice"}
            aria-pressed={voice} onClick={() => { stopAudio(); voiceEnabled.current = !voice; setVoice(!voice); setVoiceStatus(voice ? "Voice off" : "Voice ready"); }}>
            {voice ? <Volume2 aria-hidden /> : <VolumeX aria-hidden />}
          </Button>
          <span className="px-small" role="status">{voiceStatus}</span>
          <Button size="sm" variant="ghost" disabled={busy} aria-label="New conversation" title="New conversation" onClick={() => {
            stopAudio(); sessionId.current = crypto.randomUUID(); turn.current = 0;
            setMessages([{ role: "agent", text: `Welcome to ${shape.product_name}. I'm ${shape.assistant_name}.` }]);
          }}><RotateCcw aria-hidden /></Button>
        </div>
      </div>
      </> : null}
    </Panel>
    </aside>
  );
}

async function maybeExecute(
  session: ApiSession,
  productId: string,
  response: ApiTurnResponse,
  actions: Record<string, ApiActionShape>,
) {
  if (!response.execution || !response.validated_action) return null;
  if (response.status !== "completed" || response.execution.turn_id !== response.turn_id || response.execution.session_id !== response.session_id) return null;
  const action = actions[response.validated_action.type];
  if (!action?.entity) return null;
  if (action.capability !== "CREATE_RECORD" && action.capability !== "UPDATE_RECORD") return null;
  return executeProductAction(session, {
    productId,
    entity: action.entity,
    action: action.name,
    payload: response.validated_action.payload,
    executionKey: response.execution.key,
    sessionId: response.execution.session_id,
  });
}
