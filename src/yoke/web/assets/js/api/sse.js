// @ts-check

export class SseClient {
  constructor({ headers, onEvent, onState }) {
    this.headers = headers;
    this.onEvent = onEvent;
    this.onState = onState;
    this.abortController = null;
    this.stopped = false;
    this.retryMs = 700;
  }

  start() {
    this.stopped = false;
    void this.loop();
  }

  stop() {
    this.stopped = true;
    this.abortController?.abort();
    this.abortController = null;
  }

  async loop() {
    while (!this.stopped) {
      this.abortController = new AbortController();
      try {
        this.onState?.("connecting", null);
        const response = await fetch("/api/v1/event", {
          headers: this.headers(),
          signal: this.abortController.signal,
        });
        if (!response.ok || !response.body) {
          const error = new Error(`Event stream returned ${response.status}`);
          error.status = response.status;
          throw error;
        }
        this.retryMs = 700;
        this.onState?.("connected", null);
        await parseEventStream(response.body, this.onEvent);
        if (!this.stopped) this.onState?.("disconnected", "Event stream closed");
      } catch (error) {
        if (this.stopped || error?.name === "AbortError") return;
        this.onState?.("disconnected", error?.message || String(error));
      }
      if (this.stopped) return;
      await delay(this.retryMs);
      this.retryMs = Math.min(5000, Math.round(this.retryMs * 1.7));
    }
  }
}

async function parseEventStream(stream, onEvent) {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let boundary;
      while ((boundary = buffer.indexOf("\n\n")) >= 0) {
        const block = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const parsed = parseBlock(block);
        if (parsed) onEvent(parsed);
      }
    }
  } finally {
    reader.releaseLock();
  }
}

function parseBlock(block) {
  let eventType = "message";
  const data = [];
  for (const rawLine of block.split(/\r?\n/)) {
    const line = rawLine.trimEnd();
    if (!line || line.startsWith(":")) continue;
    const split = line.indexOf(":");
    const field = split < 0 ? line : line.slice(0, split);
    const value = split < 0 ? "" : line.slice(split + 1).replace(/^ /, "");
    if (field === "event") eventType = value;
    if (field === "data") data.push(value);
  }
  if (!data.length) return null;
  try {
    const payload = JSON.parse(data.join("\n"));
    if (!payload.type) payload.type = eventType;
    return payload;
  } catch {
    return null;
  }
}

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
