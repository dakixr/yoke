---
name: yoke-session-resume
description: Resume or continue work from a Yoke session id by loading its portable handoff. Use when the user asks to continue, resume, or pick up work from a Yoke session id.
---

# Resume a Yoke session

1. Extract the exact Yoke session id from the user's request.
2. Run `yoke session-handoff <session-id>`.
3. Treat the returned handoff as prior work context. If it lists active skills
   that are available now, load them before continuing the user's current
   request from the handoff's active branch and working directory.
4. If the command fails, surface that error. Use raw session persistence only
   when the task is explicitly about session storage or persistence debugging.

Completion means the requested work continues from the handoff rather than from
an inferred or manually reconstructed transcript.
