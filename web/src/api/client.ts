/**
 * The REST API client. Same origin, session cookie; errors arrive as RFC 9457 problem details
 * and are thrown as ApiError with the platform's stable code.
 */

export interface FieldError {
  loc: (string | number)[];
  msg: string;
  type: string;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly detail: string;
  readonly errors: FieldError[];

  constructor(status: number, code: string, detail: string, errors: FieldError[] = []) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.detail = detail;
    this.errors = errors;
  }

  /** The validation message for a body field, e.g. fieldError("name"). */
  fieldError(field: string): string | undefined {
    return this.errors.find((e) => e.loc[0] === "body" && e.loc[1] === field)?.msg;
  }
}

export type Query = Record<string, string | number | boolean | string[] | undefined | null>;

interface RequestOptions {
  body?: unknown;
  raw?: BodyInit;
  query?: Query;
  signal?: AbortSignal;
}

export function url(path: string, query?: Query): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value === undefined || value === null || value === "") continue;
    if (Array.isArray(value)) value.forEach((v) => params.append(key, v));
    else params.set(key, String(value));
  }
  const qs = params.toString();
  return qs ? `${path}?${qs}` : path;
}

export async function request<T>(method: string, path: string, options: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { accept: "application/json" };
  let body: BodyInit | undefined = options.raw;
  if (options.body !== undefined) {
    headers["content-type"] = "application/json";
    body = JSON.stringify(options.body);
  }
  let response: Response;
  try {
    response = await fetch(url(path, options.query), {
      method,
      headers,
      body,
      credentials: "same-origin",
      signal: options.signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError(0, "unavailable", "Cannot reach the Hollis server. Is `hollis serve` running?");
  }
  if (response.status === 204) return undefined as T;
  const text = await response.text();
  let data: unknown = undefined;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = undefined;
    }
  }
  if (!response.ok) {
    const problem = (data ?? {}) as { code?: string; detail?: string; errors?: FieldError[] };
    throw new ApiError(
      response.status,
      problem.code ?? "error",
      problem.detail ?? `The server answered ${response.status}.`,
      problem.errors ?? [],
    );
  }
  return data as T;
}

export const api = {
  get: <T>(path: string, query?: Query, signal?: AbortSignal) => request<T>("GET", path, { query, signal }),
  post: <T>(path: string, body?: unknown, query?: Query) => request<T>("POST", path, { body, query }),
  put: <T>(path: string, body?: unknown, query?: Query) => request<T>("PUT", path, { body, query }),
  patch: <T>(path: string, body?: unknown) => request<T>("PATCH", path, { body }),
  delete: <T = void>(path: string) => request<T>("DELETE", path),
  upload: <T>(path: string, data: Blob, query?: Query) => request<T>("PUT", path, { raw: data, query }),
};

/** Path segment encoding; keys and invocation ids may contain slashes, kept as separators. */
export const seg = (value: string) => encodeURIComponent(value);
export const segs = (value: string) => value.split("/").map(encodeURIComponent).join("/");
