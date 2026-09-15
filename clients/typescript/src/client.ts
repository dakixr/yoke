import createClient from "openapi-fetch";

import type { components, paths } from "./schema.js";

export interface SessionRelocateRequest {
  directory: string;
  expectedDirectory?: string;
}

export type SessionForkRequest = components["schemas"]["SessionForkRequest"] & {
  location?: { directory: string };
};

export interface YokeClientOptions {
  baseUrl: string;
  token?: string;
}

export function createYokeClient(options: YokeClientOptions) {
  const headers = options.token
    ? { Authorization: `Bearer ${options.token}` }
    : undefined;
  const client = createClient<paths>({
    baseUrl: options.baseUrl.replace(/\/$/, ""),
    headers,
  });
  return Object.assign(client, {
    relocateSession(id: string, body: SessionRelocateRequest) {
      return client.POST("/api/v1/session/{session_id}/relocate", {
        params: { path: { session_id: id } },
        body,
      });
    },
    forkSession(id: string, body: SessionForkRequest = {}) {
      return client.POST("/api/v1/session/{session_id}/fork", {
        params: { path: { session_id: id } },
        body,
      });
    },
  });
}

export type YokeClient = ReturnType<typeof createYokeClient>;
