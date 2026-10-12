import { useEffect, useRef } from "react";

export const CODE_LENGTH = 6;

/** Keep only digits, at most six. Handles pasted "123 456" or "Code: 123456". */
export function cleanCode(raw: string): string {
  return raw.replace(/\D/g, "").slice(0, CODE_LENGTH);
}

/**
 * Six slots backed by one real input: typing, pasting, and deleting all behave
 * like a normal field (and screen readers see one labelled input), while the
 * slots show progress. Calls onComplete once all six digits are in.
 */
export function CodeInput({
  value,
  onChange,
  onComplete,
  disabled,
  label,
}: {
  value: string;
  onChange: (v: string) => void;
  onComplete: (v: string) => void;
  disabled?: boolean;
  label: string;
}) {
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  return (
    <div className="code">
      {Array.from({ length: CODE_LENGTH }, (_, i) => (
        <div key={i} className={`code-slot${i === value.length ? " current" : ""}`} aria-hidden="true">
          {value[i] ?? ""}
        </div>
      ))}
      <input
        ref={inputRef}
        aria-label={label}
        inputMode="numeric"
        autoComplete="one-time-code"
        pattern="[0-9]*"
        value={value}
        disabled={disabled}
        onChange={(e) => {
          const next = cleanCode(e.target.value);
          onChange(next);
          if (next.length === CODE_LENGTH) onComplete(next);
        }}
      />
    </div>
  );
}
