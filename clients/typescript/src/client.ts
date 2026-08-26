import createClient from "openapi-fetch";

import type { paths } from "./schema.js";

export interface YokeClientOptions {
  baseUrl: string;
  token?: string;
}

export function createYokeClient(options: YokeClientOptions) {
  const headers = options.token
    ? { Authorization: `Bearer ${options.token}` }
    : undefined;
  return createClient<paths>({
    baseUrl: options.baseUrl.replace(/\/$/, ""),
    headers,
  });
}

export type YokeClient = ReturnType<typeof createYokeClient>;
