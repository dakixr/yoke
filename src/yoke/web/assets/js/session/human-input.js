import { html, useState } from "../../vendor/htm-preact.js";
import { controller } from "../state/controller.js";

export function HumanInput({ sessionID, permissions = [], questions = [] }) {
  if (!permissions.length && !questions.length) return null;
  const count = permissions.length + questions.length;
  return html`<section class="human-input" aria-live="polite">
    <div class="human-input__heading"><strong>Required actions</strong><span>${count} remaining</span></div>
    ${permissions.map((request) => html`<${PermissionRequest} key=${request.id} sessionID=${sessionID} request=${request} />`)}
    ${questions.map((request) => html`<${QuestionRequest} key=${request.id} sessionID=${sessionID} request=${request} />`)}
  </section>`;
}

function PermissionRequest({ sessionID, request }) {
  const [message, setMessage] = useState("");
  return html`<div class="attention-panel">
    <div class="attention-panel__eyebrow">Permission required</div>
    <div class="attention-panel__title">${request.permission}</div>
    <div class="attention-panel__text">${request.message}</div>
    <input value=${message} placeholder="Optional reply" aria-label="Optional permission reply" onInput=${(event) => setMessage(event.currentTarget.value)} />
    <div class="attention-panel__actions">
      <button class="primary small" onClick=${() => controller.replyPermission(sessionID, request.id, "allow", message)}>Allow</button>
      <button onClick=${() => controller.replyPermission(sessionID, request.id, "deny", message)}>Deny</button>
    </div>
  </div>`;
}

function QuestionRequest({ sessionID, request }) {
  const [answers, setAnswers] = useState([]);
  const [custom, setCustom] = useState("");
  const toggle = (answer) => {
    if (!request.multiple) setAnswers([answer]);
    else setAnswers((current) => current.includes(answer) ? current.filter((value) => value !== answer) : [...current, answer]);
  };
  const resolved = request.options?.length ? answers : custom.trim() ? [custom.trim()] : [];
  return html`<div class="attention-panel">
    <div class="attention-panel__eyebrow">Question</div>
    <div class="attention-panel__title">${request.question}</div>
    ${request.options?.length ? html`<div class="question-options">
      ${request.options.map((option) => html`<label><input type=${request.multiple ? "checkbox" : "radio"} name=${request.id} checked=${answers.includes(option)} onChange=${() => toggle(option)} /> <span>${option}</span></label>`)}
    </div>` : html`<input value=${custom} placeholder="Your answer" aria-label="Question answer" onInput=${(event) => setCustom(event.currentTarget.value)} />`}
    <div class="attention-panel__actions">
      <button class="primary small" disabled=${!resolved.length} onClick=${() => controller.replyQuestion(sessionID, request.id, resolved)}>Answer</button>
      <button onClick=${() => controller.rejectQuestion(sessionID, request.id)}>Reject</button>
    </div>
  </div>`;
}
