import { ApiError } from "../../api/client.js";

// Older daemons omit workspace. Do not disable their existing workflows.
export function workspaceUnavailable(session, runtime = null) {
  if (session?.workspace && session.workspace.status !== "available") return session.workspace;
  const error = runtime?.lastError;
  if (error?.code !== "session_workspace_unavailable") return null;
  if (error.details?.directory && error.details.directory !== session?.location?.directory) return null;
  return { status: error.details?.status || "missing", message: error.message };
}

export function requireAvailableWorkspace(session, runtime = null) {
  const workspace = workspaceUnavailable(session, runtime);
  if (!workspace) return;
  throw new ApiError(409, "session_workspace_unavailable",
    workspace.message || "Workspace unavailable. Choose Relocate workspace to continue.",
    { sessionID: session.id, directory: session.location?.directory, status: workspace.status });
}
