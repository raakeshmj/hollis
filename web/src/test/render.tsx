import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes } from "react-router";
import { vi } from "vitest";
import { ToastProvider } from "../components/ui";

export interface Call {
  method: string;
  url: string;
  body: unknown;
}

/** Stub fetch with a route table: "METHOD /path" -> [status, body]. Records every call. */
export function stubApi(routes: Record<string, [number, unknown] | ((body: unknown) => [number, unknown])>) {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      const path = input.split("?")[0] ?? input;
      const body = typeof init?.body === "string" ? JSON.parse(init.body) : init?.body;
      calls.push({ method, url: input, body });
      const route = routes[`${method} ${path}`];
      const [status, payload] = typeof route === "function" ? route(body) : (route ?? [404, { code: "not_found", detail: `no stub for ${method} ${path}` }]);
      return new Response(status === 204 ? null : JSON.stringify(payload), {
        status,
        headers: { "content-type": status >= 400 ? "application/problem+json" : "application/json" },
      });
    }),
  );
  return calls;
}

export function renderAt(ui: ReactNode, { route = "/", path = "*" }: { route?: string; path?: string } = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[route]}>
        <ToastProvider>
          <Routes>
            <Route path={path} element={ui} />
            <Route path="*" element={<p>elsewhere</p>} />
          </Routes>
        </ToastProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

export const admin = {
  name: "admin",
  role: "admin" as const,
  can_approve: true,
  memory: null,
  knowledge: null,
  created_at: "2026-11-01T10:00:00Z",
};

export const viewer = { ...admin, name: "vera", role: "viewer" as const, can_approve: false };
