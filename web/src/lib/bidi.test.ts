import { describe, expect, it } from "vitest";
import { dirOf, pieces } from "./bidi";

describe("bidi pieces", () => {
  it("keeps each sentence's punctuation with it", () => {
    expect(pieces("الحمد لله! كله تمام. Need anything for the suite today?")).toEqual(
      ["الحمد لله!", " ", "كله تمام.", " ", "Need anything for the suite today?"]);
  });
  it("splits clauses at a spaced dash", () => {
    expect(pieces("الـ desk lamp وصلت — it's waiting.")).toEqual(["الـ desk lamp وصلت", " — ", "it's waiting."]);
  });
  it("leaves times, decimals and plain text alone", () => {
    expect(pieces("$1.75 at 4:30")).toEqual(["$1.75 at 4:30"]);
  });
  it("reads direction from the first letter", () => {
    expect(dirOf("  ١٢ أهلاً hi")).toBe("rtl");
    expect(dirOf("ok يلا")).toBe("ltr");
  });
});
