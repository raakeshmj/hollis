import { describe, expect, it } from "vitest";
import { stubApi } from "../test/render";
import { ApiError, api, url } from "./client";

describe("the API client", () => {
  it("raises problem details as ApiError with the platform's code", async () => {
    stubApi({
      "POST /v1/agents": [
        422,
        {
          type: "urn:hollis:problem:invalid_request",
          code: "invalid_request",
          status: 422,
          detail: "the request is invalid",
          errors: [{ loc: ["body", "name"], msg: "String should match pattern", type: "string_pattern_mismatch" }],
        },
      ],
    });
    const error = await api.post("/v1/agents", { name: "Bad" }).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    const apiError = error as ApiError;
    expect([apiError.status, apiError.code]).toEqual([422, "invalid_request"]);
    expect(apiError.fieldError("name")).toBe("String should match pattern");
  });

  it("returns nothing for 204 and parses JSON otherwise", async () => {
    stubApi({ "DELETE /v1/agents/a": [204, null], "GET /v1/agents": [200, [{ name: "a" }]] });
    await expect(api.delete("/v1/agents/a")).resolves.toBeUndefined();
    await expect(api.get("/v1/agents")).resolves.toEqual([{ name: "a" }]);
  });

  it("reports an unreachable server plainly", async () => {
    globalThis.fetch = (() => Promise.reject(new TypeError("Failed to fetch"))) as typeof fetch;
    const error = (await api.get("/v1/health").catch((e: unknown) => e)) as ApiError;
    expect(error.code).toBe("unavailable");
    expect(error.detail).toMatch(/hollis serve/);
  });

  it("builds query strings without empty values and with repeated keys", () => {
    expect(url("/v1/runs", { phase: "running", agent: "", limit: 25, labels: ["a:b", "c:d"] })).toBe(
      "/v1/runs?phase=running&limit=25&labels=a%3Ab&labels=c%3Ad",
    );
  });
});
