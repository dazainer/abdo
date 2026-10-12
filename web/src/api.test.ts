import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, request } from "./api";

function mockFetch(status: number, body: unknown) {
  const fn = vi.fn(async (_url: string, _init?: RequestInit) =>
    new Response(body === undefined ? "" : JSON.stringify(body), { status }),
  );
  vi.stubGlobal("fetch", fn);
  return fn;
}

afterEach(() => vi.unstubAllGlobals());

describe("request", () => {
  it("returns parsed JSON on success", async () => {
    mockFetch(200, { reply: "أهلاً" });
    await expect(api.chat("hi")).resolves.toEqual({ reply: "أهلاً" });
  });

  it("sends X-Abdo and JSON on mutating requests, not on GET", async () => {
    const fn = mockFetch(200, { ok: true });
    await api.addItem("rice");
    const [, init] = fn.mock.calls[0];
    expect((init!.headers as Record<string, string>)["X-Abdo"]).toBe("1");
    expect((init!.headers as Record<string, string>)["Content-Type"]).toBe("application/json");
    await api.shopping();
    const [, getInit] = fn.mock.calls[1];
    expect((getInit!.headers as Record<string, string>)["X-Abdo"]).toBeUndefined();
  });

  it("throws ApiError carrying the server's detail message", async () => {
    mockFetch(409, { detail: "'rice' is already on the list" });
    const err = await api.addItem("rice").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(409);
    expect((err as ApiError).message).toBe("'rice' is already on the list");
  });

  it("falls back to a readable message when the error body isn't a string detail", async () => {
    mockFetch(422, { detail: [{ loc: ["body", "text"], msg: "too short" }] });
    await expect(request("POST", "/api/chat", { text: "" })).rejects.toThrow("That didn't work. Try again.");
    mockFetch(502, undefined);
    await expect(api.me()).rejects.toThrow(/server had a problem/);
  });

  it("turns a network failure into ApiError status 0", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("offline"); }));
    const err = await api.me().catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(0);
  });

  it("sends voice as multipart without forcing a JSON content type", async () => {
    const fn = mockFetch(200, { transcript: "x", reply: "y", audio: null });
    await api.voice(new Blob(["a"], { type: "audio/webm" }), "voice.webm");
    const [, init] = fn.mock.calls[0];
    expect(init!.body).toBeInstanceOf(FormData);
    expect((init!.headers as Record<string, string>)["Content-Type"]).toBeUndefined();
  });
});
