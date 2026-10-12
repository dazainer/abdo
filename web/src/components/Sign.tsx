// A Cairo street sign: Arabic on top, English beneath, on an enamel plate.
export function Sign({ ar, en, large = false }: { ar: string; en: string; large?: boolean }) {
  return (
    <h1 className={`plate sign${large ? " sign-large" : ""}`} style={{ margin: 0 }}>
      <span className="sign-ar" lang="ar" dir="rtl">{ar}</span>
      <span className="sign-en" lang="en">{en}</span>
    </h1>
  );
}
