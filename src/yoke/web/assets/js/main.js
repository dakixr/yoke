import { html, render } from "../vendor/htm-preact.js";
import { App } from "./app.js";
import { controller } from "./state/controller.js";

render(html`<${App} />`, document.getElementById("app"));
void controller.start();
