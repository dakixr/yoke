// @ts-check

import { errorMessage } from "../lib/errors.js";
import { retireSession } from "./session-machine.js";
import { store } from "./store.js";

export class BrowserLifecycle {
  constructor({ notice, resetConnection, clearBuffers, retireOwners }) {
    this.notice = notice;
    this.resetConnection = resetConnection;
    this.clearBuffers = clearBuffers;
    this.retireOwners = retireOwners;
    this.epoch = 0;
    this.scheduler = new OwnedScheduler(this);
  }

  owns(epoch) {
    return this.epoch === epoch;
  }

  retire({ stopStream = false } = {}) {
    this.epoch += 1;
    this.resetConnection({ stopStream });
    store.setState((state) => ({ ...state,
      active: Object.fromEntries(Object.entries(state.active).filter(([, runtime]) => !runtime.optimistic)),
      sessionData: Object.fromEntries(
      Object.entries(state.sessionData).map(([id, data]) => [id, retireSession(data)]),
    ) }));
    this.clearBuffers();
    this.scheduler.cancel();
    this.retireOwners();
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
      if (this.lifecycle.owns(epoch)) this.lifecycle.notice(errorMessage(error));
    }
  }
}
