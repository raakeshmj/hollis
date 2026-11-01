import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { admin, renderAt, stubApi } from "../test/render";
import { Login } from "./Login";

const unauthenticated: [number, unknown] = [401, { code: "unauthenticated", detail: "authentication required" }];

describe("Login", () => {
  it("exchanges a sign-in link's token once and removes it from the address bar", async () => {
    const calls = stubApi({
      "GET /v1/session": unauthenticated,
      "POST /v1/session": [200, { principal: admin, expires_at: "2099-01-01T00:00:00Z" }],
    });
    window.history.replaceState(null, "", "/login#token=hxl_single-use-token");
    renderAt(<Login />, { route: "/login", path: "/login" });
    await waitFor(() => expect(screen.getByText("elsewhere")).toBeInTheDocument());
    const posts = calls.filter((c) => c.method === "POST");
    expect(posts).toHaveLength(1);
    expect(posts[0]?.body).toEqual({ token: "hxl_single-use-token" });
    expect(window.location.hash).toBe("");
  });

  it("explains an invalid token", async () => {
    stubApi({ "GET /v1/session": unauthenticated, "POST /v1/session": [401, { code: "unauthenticated", detail: "invalid token" }] });
    window.history.replaceState(null, "", "/login");
    renderAt(<Login />, { route: "/login", path: "/login" });
    await userEvent.type(await screen.findByLabelText("API token"), "hx_not-a-real-token");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText(/That token is not valid/)).toBeInTheDocument();
  });
});
