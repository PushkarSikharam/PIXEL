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
import {
  SERVER_UNAVAILABLE, serverUnavailable,
  closeConversation, executeProductAction, sendProductTurn, productSpeech,
  type ApiActionShape, type ApiProductShape, type ApiSession, type ApiTurnResponse,
} from "@pixel-console/lib/pixel-api";
import { speechInputConstructor, type SpeechInput } from "@pixel-console/lib/speech-input";
import { CONSOLE_ROUTES, consoleRecordRoute } from "@pixel-console/lib/console-routes";

/**
 * A short route through whichever product is answering, drawn from what that product declares.
 *
 * The demo shows somebody five things to try, and it is the clearest part of it. The same thing
 * here cannot be a list written in this file, because the product answering might have been added
 * this morning: the steps are its own navigable actions, in the order its definition declares
 * them, which is the order its author meant somebody to meet them in.
 */
function starterSteps(shape: ApiProductShape): string[] {
  const places = shape.actions
    .filter((action) => action.capability === "NAVIGATE_VIEW")
    .map((action) => action.description.replace(/\.$/, "").trim())
    .filter(Boolean);
  return Array.from(new Set(places)).slice(0, 4);
}

/**
 * Questions worth offering, in this product's own words.
 *
 * Each one is a message somebody could have typed, phrased from the product's own name and its
 * own labels, so a chip can never ask for something the assistant has no idea about.
 */
function starterPrompts(shape: ApiProductShape, scope: "platform" | "product"): string[] {
  if (scope === "platform") {
    return [
      "What is Pixel?",
      "Help me add a product",
      "How many products do I have?",
      "Open demo mode",
      "Show me around",
    ];
  }
  const things = shape.entities.slice(0, 2).map((entity) => entity.plural.toLowerCase());
  return [
    "What can you do?",
    ...things.map((plural) => `How many ${plural} are there?`),
    `What does ${shape.product_name} know?`,
    "Show me around",
  ];
}

function openingMessage(shape: ApiProductShape, scope: "platform" | "product"): string {
  if (scope === "platform") {
    return `Welcome to Pixel. I'm ${shape.assistant_name}. I can help you add products, open your workspace, answer approved Pixel questions, and take you to the guided demo.`;
  }
  return `Welcome to ${shape.product_name}. I'm ${shape.assistant_name}. Ask me to open screens, count records, create records, or explain what this product knows.`;
}

export function EdithPanel({
  session, shape, productId, currentPage = null, selectedRecordId = null,
  scope = "product", onRecordsChanged, onUiAction,
}: {
  session: ApiSession;
  shape: ApiProductShape;
  productId: string;
  currentPage?: string | null;
  selectedRecordId?: string | null;
  /** Whether this panel is answering for Pixel itself or for a product inside it. */
  scope?: "platform" | "product";
  onRecordsChanged?: () => Promise<void>;
  onUiAction?: (action: ApiActionShape, payload: Record<string, unknown>) => void;
}) {
  const router = useRouter();
  const [messages, setMessages] = useState<Array<{ role: "visitor" | "agent"; text: string }>>([
    { role: "agent", text: openingMessage(shape, scope) },
  ]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const [voice, setVoice] = useState(false);
  const voiceEnabled = useRef(false);
  const [voiceStatus, setVoiceStatus] = useState("Voice off");
  const [listening, setListening] = useState(false);
  const [micAvailable, setMicAvailable] = useState(false);
  // Why voice input did not start, shown whether or not spoken replies are on: a press that
  // silently does nothing reads as a broken button.
  const [micProblem, setMicProblem] = useState<string | null>(null);
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
    return () => {
      alive.current = false;
      request.current?.abort();
      recognition.current?.abort();
      stopAudio();
      // Leaving takes the conversation with it, including anything it proposed and nobody did.
      // A conversation nobody spoke in does not exist on the server, so there is nothing to close.
      if (turn.current > 0) void closeConversation(session, sessionId.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => { log.current?.scrollTo({ top: log.current.scrollHeight }); }, [messages, busy]);
  // On a phone the assistant is a full screen of its own, so opening a page would mean scrolling
  // past all of it to reach the page. It starts folded there and opens when somebody asks for it.
  // Set after mount, so the server and the browser render the same thing first.
  useEffect(() => {
    if (typeof window !== "undefined" && window.matchMedia("(max-width: 1000px)").matches) {
      setCollapsed(true);
    }
  }, []);
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
        currentPage,
        selectedRecordId,
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
        const payload = response.validated_action.payload as { view?: string; record_id?: string };
        const view = action?.capability === "NAVIGATE_VIEW" ? action.view : payload.view;
        const route = typeof view === "string" ? CONSOLE_ROUTES[view] : undefined;
        // One of Pixel's own records is a place too: asked for a product by name, Pixel opens
        // that product rather than the list it appears in. Inside a product the same reply is
        // about that product's own records, which belong to the screen showing them.
        const record = scope === "platform" && action?.capability === "OPEN_RECORD" && action.entity
          ? consoleRecordRoute(action.entity, String(payload.record_id ?? ""))
          : null;
        if (record) router.push(record);
        else if (route && (!action || action.capability === "NAVIGATE_VIEW")) router.push(route);
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
      if (alive.current && !controller.signal.aborted) setMessages((all) => [...all, { role: "agent", text:
        serverUnavailable(caught) ? `${SERVER_UNAVAILABLE} Your message was not sent. Please try again in a moment.`
          : caught instanceof Error ? caught.message : "That request failed." }]);
    } finally {
      sending.current = false;
      if (alive.current) setBusy(false);
    }
  }

  function microphone() {
    if (listening) { recognition.current?.abort(); setListening(false); return; }
    const Constructor = speechInputConstructor();
    if (!Constructor || busy) return;
    setMicProblem(null);
    stopAudio();
    const capture = new Constructor();
    recognition.current = capture;
    capture.lang = "en-US"; capture.continuous = false; capture.interimResults = false;
    capture.onresult = (event) => {
      if (!alive.current) return;
      const transcript = event.results[0]?.[0]?.transcript?.trim();
      if (transcript) { voiceEnabled.current = true; setVoice(true); void send(transcript); }
    };
    capture.onerror = (event) => {
      if (!alive.current) return;
      const problem = event.error === "not-allowed" ? "Microphone permission denied" : "Speech input unavailable. Please type your message.";
      setVoiceStatus(problem);
      setMicProblem(problem);
    };
    capture.onend = () => { if (alive.current) setListening(false); };
    try { capture.start(); setListening(true); }
    catch {
      setVoiceStatus("Microphone unavailable. Please type your message.");
      setMicProblem("Microphone unavailable. Please type your message.");
    }
  }

  const steps = useMemo(() => starterSteps(shape), [shape]);
  const prompts = useMemo(() => starterPrompts(shape, scope), [shape, scope]);
  const kicker = scope === "platform" ? "Pixel" : shape.product_name;
  const state = busy ? "Thinking" : listening ? "Listening" : voice ? voiceStatus : "Ready";

  function restart() {
    stopAudio();
    recognition.current?.abort();
    if (turn.current > 0) void closeConversation(session, sessionId.current);
    sessionId.current = crypto.randomUUID();
    turn.current = 0;
    setMessages([{ role: "agent", text: openingMessage(shape, scope) }]);
  }

  return (
    <aside className="px-product-assistant px-edith" data-scope={scope} data-collapsed={collapsed}
      aria-label={scope === "platform"
        ? `${shape.assistant_name}, your assistant across ${shape.product_name}`
        : `${shape.assistant_name}, answering for ${shape.product_name}`}>

      <div className="px-edith-topbar">
        <span className="px-edith-brand">
          <span className="px-edith-mark" aria-hidden>P</span>
          {collapsed ? <span className="px-edith-folded-name">Ask {shape.assistant_name}</span> : kicker}
        </span>
        <span className="px-edith-controls">
          <button type="button" className="px-edith-chip" onClick={() => setCollapsed(!collapsed)}
            aria-expanded={!collapsed}
            aria-label={collapsed ? `Show ${shape.assistant_name}` : `Hide ${shape.assistant_name}`}>
            {collapsed ? "Show" : "Hide"}</button>
          {collapsed ? null : <>
            <button type="button" className="px-edith-chip" disabled={busy || messages.length <= 1} onClick={restart}
              aria-label="Restart this conversation"
              title={messages.length <= 1 ? "Nothing to restart yet" : undefined}>Restart</button>
            <span className="px-edith-state" role="status">{state}</span>
          </>}
        </span>
      </div>

      {collapsed ? null : <>
        <div className="px-edith-intro">
          <div className="px-edith-title-row">
            <div>
              <p className="px-edith-kicker">{scope === "platform" ? "Your guide to Pixel" : "Product guide"}</p>
              <h2>{shape.assistant_name}</h2>
            </div>
            {voice ? <span className="px-edith-voice-badge">Voice</span> : null}
          </div>
        </div>

        <div ref={log} className="px-edith-transcript" role="log" aria-live="polite"
          aria-label="Conversation">
          {messages.map((message, index) => (
            <article key={index} className="px-edith-message" data-role={message.role}>
              <span>{message.role === "visitor" ? "You" : "Agent"}</span>
              <p>{message.text}</p>
            </article>
          ))}
        </div>

        {steps.length ? (
          <div className="px-edith-path" aria-label={`Where to start in ${shape.product_name}`}>
            <div className="px-edith-path-header">
              <span>Where to start</span>
              <strong>{steps.length} steps</strong>
            </div>
            <div className="px-edith-path-list">
              {steps.map((step, index) => (
                <button key={step} type="button" disabled={busy} onClick={() => void send(step)}>
                  <i aria-hidden>{index + 1}</i>
                  {step}
                </button>
              ))}
            </div>
          </div>
        ) : null}

        <div className="px-edith-prompts" aria-label="Suggested questions">
          {prompts.map((prompt) => (
            <button key={prompt} type="button" disabled={busy} onClick={() => void send(prompt)}>{prompt}</button>
          ))}
        </div>

        <form className="px-edith-form" onSubmit={(event) => { event.preventDefault(); void send(); }}>
          <span className="px-edith-input-mark" aria-hidden>P</span>
          <input value={input} onChange={(event) => setInput(event.target.value)}
            placeholder={`Ask ${shape.assistant_name}`} aria-label={`Ask ${shape.assistant_name}`} />
          <button type="submit" disabled={busy || !input.trim()} aria-label="Send message">Send</button>
        </form>

        <div className="px-edith-voice" data-active={listening || voice}>
          <div className="px-edith-voice-top">
            <button type="button" className="px-edith-mic" data-listening={listening}
              aria-pressed={listening} disabled={busy || !micAvailable} onClick={microphone}
              aria-label={listening ? "End Voice input" : "Start Voice input"}
              title={micAvailable ? undefined : "Speech input is not supported in this browser"}>
              <span className="px-edith-dot" aria-hidden />
              {listening ? "End Voice" : "Start Voice"}
            </button>
            <button type="button" className="px-edith-tts" data-on={voice} aria-pressed={voice}
              onClick={() => {
                stopAudio();
                voiceEnabled.current = !voice;
                setVoice(!voice);
                setVoiceStatus(voice ? "Voice off" : "Voice ready");
              }}
              aria-label={voice ? "Voice On: turn spoken replies off" : "Voice Off: turn spoken replies on"}>
              {voice ? "Voice On" : "Voice Off"}
            </button>
          </div>
          <div className="px-edith-voice-status">
            <span>Voice: <strong>{micProblem && !listening ? micProblem : voice || listening ? voiceStatus : "Off"}</strong></span>
            <span className="px-edith-spectrum" data-animating={listening || busy}
              aria-hidden><i /><i /><i /><i /><i /></span>
          </div>
        </div>
      </>}
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
