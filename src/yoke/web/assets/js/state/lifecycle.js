// @ts-check

import { ApiError } from "../api/client.js";

export class BrowserLifecycle {
  constructor(controller) {
    this.controller = controller;
    this.epoch = 0;
    this.scheduler = new OwnedScheduler(this);
  }

  owns(epoch) {
    return this.epoch === epoch;
  }

  retire({ stopStream = false } = {}) {
    const controller = this.controller;
    this.epoch += 1;
    if (stopStream) controller.sse?.stop();
    if (stopStream) controller.sse = null;
    controller.bootstrapping = false;
    controller.resyncing = false;
    controller.bootstrapEpoch = null;
    controller.resyncEpoch = null;
    controller.broadResyncPending = false;
    controller.bufferEvents = false;
    controller.eventBuffer = [];
    this.scheduler.cancel();
    if (controller.liveFrame !== null) cancelAnimationFrame(controller.liveFrame);
    controller.liveFrame = null;
    controller.pendingLiveEvents.clear();
    for (const map of mutationOwnershipMaps(controller)) map.clear();
    controller.inspectorState.invalidateLifecycle();
  }
}

class OwnedScheduler {
  constructor(lifecycle) {
    this.lifecycle = lifecycle;
    this.timers = new Map();
    this.tasks = new Set();
  }

  schedule(key, delay, task) {
    clearTimeout(this.timers.get(key));
    this.arm(key, delay, task);
  }

  throttle(key, delay, task) {
    if (!this.timers.has(key)) this.arm(key, delay, task);
  }

  cancel() {
    for (const timer of this.timers.values()) clearTimeout(timer);
    this.timers.clear();
  }

  arm(key, delay, task) {
    const epoch = this.lifecycle.epoch;
    const timer = setTimeout(async () => {
      this.timers.delete(key);
      if (!this.lifecycle.owns(epoch)) return;
      const running = this.run(task, epoch);
      this.tasks.add(running);
      try { await running; } finally { this.tasks.delete(running); }
    }, delay);
    this.timers.set(key, timer);
  }

  async run(task, epoch) {
    try {
      await task();
    } catch (error) {
      if (this.lifecycle.owns(epoch)) this.lifecycle.controller.notice(errorMessage(error));
    }
  }
}

function mutationOwnershipMaps(controller) {
  return [
    controller.messageRefreshGeneration,
    controller.liveToolRefreshGeneration,
    controller.optimisticSessionGeneration,
    controller.sessionMutationChains,
    controller.sessionPendingMutations,
    controller.queueMutationChains,
    controller.queueServerRevisions,
    controller.queueMutationGeneration,
    controller.queuePendingMutations,
    controller.humanInputGeneration,
    controller.selectionGeneration,
    controller.selectionMutationChains,
    controller.pendingSelections,
    controller.toolMutationChains,
    controller.toolMutationGeneration,
    controller.mcpMutationChains,
    controller.mcpMutationGeneration,
  ];
}

function errorMessage(error) {
  if (error instanceof ApiError) {
    return `${error.message}${error.requestID ? ` (${error.requestID})` : ""}`;
  }
  return error?.message || String(error);
}
