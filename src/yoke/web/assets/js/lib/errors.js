import { ApiError } from "../api/client.js";

export function errorMessage(error) {
  if (error instanceof ApiError) return `${error.message}${error.requestID ? ` (${error.requestID})` : ""}`;
  return error?.message || String(error);
}
