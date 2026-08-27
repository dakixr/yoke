// @ts-check

function copySessionData(state, sessionID) {
  return { ...(state.sessionData[sessionID] || {}) };
}

function nextLiveSequence(data) {
  const next = (data.liveTimelineSequence || 0) + 1;
  data.liveTimelineSequence = next;
  return next;
}

function assistantEventKey(event) {
  const turnID = event.data?.turnID ?? "turn";
  const iteration = event.data?.iteration ?? "iteration";
  const phase = event.data?.phase || "assistant";
  return `${turnID}:${iteration}:${phase}`;
}

function toolCallID(event) {
  return event.data?.tool_call_id || event.data?.toolCallID || null;
}

export function mergeSessionSummary(state, session) {
  return {
    ...state,
    sessions: { ...state.sessions, [session.id]: session },
  };
}

export function installActiveSnapshot(state, active) {
  const done = { ...state.ui.doneUnreviewed };
  for (const [sessionID, previous] of Object.entries(state.active)) {
    const current = active[sessionID];
    if (
      previous?.state && previous.state !== "idle" &&
      (!current || current.state === "idle") &&
      state.ui.selectedSessionID !== sessionID
    ) {
      done[sessionID] = true;
    }
  }
  return {
    ...state,
    active,
    ui: { ...state.ui, doneUnreviewed: done },
  };
}

export function reducePublicEvent(state, event) {
  const sessionID = event.sessionID || null;
  let next = state;
  if (sessionID && event.durable?.seq) {
    const sessionData = copySessionData(next, sessionID);
    sessionData.latestSeq = Math.max(sessionData.latestSeq || 0, event.durable.seq);
    next = {
      ...next,
      sessionData: { ...next.sessionData, [sessionID]: sessionData },
    };
  }

  if (event.type === "server.connected") {
    return {
      ...next,
      connection: {
        ...next.connection,
        status: "connected",
        serverInstanceID: event.data?.serverInstanceID || null,
      },
    };
  }
  if (!sessionID) return next;

  if (event.type === "session.active.changed") {
    const previous = next.active[sessionID];
    const current = {
      state: event.data?.state || "idle",
      turnID: event.data?.turnID ?? null,
      startedAt: event.data?.startedAt ?? null,
      error: event.data?.error ?? null,
      activity: event.data?.activity ?? null,
    };
    const done = { ...next.ui.doneUnreviewed };
    if (
      previous?.state && previous.state !== "idle" && current.state === "idle" &&
      next.ui.selectedSessionID !== sessionID
    ) {
      done[sessionID] = true;
    }
    return {
      ...next,
      active: { ...next.active, [sessionID]: current },
      ui: { ...next.ui, doneUnreviewed: done },
    };
  }

  const data = copySessionData(next, sessionID);
  if (event.type === "session.permission.requested") {
    const current = data.permissions || [];
    data.permissions = [
      ...current.filter((item) => item.id !== event.data?.id),
      event.data,
    ];
    next = {
      ...next,
      attention: {
        ...next.attention,
        [sessionID]: {
          permissions: data.permissions.length,
          questions: next.attention[sessionID]?.questions || 0,
        },
      },
    };
  } else if (event.type === "session.permission.resolved") {
    data.permissions = (data.permissions || []).filter(
      (item) => item.id !== event.data?.requestID,
    );
    next = {
      ...next,
      attention: {
        ...next.attention,
        [sessionID]: {
          permissions: data.permissions.length,
          questions: next.attention[sessionID]?.questions || 0,
        },
      },
    };
  } else if (event.type === "session.question.requested") {
    const current = data.questions || [];
    data.questions = [
      ...current.filter((item) => item.id !== event.data?.id),
      event.data,
    ];
    next = {
      ...next,
      attention: {
        ...next.attention,
        [sessionID]: {
          permissions: next.attention[sessionID]?.permissions || 0,
          questions: data.questions.length,
        },
      },
    };
  } else if (event.type === "session.question.resolved") {
    data.questions = (data.questions || []).filter(
      (item) => item.id !== event.data?.requestID,
    );
    next = {
      ...next,
      attention: {
        ...next.attention,
        [sessionID]: {
          permissions: next.attention[sessionID]?.permissions || 0,
          questions: data.questions.length,
        },
      },
    };
  } else if (event.type === "session.runtime.failed") {
    data.lastError = event.data?.error || "Agent execution failed.";
  } else if (event.type === "session.prompt.admitted") {
    const inputID = event.data?.inputID;
    if (inputID) {
      data.pendingPrompts = {
        ...(data.pendingPrompts || {}),
        [inputID]: {
          id: inputID,
          prompt: event.data?.prompt || { text: "", attachments: [] },
          delivery: event.data?.delivery || "steer",
          timeCreated: event.time || null,
        },
      };
    }
  } else if (event.type === "session.prompt.edited") {
    const inputID = event.data?.inputID;
    if (inputID && data.pendingPrompts?.[inputID]) {
      data.pendingPrompts = {
        ...data.pendingPrompts,
        [inputID]: {
          ...data.pendingPrompts[inputID],
          prompt: event.data?.prompt || data.pendingPrompts[inputID].prompt,
          delivery: event.data?.delivery || data.pendingPrompts[inputID].delivery,
        },
      };
    }
  } else if (event.type === "session.prompt.removed") {
    const inputID = event.data?.inputID;
    if (inputID && data.pendingPrompts?.[inputID]) {
      const pendingPrompts = { ...data.pendingPrompts };
      delete pendingPrompts[inputID];
      data.pendingPrompts = pendingPrompts;
    }
  } else if (event.type === "session.prompt.promoted") {
    const inputID = event.data?.inputID;
    const admitted = inputID ? data.pendingPrompts?.[inputID] : null;
    if (admitted) {
      data.livePrompt = admitted;
      data.lastError = null;
      const pendingPrompts = { ...data.pendingPrompts };
      delete pendingPrompts[inputID];
      data.pendingPrompts = pendingPrompts;
    }
  } else if (event.type === "session.message.updated" && !event.durable) {
    const key = assistantEventKey(event);
    const current = data.liveAssistants?.[key];
    data.liveAssistants = {
      ...(data.liveAssistants || {}),
      [key]: {
        id: key,
        sequence: current?.sequence ?? nextLiveSequence(data),
        iteration: event.data?.iteration ?? null,
        timeCreated: current?.timeCreated || event.time || null,
        phase: event.data?.phase || null,
        content: event.data?.content || "",
        turnID: event.data?.turnID ?? null,
      },
    };
  } else if (event.type === "session.message.updated" && event.durable) {
    data.livePrompt = null;
    data.lastError = null;
  } else if (event.type === "session.tool.started") {
    const callID = toolCallID(event);
    if (callID) {
      const current = data.liveTools?.[callID];
      data.liveTools = {
        ...(data.liveTools || {}),
        [callID]: {
          callID,
          sequence: current?.sequence ?? nextLiveSequence(data),
          status: "running",
          name: event.data?.tool_name || event.data?.toolName || current?.name || "tool",
          arguments: event.data?.tool_arguments || event.data?.toolArguments || current?.arguments || "",
          iteration: event.data?.iteration ?? current?.iteration ?? null,
          turnID: event.data?.turnID ?? current?.turnID ?? null,
          startedAt: current?.startedAt || event.time || null,
          endedAt: null,
          ok: null,
        },
      };
    }
  } else if (event.type === "session.tool.ended") {
    const callID = toolCallID(event);
    if (callID) {
      const current = data.liveTools?.[callID];
      const cancelled = Boolean(event.data?.result?.cancelled);
      data.liveTools = {
        ...(data.liveTools || {}),
        [callID]: {
          callID,
          sequence: current?.sequence ?? nextLiveSequence(data),
          status: cancelled ? "cancelled" : event.data?.ok === false ? "failed" : "completed",
          name: event.data?.tool_name || event.data?.toolName || current?.name || "tool",
          arguments: current?.arguments || "",
          iteration: event.data?.iteration ?? current?.iteration ?? null,
          turnID: event.data?.turnID ?? current?.turnID ?? null,
          startedAt: current?.startedAt || null,
          endedAt: event.time || null,
          ok: event.data?.ok ?? null,
        },
      };
    }
  } else if (event.type === "session.context.updated") {
    data.contextUsage = event.data;
  }
  return {
    ...next,
    sessionData: { ...next.sessionData, [sessionID]: data },
  };
}
