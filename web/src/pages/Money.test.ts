import { describe, expect, it } from "vitest";
import { fmt, previewSplit, toCents } from "./Money";

describe("money helpers (display only; the server records the real split)", () => {
  it("parses amounts to cents", () => {
    expect(toCents("42")).toBe(4200);
    expect(toCents("42.5")).toBe(4250);
    expect(toCents("$1,200.05")).toBe(120005);
    expect(toCents("0")).toBeNull();
    expect(toCents("1.005")).toBeNull();
    expect(toCents("ten")).toBeNull();
  });

  it("previews the same exact split as the server", () => {
    const s = previewSplit(10000, [3, 1, 2]);
    expect([s.get(1), s.get(2), s.get(3)]).toEqual([3334, 3333, 3333]);
    expect([...previewSplit(4250, [1, 2, 3, 4]).values()].reduce((a, b) => a + b, 0)).toBe(4250);
    expect(previewSplit(500, []).size).toBe(0);
  });

  it("formats dollars", () => {
    expect(fmt(1050)).toBe("$10.50");
    expect(fmt(-5)).toBe("$0.05");
    expect(fmt(123456)).toBe("$1,234.56");
  });
});
