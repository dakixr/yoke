export function sessionStatusDescriptor({ runtime, attention, done, queue, age }) {
  const attentionCount = (attention?.permissions || 0) + (attention?.questions || 0);
  if (attentionCount) {
    return {
      kind: "attention",
      label: attentionCount === 1 ? "Action required" : `${attentionCount} actions required`,
    };
  }
  if (runtime?.state === "waiting_input") return { kind: "attention", label: "Waiting for you" };
  if (runtime?.state === "error") return { kind: "error", label: "Error" };
  if (runtime?.state === "stopping") return { kind: "quiet", label: "Stopping" };
  if (runtime?.state === "running") return { kind: "working", label: "Working" };
  const pending = queueStatusLabel(queue);
  if (pending) return { kind: "quiet", label: pending };
  if (done) return { kind: "done", label: "Done" };
  return { kind: "quiet", label: age || "now" };
}

export function queueStatusLabel(queue) {
  const parts = [];
  const steering = Math.max(0, Number(queue?.steering) || 0);
  const queued = Math.max(0, Number(queue?.queued) || 0);
  const paused = Math.max(0, Number(queue?.paused) || 0);
  if (steering) parts.push(`${steering} steer`);
  if (queued) parts.push(`${queued} queued`);
  if (paused) parts.push(`${paused} paused`);
  return parts.join(" · ");
}

export function hasPendingQueue(queue) {
  return Math.max(0, Number(queue?.total) || 0) > 0;
}

export function connectionStatusDescriptor(connection) {
  if (connection?.current) return { kind: "connected", label: "Connected" };
  if (connection?.status === "resyncing") return { kind: "syncing", label: "Synchronizing" };
  if (connection?.status === "connecting") return { kind: "syncing", label: "Connecting" };
  if (connection?.status === "connected") return { kind: "syncing", label: "Synchronizing" };
  if (connection?.status === "auth") return { kind: "disconnected", label: "Authentication required" };
  return { kind: "disconnected", label: "Disconnected" };
}
