"""CLI and local HTTP access for Yoke Observe runs."""

from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Annotated
from urllib.parse import parse_qs
from urllib.parse import urlparse

import click
import typer

from yoke.observe import JsonlObserveStore
from yoke.observe.models import WorkflowState


observe_app = typer.Typer(help="Inspect observed SDK workflow runs.")


@observe_app.command("list")
def list_runs(
    root: Annotated[
        Path,
        typer.Option(
            "--root",
            help="Workspace root containing .yoke/observe.",
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
    ] = Path.cwd(),
) -> None:
    """List observed workflow runs."""
    store = JsonlObserveStore(root)
    for manifest in store.list_runs():
        click.echo(
            f"{manifest.run_id}\t{manifest.status}\t"
            f"{manifest.event_count}\t{manifest.name}"
        )


@observe_app.command("state")
def state(
    run_id: Annotated[
        str,
        typer.Argument(help="Run id to inspect, or 'latest'."),
    ] = "latest",
    root: Annotated[
        Path,
        typer.Option(
            "--root",
            help="Workspace root containing .yoke/observe.",
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
    ] = Path.cwd(),
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print the full projected state as JSON."),
    ] = False,
) -> None:
    """Print the current projected state for a run."""
    store = JsonlObserveStore(root)
    resolved_run_id = _resolve_run_id(store, run_id)
    projection = store.latest_state(resolved_run_id)
    if projection is None:
        raise typer.BadParameter(f"Unknown observe run: {resolved_run_id}")
    if json_output:
        click.echo(projection.model_dump_json(indent=2, exclude_none=True))
        return
    click.echo(_format_state(projection))


@observe_app.command("events")
def events(
    run_id: Annotated[
        str,
        typer.Argument(help="Run id to inspect, or 'latest'."),
    ] = "latest",
    root: Annotated[
        Path,
        typer.Option(
            "--root",
            help="Workspace root containing .yoke/observe.",
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
    ] = Path.cwd(),
    after: Annotated[
        int, typer.Option(help="Only print events after this sequence.")
    ] = 0,
) -> None:
    """Print observe events as JSON lines."""
    store = JsonlObserveStore(root)
    resolved_run_id = _resolve_run_id(store, run_id)
    for event in store.events(resolved_run_id, after=after):
        click.echo(event.model_dump_json(exclude_none=True))


@observe_app.command("watch")
def watch(
    run_id: Annotated[
        str,
        typer.Argument(help="Run id to inspect, or 'latest'."),
    ] = "latest",
    root: Annotated[
        Path,
        typer.Option(
            "--root",
            help="Workspace root containing .yoke/observe.",
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
    ] = Path.cwd(),
    interval: Annotated[
        float,
        typer.Option(help="Polling interval in seconds."),
    ] = 0.5,
) -> None:
    """Watch new observe events as JSON lines."""
    store = JsonlObserveStore(root)
    resolved_run_id = _resolve_run_id(store, run_id)
    after = 0
    while True:
        emitted = False
        for event in store.events(resolved_run_id, after=after):
            click.echo(event.model_dump_json(exclude_none=True))
            after = event.sequence
            emitted = True
        if not emitted:
            time.sleep(interval)


@observe_app.command("serve")
def serve(
    root: Annotated[
        Path,
        typer.Option(
            "--root",
            help="Workspace root containing .yoke/observe.",
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
        ),
    ] = Path.cwd(),
    host: Annotated[str, typer.Option(help="Host to bind.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to bind.")] = 8787,
) -> None:
    """Serve observe runs over a small local JSON HTTP API."""
    store = JsonlObserveStore(root)
    handler = _handler_for_store(store)
    server = ThreadingHTTPServer((host, port), handler)
    click.echo(f"Yoke Observe listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        click.echo("Stopping Yoke Observe.")


def _resolve_run_id(store: JsonlObserveStore, run_id: str) -> str:
    if run_id != "latest":
        return run_id
    latest = store.latest_run_id()
    if latest is None:
        raise typer.BadParameter("No observed workflow runs found.")
    return latest


def _format_state(state: WorkflowState) -> str:
    lines = [
        f"run: {state.run_id}",
        f"name: {state.name or state.run_id}",
        f"status: {state.status}",
        f"events: {state.event_count}",
        "nodes:",
    ]
    for node in state.nodes.values():
        detail = f"  {node.node_id} [{node.status}] {node.kind}: {node.label}"
        if node.output_type:
            detail += f" -> {node.output_type}"
        if node.error:
            detail += f" error={node.error}"
        lines.append(detail)
    if state.edges:
        lines.append("edges:")
        for edge in state.edges:
            label = f" ({edge.label})" if edge.label else ""
            lines.append(f"  {edge.from_node_id} -> {edge.to_node_id}{label}")
    return "\n".join(lines)


def _handler_for_store(store: JsonlObserveStore):
    class ObserveHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            parts = [part for part in parsed.path.split("/") if part]
            query = parse_qs(parsed.query)
            if not parts or (len(parts) == 2 and parts[0] == "runs"):
                self._send_html(OBSERVE_UI_HTML)
                return
            if parts == ["runs"]:
                self._send_json(
                    [run.model_dump(mode="json") for run in store.list_runs()]
                )
                return
            if len(parts) == 3 and parts[0] == "runs" and parts[2] == "state":
                state = store.latest_state(parts[1])
                if state is None:
                    self._send_error(404, "Unknown run")
                    return
                self._send_json(state.model_dump(mode="json", exclude_none=True))
                return
            if len(parts) == 3 and parts[0] == "runs" and parts[2] == "events":
                after = _parse_after(query)
                events = [
                    event.model_dump(mode="json", exclude_none=True)
                    for event in store.events(parts[1], after=after)
                ]
                self._send_json(events)
                return
            self._send_error(404, "Unknown endpoint")

        def log_message(self, format: str, *args: object) -> None:
            return

        def _send_json(self, payload: object) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, html: str) -> None:
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_error(self, status: int, message: str) -> None:
            body = json.dumps({"error": message}).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return ObserveHandler


def _parse_after(query: dict[str, list[str]]) -> int:
    values = query.get("after")
    if not values:
        return 0
    try:
        return int(values[0])
    except ValueError:
        return 0


OBSERVE_UI_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Yoke Observe</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
  <script src="https://unpkg.com/dagre@0.8.5/dist/dagre.min.js"></script>
  <script src="https://unpkg.com/cytoscape@3.30.4/dist/cytoscape.min.js"></script>
  <script src="https://unpkg.com/cytoscape-dagre@2.5.0/cytoscape-dagre.js"></script>
  <style>
    :root {
      color-scheme: light;
      --bg: #f5f6f1;
      --surface: #ffffff;
      --surface-soft: #f7f8f4;
      --ink: #171918;
      --muted: #68706c;
      --line: #dfe4dd;
      --line-strong: #a7b0aa;
      --blue: #176b87;
      --green: #23724b;
      --red: #a84435;
      --amber: #9a6a16;
      --violet: #6750a4;
      --graph-line: #87918b;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--ink);
      font: 13px/1.45 "IBM Plex Sans", ui-sans-serif, system-ui, sans-serif;
    }
    button {
      border: 1px solid var(--line);
      background: var(--surface);
      color: var(--ink);
      font: inherit;
      cursor: pointer;
    }
    button:hover { border-color: var(--line-strong); }
    h1, h2, h3, p { margin: 0; }
    h1 { font-size: 22px; letter-spacing: 0; }
    h2 { font-size: 16px; letter-spacing: 0; }
    h3 { font-size: 13px; margin-bottom: 8px; letter-spacing: 0; }
    .muted { color: var(--muted); }
    .small { font-size: 12px; }
    .break { overflow-wrap: anywhere; }
    .app {
      min-height: 100vh;
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
    }
    .topbar {
      min-height: 62px;
      padding: 12px 22px;
      border-bottom: 1px solid var(--line);
      background: rgba(255,255,255,0.92);
      backdrop-filter: blur(10px);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      position: sticky;
      top: 0;
      z-index: 5;
    }
    .brand-mark {
      display: flex;
      align-items: baseline;
      gap: 10px;
      min-width: 0;
    }
    .toolbar {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
      justify-content: end;
    }
    .icon-button, .text-button {
      height: 34px;
      border-radius: 6px;
      padding: 0 12px;
      font-weight: 600;
    }
    .view { display: none; min-height: 0; }
    .view.active { display: block; }
    .workflow-view.view { display: none; }
    .workflow-view.view.active { display: grid; }
    .selector {
      max-width: 1180px;
      margin: 0 auto;
      padding: 34px 22px 48px;
    }
    .selector-head {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 18px;
      align-items: end;
      margin-bottom: 22px;
    }
    .selector-head p { max-width: 660px; margin-top: 6px; }
    .run-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
      gap: 10px;
    }
    .run-card {
      text-align: left;
      min-height: 126px;
      border-radius: 6px;
      padding: 14px;
      background: var(--surface);
      display: grid;
      gap: 10px;
      box-shadow: 0 1px 0 rgba(23,25,24,0.03);
    }
    .run-card:hover {
      transform: translateY(-1px);
      box-shadow: 0 10px 24px rgba(23,25,24,0.08);
    }
    .run-name, .node-title {
      font-weight: 700;
      overflow-wrap: anywhere;
    }
    .status-row {
      display: flex;
      align-items: center;
      gap: 8px;
      min-width: 0;
    }
    .run-card-foot {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      color: var(--muted);
      font-size: 12px;
      font-variant-numeric: tabular-nums;
    }
    .dot {
      width: 8px;
      height: 8px;
      border-radius: 999px;
      background: var(--line-strong);
      flex: 0 0 auto;
    }
    .dot.completed { background: var(--green); }
    .dot.failed { background: var(--red); }
    .dot.running { background: var(--blue); }
    .dot.pending { background: var(--amber); }
    .workflow-view {
      height: calc(100vh - 62px);
      min-width: 0;
      grid-template-columns: minmax(0, 1fr) 390px;
      overflow: hidden;
    }
    .workspace {
      min-width: 0;
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
      overflow: hidden;
      background:
        linear-gradient(90deg, rgba(23,25,24,0.035) 1px, transparent 1px),
        linear-gradient(180deg, rgba(23,25,24,0.035) 1px, transparent 1px),
        var(--bg);
      background-size: 30px 30px;
    }
    .workspace-head {
      display: grid;
      gap: 12px;
      padding: 16px 18px;
      border-bottom: 1px solid var(--line);
      background: rgba(255,255,255,0.9);
      backdrop-filter: blur(8px);
    }
    .title-line {
      display: flex;
      align-items: end;
      justify-content: space-between;
      gap: 16px;
    }
    .title-line h2 {
      font-size: 18px;
      overflow-wrap: anywhere;
    }
    .metrics {
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 10px;
    }
    .metric {
      min-height: 58px;
      border-left: 2px solid var(--line-strong);
      padding: 7px 10px;
      background: rgba(255,255,255,0.58);
    }
    .metric strong {
      display: block;
      font-size: 20px;
      line-height: 1.1;
    }
    .graph-wrap {
      min-height: 0;
      padding: 12px;
      position: relative;
    }
    #graph {
      width: 100%;
      height: 100%;
      min-height: 420px;
      background: rgba(255,255,255,0.68);
      border: 1px solid rgba(223, 228, 221, 0.82);
      border-radius: 4px;
    }
    .graph-empty {
      position: absolute;
      inset: 12px;
      display: grid;
      place-items: center;
      color: var(--muted);
      border: 1px dashed var(--line);
      border-radius: 4px;
      background: var(--surface);
    }
    .inspector {
      background: var(--surface);
      border-left: 1px solid var(--line);
      min-width: 0;
      overflow: auto;
    }
    .inspector-head {
      padding: 16px;
      border-bottom: 1px solid var(--line);
      position: sticky;
      top: 0;
      background: rgba(255,255,255,0.94);
      backdrop-filter: blur(8px);
      z-index: 2;
    }
    .inspector-body { padding: 14px; }
    .section {
      border-bottom: 1px solid var(--line);
      padding-bottom: 14px;
      margin-bottom: 14px;
    }
    .section:last-child {
      border-bottom: 0;
      margin-bottom: 0;
      padding-bottom: 0;
    }
    .pill {
      display: inline-flex;
      align-items: center;
      min-height: 22px;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 0 8px;
      font-size: 12px;
      color: var(--muted);
      background: var(--surface-soft);
    }
    .pill.completed { color: var(--green); border-color: rgba(47, 111, 78, 0.35); }
    .pill.running { color: var(--blue); border-color: rgba(25, 94, 131, 0.35); }
    .pill.failed { color: var(--red); border-color: rgba(163, 58, 42, 0.35); }
    .kv {
      display: grid;
      grid-template-columns: 84px minmax(0, 1fr);
      gap: 6px 10px;
      margin-top: 10px;
    }
    .kv div:nth-child(odd) { color: var(--muted); }
    .break { overflow-wrap: anywhere; }
    pre {
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      max-height: 260px;
      overflow: auto;
      margin: 10px 0 0;
      padding: 10px;
      background: #f9fafb;
      border: 1px solid var(--line);
      border-radius: 6px;
      font: 12px/1.45 "IBM Plex Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }
    .data-view {
      display: grid;
      gap: 8px;
      margin-top: 10px;
    }
    .field {
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fafbf9;
      padding: 9px;
      min-width: 0;
    }
    .field-label {
      color: var(--muted);
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      margin-bottom: 5px;
    }
    .field-value {
      overflow-wrap: anywhere;
      white-space: pre-wrap;
    }
    .field-grid {
      display: grid;
      gap: 7px;
    }
    .field-list {
      display: grid;
      gap: 7px;
      padding-left: 0;
      margin: 0;
      list-style: none;
    }
    .field-list-item {
      border-left: 2px solid var(--line-strong);
      padding-left: 8px;
      min-width: 0;
    }
    .code-preview {
      margin: 5px 0 0;
      max-height: 190px;
    }
    .agent-panel {
      display: grid;
      gap: 10px;
      margin-top: 10px;
    }
    .agent-card {
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #f5faf8;
      padding: 10px;
    }
    .agent-card-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 8px;
    }
    .agent-final {
      border-left: 2px solid var(--green);
      padding-left: 9px;
      margin-bottom: 10px;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }
    .turn {
      display: grid;
      gap: 6px;
      border-top: 1px solid var(--line);
      padding-top: 9px;
      margin-top: 9px;
    }
    .turn-title {
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
    }
    .message {
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      font-size: 12px;
      line-height: 1.45;
    }
    .summary-list {
      display: grid;
      gap: 10px;
    }
    .summary-item {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      border-bottom: 1px solid var(--line);
      padding-bottom: 8px;
    }
    .summary-item:last-child { border-bottom: 0; padding-bottom: 0; }
    .primary-action {
      width: 100%;
      height: 36px;
      border-radius: 6px;
      font-weight: 700;
      margin-top: 10px;
    }
    .detail-overlay {
      position: fixed;
      inset: 0;
      z-index: 20;
      display: none;
      background: rgba(23,25,24,0.28);
    }
    .detail-overlay.active { display: grid; }
    .detail-panel {
      justify-self: end;
      width: min(1120px, 100vw);
      height: 100vh;
      background: var(--surface);
      border-left: 1px solid var(--line);
      box-shadow: -18px 0 48px rgba(23,25,24,0.16);
      display: grid;
      grid-template-rows: auto minmax(0, 1fr);
    }
    .detail-head {
      padding: 18px 20px;
      border-bottom: 1px solid var(--line);
      display: flex;
      align-items: start;
      justify-content: space-between;
      gap: 16px;
    }
    .detail-body {
      min-height: 0;
      overflow: auto;
      padding: 18px 20px 28px;
      display: grid;
      gap: 18px;
      align-content: start;
    }
    .detail-summary {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
    }
    .summary-tile {
      border-left: 2px solid var(--line-strong);
      padding: 7px 9px;
      background: var(--surface-soft);
      min-width: 0;
    }
    .summary-tile strong {
      display: block;
      overflow-wrap: anywhere;
    }
    .detail-workbench {
      min-height: 0;
      height: calc(100vh - 168px);
      display: grid;
      grid-template-columns: 220px minmax(0, 1fr);
      border: 1px solid var(--line);
      border-radius: 6px;
      overflow: hidden;
      background: var(--surface);
    }
    .detail-nav {
      overflow: auto;
      border-right: 1px solid var(--line);
      background: var(--surface-soft);
      padding: 8px;
      display: grid;
      align-content: start;
      gap: 6px;
    }
    .detail-nav button {
      width: 100%;
      min-height: 38px;
      border-radius: 6px;
      padding: 7px 8px;
      text-align: left;
      background: transparent;
      display: grid;
      gap: 2px;
    }
    .detail-nav button.active {
      background: var(--surface);
      border-color: var(--ink);
    }
    .nav-label {
      font-weight: 700;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .nav-meta {
      color: var(--muted);
      font-size: 11px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .detail-content {
      min-height: 0;
      overflow: auto;
      padding: 14px;
    }
    .detail-content-head {
      display: flex;
      align-items: start;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 12px;
    }
    .turn-block {
      display: grid;
      gap: 10px;
    }
    details.disclosure {
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fafbf9;
      padding: 9px;
    }
    details.disclosure summary {
      cursor: pointer;
      font-weight: 700;
    }
    .detail-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 14px;
    }
    .detail-section {
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--surface);
      padding: 14px;
      min-width: 0;
    }
    .detail-section.wide { grid-column: 1 / -1; }
    .event {
      display: grid;
      grid-template-columns: 44px minmax(0, 1fr);
      gap: 8px;
      padding: 8px 0;
      border-bottom: 1px solid #eef1f5;
    }
    .event:last-child { border-bottom: 0; }
    .event-seq { color: var(--muted); font-variant-numeric: tabular-nums; }
    .empty {
      color: var(--muted);
      border: 1px dashed var(--line);
      border-radius: 4px;
      padding: 14px;
      background: var(--surface-soft);
    }
    @media (max-width: 1100px) {
      .workflow-view { grid-template-columns: minmax(0, 1fr); height: auto; }
      .inspector {
        border-left: 0;
        border-top: 1px solid var(--line);
        max-height: 360px;
      }
    }
    @media (max-width: 760px) {
      .topbar { align-items: flex-start; padding: 12px 14px; }
      .brand-mark { display: block; }
      .selector { padding: 24px 14px 36px; }
      .selector-head { grid-template-columns: 1fr; }
      .metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      #graph { height: 460px; }
    }
  </style>
</head>
<body>
  <div class="app">
    <header class="topbar">
      <div class="brand-mark">
        <h1>Yoke Observe</h1>
        <p class="muted small" id="connection">Loading</p>
      </div>
      <div class="toolbar">
        <button id="backToRuns" class="text-button" title="Back to runs">Runs</button>
        <button id="refresh" class="icon-button" title="Refresh" aria-label="Refresh">Refresh</button>
      </div>
    </header>

    <section id="selectorView" class="view selector">
      <div class="selector-head">
        <div>
          <h2>Workflow Runs</h2>
          <p class="muted">Pick a run to inspect its live typed execution graph, current node state, structured outputs, and agent conversation trace.</p>
        </div>
        <span id="runCount" class="pill">0 runs</span>
      </div>
      <div id="runGrid" class="run-grid"></div>
    </section>

    <section id="workflowView" class="view workflow-view">
      <main class="workspace">
        <div class="workspace-head">
          <div class="title-line">
            <div>
              <h2 id="runTitle">No run selected</h2>
              <p class="muted small" id="runMeta"></p>
            </div>
            <span id="runStatus" class="pill">idle</span>
          </div>
          <div id="metrics" class="metrics"></div>
        </div>
        <div class="graph-wrap">
          <div id="graph"></div>
          <div id="graphEmpty" class="graph-empty">No workflow graph yet.</div>
        </div>
      </main>

      <aside class="inspector">
        <div class="inspector-head">
          <h2>Inspector</h2>
          <p class="muted small" id="inspectorHint">Run overview</p>
        </div>
        <div id="inspector" class="inspector-body"></div>
      </aside>
    </section>

    <div id="detailOverlay" class="detail-overlay" aria-hidden="true">
      <section class="detail-panel" role="dialog" aria-modal="true" aria-labelledby="detailTitle">
        <div class="detail-head">
          <div>
            <h2 id="detailTitle">Node details</h2>
            <p id="detailMeta" class="muted small"></p>
          </div>
          <button id="closeDetails" class="text-button" title="Close details">Close</button>
        </div>
        <div id="detailBody" class="detail-body"></div>
      </section>
    </div>
  </div>

  <script>
    const state = {
      runs: [],
      selected: null,
      workflow: null,
      events: [],
      after: 0,
      selectedNodeId: null,
      detailNodeId: null,
      detailView: "summary",
      cy: null
    };
    if (typeof cytoscapeDagre === "function") {
      cytoscape.use(cytoscapeDagre);
    }
    const $ = (id) => document.getElementById(id);

    async function getJson(path) {
      const response = await fetch(path, { cache: "no-store" });
      if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
      return await response.json();
    }

    function runFromPath() {
      const parts = location.pathname.split("/").filter(Boolean);
      return parts.length === 2 && parts[0] === "runs" ? parts[1] : null;
    }

    async function loadRuns() {
      state.runs = await getJson("/runs");
      const wanted = runFromPath();
      state.selected = wanted || state.selected;
      renderRuns();
      if (wanted) await loadState(wanted);
      else renderSelector();
      $("connection").textContent = state.runs.length ? "Live" : "No runs";
    }

    function renderSelector() {
      state.workflow = null;
      state.selectedNodeId = null;
      $("selectorView").classList.add("active");
      $("workflowView").classList.remove("active");
      $("backToRuns").style.display = "none";
      if (state.cy) state.cy.elements().remove();
      history.replaceState(null, "", "/");
      renderRuns();
    }

    function renderWorkflowView() {
      $("selectorView").classList.remove("active");
      $("workflowView").classList.add("active");
      $("backToRuns").style.display = "";
    }

    async function refreshCurrentView() {
      state.runs = await getJson("/runs");
      renderRuns();
      if (runFromPath() && state.selected) await loadState(state.selected);
      else renderSelector();
    }

    async function loadState(runId) {
      renderWorkflowView();
      state.workflow = await getJson(`/runs/${runId}/state`);
      state.events = await getJson(`/runs/${runId}/events?after=0`);
      state.after = state.events.reduce((max, event) => Math.max(max, event.sequence), 0);
      state.selected = runId;
      state.selectedNodeId = pickInitialNodeId();
      history.replaceState(null, "", `/runs/${runId}`);
      render();
    }

    async function pollEvents() {
      if (!state.selected) return;
      const events = await getJson(`/runs/${state.selected}/events?after=${state.after}`);
      if (!events.length) return;
      state.events.push(...events);
      state.after = events.reduce((max, event) => Math.max(max, event.sequence), state.after);
      state.workflow = await getJson(`/runs/${state.selected}/state`);
      if (state.selectedNodeId && !state.workflow.nodes[state.selectedNodeId]) {
        state.selectedNodeId = pickInitialNodeId();
      }
      render();
    }

    function renderRuns() {
      $("runCount").textContent = `${state.runs.length} run${state.runs.length === 1 ? "" : "s"}`;
      $("runGrid").innerHTML = state.runs.map((run) => `
        <button class="run-card" data-run="${run.run_id}">
          <div class="status-row">
            <span class="dot ${escapeHtml(run.status)}"></span>
            <span class="run-name">${escapeHtml(run.name)}</span>
          </div>
          <div class="muted small break">${escapeHtml(run.run_id)}</div>
          <div class="run-card-foot">
            <span>${run.event_count} events</span>
            <span>${escapeHtml(compactDate(run.updated_at || run.created_at))}</span>
          </div>
        </button>
      `).join("") || `<div class="empty">No observed runs.</div>`;
      document.querySelectorAll("[data-run]").forEach((button) => {
        button.addEventListener("click", () => selectRun(button.dataset.run));
      });
    }

    function render() {
      if (!state.workflow) return renderEmpty();
      const wf = state.workflow;
      const nodes = Object.values(wf.nodes || {});
      const graph = graphElements(nodes, wf.edges || []);
      $("runTitle").textContent = wf.name || wf.run_id;
      $("runMeta").textContent = `${wf.run_id} - ${wf.event_count} events`;
      $("runStatus").textContent = wf.status;
      $("runStatus").className = `pill ${wf.status}`;
      $("metrics").innerHTML = metric("Nodes", graph.nodes.length)
        + metric("Running", graph.nodes.filter((n) => n.status === "running").length)
        + metric("Completed", graph.nodes.filter((n) => n.status === "completed").length)
        + metric("Failed", graph.nodes.filter((n) => n.status === "failed").length)
        + metric("Edges", graph.edges.length);
      renderGraph(graph);
      renderInspector();
      renderRuns();
    }

    function renderEmpty() {
      $("connection").textContent = "No data";
      $("runTitle").textContent = "No run selected";
      $("runMeta").textContent = "";
      $("runStatus").textContent = "idle";
      $("metrics").innerHTML = "";
      $("graphEmpty").style.display = "grid";
      $("inspector").innerHTML = `<div class="empty">No run selected.</div>`;
      if (state.cy) state.cy.elements().remove();
    }

    function metric(label, value) {
      return `<div class="metric"><span class="muted small">${label}</span><strong>${value}</strong></div>`;
    }

    function renderGraph(graph) {
      $("graphEmpty").style.display = graph.nodes.length ? "none" : "grid";
      const elements = [
        ...graph.nodes.map((node) => ({
          data: {
            id: node.node_id,
            label: graphNodeLabel(node),
            status: node.status,
            kind: node.kind,
            promptCount: promptCount(node),
            detail: graphNodeDetail(node)
          }
        })),
        ...graph.edges.map((edge, index) => ({
          data: {
            id: `edge-${index}-${edge.from_node_id}-${edge.to_node_id}`,
            source: edge.from_node_id,
            target: edge.to_node_id,
            label: edge.label || ""
          }
        }))
      ];
      if (!state.cy) {
        state.cy = cytoscape({
          container: $("graph"),
          minZoom: 0.25,
          maxZoom: 2.5,
          wheelSensitivity: 0.18,
          boxSelectionEnabled: false,
          autounselectify: false,
          style: graphStyle()
        });
        state.cy.on("tap", "node", (event) => {
          state.selectedNodeId = event.target.id();
          renderInspector();
          updateSelectedNode();
        });
        state.cy.on("tap", (event) => {
          if (event.target === state.cy) {
            state.selectedNodeId = null;
            renderInspector();
            updateSelectedNode();
          }
        });
      }
      state.cy.elements().remove();
      state.cy.add(elements);
      state.cy.layout({
        name: typeof cytoscapeDagre === "function" ? "dagre" : "breadthfirst",
        rankDir: "TB",
        rankSep: 96,
        nodeSep: 42,
        edgeSep: 18,
        spacingFactor: 1.05,
        padding: 56,
        animate: false
      }).run();
      updateSelectedNode();
      state.cy.fit(undefined, 44);
    }

    function graphElements(nodes, edges) {
      const byId = Object.fromEntries(nodes.map((node) => [node.node_id, node]));
      const childAgents = {};
      for (const node of nodes) {
        if (node.kind === "agent" && node.parent_node_id) {
          if (!childAgents[node.parent_node_id]) childAgents[node.parent_node_id] = [];
          childAgents[node.parent_node_id].push(node);
        }
      }
      const hidden = new Set(
        nodes
          .filter((node) => node.kind === "agent" && node.parent_node_id)
          .map((node) => node.node_id)
      );
      const visibleNodes = nodes
        .filter((node) => !hidden.has(node.node_id))
        .map((node) => ({ ...node, child_agents: childAgents[node.node_id] || [] }));
      const visibleIds = new Set(visibleNodes.map((node) => node.node_id));
      const visibleEdges = [];
      const edgeKeys = new Set();
      for (const edge of edges) {
        if (edge.label === "contains") continue;
        let source = edge.from_node_id;
        let target = edge.to_node_id;
        if (hidden.has(source)) source = byId[source] && byId[source].parent_node_id;
        if (hidden.has(target)) target = byId[target] && byId[target].parent_node_id;
        if (!source || !target || source === target) continue;
        if (!visibleIds.has(source) || !visibleIds.has(target)) continue;
        const key = `${source}->${target}:${edge.label || ""}`;
        if (edgeKeys.has(key)) continue;
        edgeKeys.add(key);
        visibleEdges.push({ ...edge, from_node_id: source, to_node_id: target });
      }
      return { nodes: visibleNodes, edges: visibleEdges };
    }

    function graphStyle() {
      return [
        {
          selector: "node",
          style: {
            "shape": "round-rectangle",
            "width": 230,
            "height": 78,
            "background-color": "#ffffff",
            "border-color": "#a7b0aa",
            "border-width": 1,
            "label": "data(label)",
            "text-wrap": "wrap",
            "text-max-width": 190,
            "font-size": 12,
            "font-weight": 700,
            "font-family": "IBM Plex Sans",
            "color": "#171918",
            "text-valign": "center",
            "text-halign": "center",
            "overlay-opacity": 0,
            "underlay-opacity": 0,
            "text-margin-y": 0,
            "line-height": 1.18
          }
        },
        { selector: "node[status = 'completed']", style: { "border-color": "#23724b" } },
        { selector: "node[status = 'running']", style: { "border-color": "#176b87", "border-width": 2 } },
        { selector: "node[status = 'failed']", style: { "border-color": "#a84435", "border-width": 2, "background-color": "#fff6f2" } },
        { selector: "node[kind = 'agent']", style: { "background-color": "#f5faf8" } },
        {
          selector: "node:selected",
          style: {
            "border-color": "#6750a4",
            "border-width": 3,
            "underlay-color": "#6750a4",
            "underlay-opacity": 0.08,
            "underlay-padding": 8
          }
        },
        {
          selector: "edge",
          style: {
            "curve-style": "taxi",
            "taxi-direction": "downward",
            "taxi-turn": 28,
            "target-arrow-shape": "triangle",
            "target-arrow-color": "#87918b",
            "line-color": "#87918b",
            "width": 1.4,
            "arrow-scale": 0.9,
            "overlay-opacity": 0
          }
        },
        {
          selector: "edge[label = 'contains']",
          style: {
            "line-style": "dashed",
            "line-color": "#b7c1bc",
            "target-arrow-color": "#b7c1bc",
            "width": 1.1
          }
        }
      ];
    }

    function updateSelectedNode() {
      if (!state.cy) return;
      state.cy.nodes().unselect();
      if (state.selectedNodeId) {
        const node = state.cy.getElementById(state.selectedNodeId);
        if (node.length) node.select();
      }
    }

    function renderInspector() {
      const node = state.workflow && state.selectedNodeId
        ? state.workflow.nodes[state.selectedNodeId]
        : null;
      $("inspectorHint").textContent = node ? "Node data" : "Run overview";
      if (!node) {
        $("inspector").innerHTML = renderRunOverview();
        return;
      }
      $("inspector").innerHTML = `
        <div class="section">
          <div class="status-row">
            <span class="dot ${escapeHtml(node.status)}"></span>
            <h3 class="node-title">${escapeHtml(node.label)}</h3>
          </div>
          ${node.error ? `<pre>${escapeHtml(node.error)}</pre>` : ""}
        </div>
        <div class="section">
          <div class="summary-list">
            <div class="summary-item"><span class="muted">Status</span><strong>${escapeHtml(node.status)}</strong></div>
            <div class="summary-item"><span class="muted">Output</span><strong>${escapeHtml(node.output_type || "none")}</strong></div>
            <div class="summary-item"><span class="muted">Duration</span><strong>${escapeHtml(nodeDuration(node))}</strong></div>
            <div class="summary-item"><span class="muted">Agent turns</span><strong>${agentTurnCount(node)}</strong></div>
          </div>
        </div>
        <button id="openDetails" class="primary-action">Open details</button>
      `;
      $("openDetails").addEventListener("click", () => openDetails(node.node_id));
    }

    function renderRunOverview() {
      if (!state.workflow) return `<div class="empty">No run selected.</div>`;
      const recent = state.events.slice(-8).reverse();
      return `
        <div class="section">
          <h3>Run</h3>
          <div class="kv">
            <div>Status</div><div>${escapeHtml(state.workflow.status)}</div>
            <div>Events</div><div>${state.workflow.event_count}</div>
            <div>Updated</div><div>${escapeHtml(state.workflow.updated_at || "")}</div>
            <div>Duration</div><div>${escapeHtml(runDuration())}</div>
          </div>
        </div>
        <div class="section">
          <h3>Recent Events</h3>
          ${recent.length ? recent.map(renderCompactEvent).join("") : `<p class="muted small">No events yet.</p>`}
        </div>
      `;
    }

    function openDetails(nodeId) {
      if (!state.workflow) return;
      const node = state.workflow.nodes[nodeId];
      if (!node) return;
      state.detailNodeId = nodeId;
      state.detailView = "summary";
      renderDetailView();
      $("detailOverlay").classList.add("active");
      $("detailOverlay").setAttribute("aria-hidden", "false");
    }

    function closeDetails() {
      $("detailOverlay").classList.remove("active");
      $("detailOverlay").setAttribute("aria-hidden", "true");
    }

    function renderDetailView() {
      if (!state.workflow || !state.detailNodeId) return;
      const node = state.workflow.nodes[state.detailNodeId];
      if (!node) return;
      const agents = node.kind === "agent" ? [node] : childAgentsFor(node);
      const navItems = detailNavItems(node, agents);
      if (!navItems.some((item) => item.id === state.detailView)) {
        state.detailView = "summary";
      }
      $("detailTitle").textContent = node.label;
      $("detailMeta").textContent = `${node.kind} · ${node.status} · ${nodeDuration(node)}`;
      $("detailBody").innerHTML = `
        <div class="detail-summary">
          ${summaryTile("Status", node.status)}
          ${summaryTile("Output", node.output_type || "none")}
          ${summaryTile("Duration", nodeDuration(node))}
          ${summaryTile("Agent turns", String(agentTurnCount(node)))}
        </div>
        <div class="detail-workbench">
          <nav class="detail-nav">
            ${navItems.map((item) => `
              <button class="${item.id === state.detailView ? "active" : ""}" data-detail-view="${escapeHtml(item.id)}">
                <span class="nav-label">${escapeHtml(item.label)}</span>
                <span class="nav-meta">${escapeHtml(item.meta || "")}</span>
              </button>
            `).join("")}
          </nav>
          <main class="detail-content">
            ${renderDetailContent(node, agents)}
          </main>
        </div>
      `;
      document.querySelectorAll("[data-detail-view]").forEach((button) => {
        button.addEventListener("click", () => {
          state.detailView = button.dataset.detailView;
          renderDetailView();
        });
      });
    }

    function summaryTile(label, value) {
      return `
        <div class="summary-tile">
          <span class="muted small">${escapeHtml(label)}</span>
          <strong>${escapeHtml(value)}</strong>
        </div>
      `;
    }

    function detailNavItems(node, agents) {
      const items = [
        { id: "summary", label: "Summary", meta: "what matters" },
        { id: "input", label: "Input", meta: compactSummary(nodeInput(node)) },
        { id: "output", label: "Output", meta: compactSummary(nodeOutput(node)) },
      ];
      for (const [agentIndex, agent] of agents.entries()) {
        const finalMessage = latestAgentMessage(agent);
        if (finalMessage) {
          items.push({
            id: `agent-${agentIndex}-final`,
            label: "Final message",
            meta: compactLabel(agent.output_type || "agent", 28),
          });
        }
        for (const turn of agentTurns(agent)) {
          items.push({
            id: `agent-${agentIndex}-turn-${turn.turn}`,
            label: `Turn ${turn.turn}`,
            meta: turn.output_type || "agent",
          });
        }
      }
      return items;
    }

    function renderDetailContent(node, agents) {
      if (state.detailView === "input") {
        return renderPane("Input", compactSummary(nodeInput(node)), renderDataView(nodeInput(node)));
      }
      if (state.detailView === "output") {
        return renderPane("Output", compactSummary(nodeOutput(node)), renderDataView(nodeOutput(node)));
      }
      if (state.detailView.startsWith("agent-")) {
        return renderAgentPane(agents);
      }
      return renderSummaryPane(node, agents);
    }

    function renderPane(title, meta, body) {
      return `
        <div class="detail-content-head">
          <div>
            <h3>${escapeHtml(title)}</h3>
            <p class="muted small">${escapeHtml(meta)}</p>
          </div>
        </div>
        ${body}
      `;
    }

    function renderSummaryPane(node, agents) {
      const output = nodeOutput(node);
      const input = nodeInput(node);
      const finalMessages = agents.map(latestAgentMessage).filter(Boolean);
      return `
        <div class="detail-content-head">
          <div>
            <h3>Summary</h3>
            <p class="muted small">compact node overview</p>
          </div>
        </div>
        <div class="detail-grid">
          <section class="detail-section">
            <h3>Input</h3>
            <p class="field-value">${escapeHtml(compactSummary(input))}</p>
          </section>
          <section class="detail-section">
            <h3>Output</h3>
            <p class="field-value">${escapeHtml(compactSummary(output))}</p>
          </section>
          ${finalMessages.length ? `
            <section class="detail-section wide">
              <h3>Latest Agent Message</h3>
              <div class="agent-final">${escapeHtml(compactText(finalMessages[finalMessages.length - 1], 1400))}</div>
            </section>
          ` : ""}
        </div>
      `;
    }

    function nodeInput(node) {
      const prompts = (node.events || [])
        .filter((event) => event.event === "prompt_started")
        .map((event, index) => pruneEmpty({
          turn: index + 1,
          output_type: event.output_type || null,
          prompt: event.prompt || ""
        }));
      if (prompts.length) return { prompts };
      const inputs = node.metadata && node.metadata.inputs;
      return unwrapPreview(inputs || {});
    }

    function nodeOutput(node) {
      return unwrapPreview(node.output_preview === undefined ? {} : node.output_preview);
    }

    function renderAgentDetailSections(node) {
      const agents = node.kind === "agent" ? [node] : childAgentsFor(node);
      if (!agents.length) return "";
      return `
        <section class="detail-section wide">
          <h3>Agent Activity</h3>
          <div class="agent-panel">
            ${agents.map(renderAgentCard).join("")}
          </div>
        </section>
      `;
    }

    function renderAgentPane(agents) {
      const match = state.detailView.match(/^agent-(\d+)-(final|turn)-?(\d+)?$/);
      if (!match) return renderPane("Agent", "", `<div class="empty">No agent detail.</div>`);
      const agent = agents[Number(match[1])];
      if (!agent) return renderPane("Agent", "", `<div class="empty">No agent detail.</div>`);
      if (match[2] === "final") {
        const finalMessage = latestAgentMessage(agent);
        return renderPane(
          "Final message",
          `${agent.output_type || "agent"} · ${promptCount(agent)} turns`,
          finalMessage
            ? `<div class="agent-final">${escapeHtml(compactText(finalMessage, 2600))}</div>`
            : `<div class="empty">No final message.</div>`
        );
      }
      const turn = agentTurns(agent).find((item) => item.turn === Number(match[3]));
      if (!turn) return renderPane("Agent turn", "", `<div class="empty">No turn detail.</div>`);
      return `
        <div class="detail-content-head">
          <div>
            <h3>Turn ${turn.turn}</h3>
            <p class="muted small">${escapeHtml(turn.output_type || "agent")}</p>
          </div>
        </div>
        <div class="turn-block">
          <details class="disclosure">
            <summary>Prompt</summary>
            <pre>${escapeHtml(turn.prompt || "")}</pre>
          </details>
          <details class="disclosure" open>
            <summary>Commentary</summary>
            <pre>${escapeHtml(turn.response || "")}</pre>
          </details>
        </div>
      `;
    }

    function renderAgentCard(agent) {
      const turns = agentTurns(agent);
      const finalMessage = latestAgentMessage(agent);
      const title = compactLabel(agent.output_type || "Agent", 34);
      return `
        <div class="agent-card">
          <div class="agent-card-head">
            <strong>${escapeHtml(title)}</strong>
            <span class="pill">${turns.length} turn${turns.length === 1 ? "" : "s"}</span>
          </div>
          ${finalMessage ? `
            <div>
              <div class="field-label">Final message</div>
              <div class="agent-final">${escapeHtml(compactText(finalMessage, 1400))}</div>
            </div>
          ` : ""}
          ${turns.map((turn) => `
            <div class="turn">
              <div class="turn-title">Turn ${turn.turn}${turn.output_type ? ` · ${escapeHtml(turn.output_type)}` : ""}</div>
              ${turn.prompt ? `<div class="message"><strong>Prompt</strong><br>${escapeHtml(compactText(turn.prompt, 900))}</div>` : ""}
              ${turn.response ? `<div class="message"><strong>Commentary</strong><br>${escapeHtml(compactText(turn.response, 900))}</div>` : ""}
            </div>
          `).join("")}
        </div>
      `;
    }

    function childAgentsFor(node) {
      if (!state.workflow) return [];
      return Object.values(state.workflow.nodes || {})
        .filter((candidate) => candidate.kind === "agent" && candidate.parent_node_id === node.node_id);
    }

    function agentTurns(agent) {
      const events = agent.events || [];
      const starts = events.filter((event) => event.event === "prompt_started");
      const ends = events.filter((event) => event.event === "model_end");
      return starts.map((event, index) => ({
        turn: index + 1,
        output_type: event.output_type || null,
        prompt: event.prompt || "",
        response: ends[index] && ends[index].content ? ends[index].content : ""
      }));
    }

    function latestAgentMessage(agent) {
      const events = (agent.events || []).filter((event) => event.event === "model_end" && event.content);
      if (!events.length) return "";
      return events[events.length - 1].content || "";
    }

    function renderDataView(value) {
      return `<div class="data-view">${renderValue(unwrapPreview(value), "value")}</div>`;
    }

    function renderValue(value, label) {
      value = unwrapPreview(value);
      if (value && typeof value === "object" && !Array.isArray(value)) {
        if (value.__truncated__ === true && value.preview !== undefined) {
          return `
            <div class="field">
              <div class="field-label">${escapeHtml(label)}</div>
              <div class="muted small">${value.characters || ""} characters, preview shown</div>
              <pre class="code-preview">${escapeHtml(value.preview)}</pre>
            </div>
          `;
        }
        const entries = Object.entries(value).filter(([key]) => key !== "__truncated__");
        if (!entries.length) return `<div class="empty">No data.</div>`;
        return `
          <div class="field-grid">
            ${entries.map(([key, item]) => `
              <div class="field">
                <div class="field-label">${escapeHtml(key)}</div>
                ${renderValue(item, key)}
              </div>
            `).join("")}
          </div>
        `;
      }
      if (Array.isArray(value)) {
        if (!value.length) return `<div class="muted small">[]</div>`;
        return `
          <ul class="field-list">
            ${value.map((item, index) => `
              <li class="field-list-item">${renderValue(item, String(index + 1))}</li>
            `).join("")}
          </ul>
        `;
      }
      return `<div class="field-value">${escapeHtml(value === undefined || value === null ? "" : String(value))}</div>`;
    }

    function unwrapPreview(value) {
      if (Array.isArray(value)) return value.map(unwrapPreview);
      if (!value || typeof value !== "object") return value;
      const keys = Object.keys(value);
      if (keys.length === 2 && keys.includes("type") && keys.includes("value")) {
        return unwrapPreview(value.value);
      }
      const result = {};
      Object.entries(value).forEach(([key, item]) => {
        result[key] = unwrapPreview(item);
      });
      return result;
    }

    function pruneEmpty(value) {
      if (Array.isArray(value)) return value.map(pruneEmpty);
      if (!value || typeof value !== "object") return value;
      const result = {};
      Object.entries(value).forEach(([key, item]) => {
        if (item === null || item === undefined || item === "") return;
        result[key] = pruneEmpty(item);
      });
      return result;
    }

    function nodeDuration(node) {
      return durationText(node.started_at, node.completed_at || node.failed_at || new Date().toISOString());
    }

    function runDuration() {
      if (!state.events.length) return "n/a";
      return durationText(state.events[0].timestamp, state.workflow.updated_at);
    }

    function durationText(start, end) {
      if (!start || !end) return "n/a";
      const seconds = Math.max(0, Math.round((Date.parse(end) - Date.parse(start)) / 1000));
      if (seconds < 60) return `${seconds}s`;
      const minutes = Math.floor(seconds / 60);
      return `${minutes}m ${seconds % 60}s`;
    }

    function renderCompactEvent(event) {
      return `
        <div class="event">
          <div class="event-seq">#${event.sequence}</div>
          <div>
            <strong>${escapeHtml(event.type)}</strong>
            <div class="muted small break">${escapeHtml(event.node_id || event.timestamp || "")}</div>
          </div>
        </div>
      `;
    }

    function pickInitialNodeId() {
      if (!state.workflow) return null;
      const nodes = Object.values(state.workflow.nodes || {});
      const failed = nodes.find((node) => node.status === "failed");
      const running = nodes.find((node) => node.status === "running");
      const typed = nodes.find((node) => node.output_type);
      return (failed || running || typed || nodes[0] || {}).node_id || null;
    }

    function compactLabel(value, length) {
      value = String(value || "");
      return value.length > length ? `${value.slice(0, length - 1)}...` : value;
    }

    function compactSummary(value) {
      value = unwrapPreview(value);
      if (Array.isArray(value)) return `${value.length} item${value.length === 1 ? "" : "s"}`;
      if (value && typeof value === "object") {
        const keys = Object.keys(value).filter((key) => key !== "__truncated__");
        if (!keys.length) return "empty";
        return keys.slice(0, 4).join(", ") + (keys.length > 4 ? ` +${keys.length - 4}` : "");
      }
      const text = String(value || "");
      return compactLabel(text || "empty", 72);
    }

    function compactText(value, length) {
      value = String(value || "");
      return value.length > length
        ? `${value.slice(0, length)}\n... ${value.length - length} more characters`
        : value;
    }

    function graphNodeLabel(node) {
      if (node.kind === "agent") {
        const turns = promptCount(node);
        const output = compactLabel(node.output_type || "agent", 18);
        return `Agent · ${output}${turns ? ` · ${turns} turns` : ""}`;
      }
      const label = String(node.label || node.output_type || node.kind || "node");
      if (label.includes(".")) {
        const parts = label.split(".");
        return compactLabel(parts[parts.length - 1], 34);
      }
      if (label.includes("_")) {
        return compactLabel(label.replace(/^_+/, ""), 34);
      }
      return compactLabel(label, 34);
    }

    function selectRun(runId) {
      state.selected = runId;
      loadState(runId).catch(showError);
    }

    function compactDate(value) {
      if (!value) return "";
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return value;
      return date.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
    }

    function graphNodeDetail(node) {
      return node.output_type || node.kind || "";
    }

    function promptCount(node) {
      return (node.events || []).filter((event) => event.event === "prompt_started").length;
    }

    function agentTurnCount(node) {
      return (node.kind === "agent" ? [node] : childAgentsFor(node))
        .reduce((total, agent) => total + promptCount(agent), 0);
    }

    function showError(error) {
      $("connection").textContent = error.message;
    }

    function escapeHtml(value) {
      return String(value).replace(/[&<>"']/g, (char) => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;"
      })[char]);
    }

    $("backToRuns").addEventListener("click", renderSelector);
    $("refresh").addEventListener("click", () => refreshCurrentView().catch(showError));
    $("closeDetails").addEventListener("click", closeDetails);
    $("detailOverlay").addEventListener("click", (event) => {
      if (event.target === $("detailOverlay")) closeDetails();
    });
    loadRuns().catch(showError);
    setInterval(() => pollEvents().catch(showError), 1500);
  </script>
</body>
</html>
"""
