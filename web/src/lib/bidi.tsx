// Mixed Egyptian Arabic + English in one message. A plain dir="auto" bubble takes the
// direction of its first word, so in a reply that opens in Arabic, an English sentence's
// "?" or "." lands on the wrong side ("?for the suite today"). Here each line gets its own
// direction, and each sentence (or clause after a dash) is isolated so its punctuation
// stays with it.

const ARABIC = /[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]/;
const LATIN = /[A-Za-z]/;

/** "rtl" if the first letter is Arabic, else "ltr". */
export function dirOf(text: string): "rtl" | "ltr" {
  for (const ch of text) {
    if (ARABIC.test(ch)) return "rtl";
    if (LATIN.test(ch)) return "ltr";
  }
  return "ltr";
}

// Where a line splits into isolated pieces: a spaced dash between clauses, or the space
// after a sentence end (the mark stays with its sentence). No regex lookbehind: older iOS
// Safari can't parse it, and a parse error would blank the whole app.
const SEP = /\s+[—–-]\s+|[.!?؟]\s+/g;
const END = /^[.!?؟]/;

/** [piece, separator, piece, separator, ..., piece] */
export function pieces(text: string): string[] {
  const out: string[] = [];
  let last = 0;
  for (const m of text.matchAll(SEP)) {
    const keep = END.test(m[0]) ? 1 : 0;
    const at = m.index ?? 0;
    out.push(text.slice(last, at + keep), m[0].slice(keep));
    last = at + m[0].length;
  }
  out.push(text.slice(last));
  return out;
}

function Line({ text }: { text: string }) {
  const parts = pieces(text);
  return (
    <div className="bidi-line" dir={dirOf(text)}>
      {parts.map((p, i) => (i % 2 === 1 ? p : p ? <bdi key={i}>{p}</bdi> : null))}
      {!text && " "}
    </div>
  );
}

/** A message body with every line and sentence reading in its own direction. */
export function BidiText({ text }: { text: string }) {
  return <>{text.split("\n").map((line, i) => <Line key={i} text={line} />)}</>;
}
