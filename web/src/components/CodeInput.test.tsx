import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { CodeInput, cleanCode } from "./CodeInput";

afterEach(cleanup);

function Harness({ onComplete }: { onComplete: (v: string) => void }) {
  const [value, setValue] = useState("");
  return <CodeInput value={value} onChange={setValue} onComplete={onComplete} label="6-digit code" />;
}

describe("cleanCode", () => {
  it("keeps only digits, at most six", () => {
    expect(cleanCode("12a3")).toBe("123");
    expect(cleanCode("Code: 123 456")).toBe("123456");
    expect(cleanCode("1234567")).toBe("123456");
  });
});

describe("CodeInput", () => {
  it("fills the slots and completes on the sixth digit", () => {
    const done = vi.fn();
    render(<Harness onComplete={done} />);
    const input = screen.getByLabelText("6-digit code");
    fireEvent.change(input, { target: { value: "12345" } });
    expect(done).not.toHaveBeenCalled();
    fireEvent.change(input, { target: { value: "123456" } });
    expect(done).toHaveBeenCalledWith("123456");
  });

  it("accepts a pasted code with spaces", () => {
    const done = vi.fn();
    render(<Harness onComplete={done} />);
    fireEvent.change(screen.getByLabelText("6-digit code"), { target: { value: "987 654" } });
    expect(done).toHaveBeenCalledWith("987654");
  });

  it("is one labelled numeric input for assistive tech", () => {
    render(<Harness onComplete={() => {}} />);
    const input = screen.getByLabelText("6-digit code");
    expect(input.getAttribute("inputmode")).toBe("numeric");
    expect(input.getAttribute("autocomplete")).toBe("one-time-code");
  });
});
