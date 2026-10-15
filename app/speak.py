"""Numbers to words, right before text-to-speech.

The voice prompt asks Abdo to write numbers as words, but the model still slips ("٣٠",
"$12.50", "7:30"), and ElevenLabs then reads digits badly: Flash v2.5 skips number
normalization, and multilingual v2 reads Arabic digits in MSA or not at all. So every reply
is normalized here, deterministically, before synthesis: Egyptian Arabic words when the
number sits in Arabic text, English words when it sits in English. The text shown in chat
keeps its digits; only what's spoken changes.

Covers: Arabic-Indic digits, money ($12.50), clock times (7:30, with am/pm), percentages,
decimals, plain integers, years, and long digit strings (read digit by digit, like a phone
number or code).
"""
import re

_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٫٬", "01234567890123456789.,")

# ---- Egyptian Arabic --------------------------------------------------------------------
_UNITS = ["صفر", "واحد", "اتنين", "تلاتة", "أربعة", "خمسة", "ستة", "سبعة", "تمانية", "تسعة", "عشرة"]
_TEENS = {11: "حداشر", 12: "اتناشر", 13: "تلتاشر", 14: "أربعتاشر", 15: "خمستاشر", 16: "ستاشر",
          17: "سبعتاشر", 18: "تمنتاشر", 19: "تسعتاشر"}
_TENS = {20: "عشرين", 30: "تلاتين", 40: "أربعين", 50: "خمسين", 60: "ستين", 70: "سبعين",
         80: "تمانين", 90: "تسعين"}
_HUNDREDS = {100: "مية", 200: "ميتين", 300: "تلتمية", 400: "ربعمية", 500: "خمسمية", 600: "ستمية",
             700: "سبعمية", 800: "تمنمية", 900: "تسعمية"}
_THOUSANDS_FEW = {3: "تلات", 4: "أربع", 5: "خمس", 6: "ست", 7: "سبع", 8: "تمن", 9: "تسع", 10: "عشر"}


def ar_words(n: int) -> str:
    """An integer in Egyptian Arabic words: 25 -> "خمسة وعشرين", 2026 -> "ألفين وستة وعشرين"."""
    if n < 0:
        return "سالب " + ar_words(-n)
    if n <= 10:
        return _UNITS[n]
    if n < 20:
        return _TEENS[n]
    if n < 100:
        t, u = divmod(n, 10)
        return _TENS[t * 10] if u == 0 else f"{_UNITS[u]} و{_TENS[t * 10]}"
    if n < 1000:
        h, r = divmod(n, 100)
        return _HUNDREDS[h * 100] + (f" و{ar_words(r)}" if r else "")
    if n < 1_000_000:
        k, r = divmod(n, 1000)
        head = ("ألف" if k == 1 else "ألفين" if k == 2 else f"{_THOUSANDS_FEW[k]} تلاف" if k <= 10
                else f"{ar_words(k)} ألف")
        return head + (f" و{ar_words(r)}" if r else "")
    m, r = divmod(n, 1_000_000)
    head = ("مليون" if m == 1 else "مليونين" if m == 2 else f"{ar_words(m)} ملايين" if m <= 10
            else f"{ar_words(m)} مليون")
    return head + (f" و{ar_words(r)}" if r else "")


def _ar_money(dollars: int, cents: int) -> str:
    def unit(n, one, two):
        return one if n == 1 else two if n == 2 else f"{ar_words(n)} {one}"
    parts = []
    if dollars or not cents:
        parts.append(unit(dollars, "دولار", "دولارين"))
    if cents:
        parts.append(unit(cents, "سنت", "سنتين"))
    return " و".join(parts)


def _hour(h: int) -> str:
    """Clock hours: "الساعة واحدة" (feminine one), the rest as plain numbers."""
    return "واحدة" if h == 1 else ar_words(h)


def _ar_time(h: int, m: int, suffix: str | None) -> str:
    h12 = h % 12 or 12
    a, b = _hour(h12), _hour(h12 % 12 + 1)
    named = {0: a, 15: f"{a} وربع", 20: f"{a} وتلت", 30: f"{a} ونص", 40: f"{b} إلا تلت",
             45: f"{b} إلا ربع", 5: f"{a} وخمسة", 10: f"{a} وعشرة", 25: f"{a} ونص إلا خمسة",
             35: f"{a} ونص وخمسة", 50: f"{b} إلا عشرة", 55: f"{b} إلا خمسة"}
    out = named.get(m, f"{a} و{ar_words(m)} دقيقة")
    hour24 = h if suffix is None else (h % 12 + (12 if suffix == "pm" else 0))
    if suffix or h > 12:
        out += " الصبح" if hour24 < 12 else " الضهر" if hour24 < 15 else " العصر" if hour24 < 18 else " بالليل"
    return out


# ---- English --------------------------------------------------------------------------------
_EN_UNITS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
             "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_EN_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


def en_words(n: int) -> str:
    if n < 0:
        return "minus " + en_words(-n)
    if n < 20:
        return _EN_UNITS[n]
    if n < 100:
        t, u = divmod(n, 10)
        return _EN_TENS[t] + (f"-{_EN_UNITS[u]}" if u else "")
    if n < 1000:
        h, r = divmod(n, 100)
        return f"{_EN_UNITS[h]} hundred" + (f" {en_words(r)}" if r else "")
    for size, name in ((1_000_000, "million"), (1000, "thousand")):
        if n >= size:
            q, r = divmod(n, size)
            return f"{en_words(q)} {name}" + (f" {en_words(r)}" if r else "")
    return str(n)


def _en_money(dollars: int, cents: int) -> str:
    parts = []
    if dollars or not cents:
        parts.append(f"{en_words(dollars)} dollar{'' if dollars == 1 else 's'}")
    if cents:
        parts.append(f"{en_words(cents)} cent{'' if cents == 1 else 's'}")
    return " and ".join(parts)


def _en_time(h: int, m: int, suffix: str | None) -> str:
    h12 = h % 12 or 12
    mins = "o'clock" if m == 0 else f"oh {en_words(m)}" if m < 10 else en_words(m)
    out = f"{en_words(h12)} {mins}"
    return out + (f" {suffix[0]} m" if suffix else "")


# ---- the pass over a reply ----------------------------------------------------------------
_TOKEN = re.compile(
    r"(?P<money>\$\s?(?P<d>\d[\d,]*)(?:\.(?P<c>\d{1,2}))?)"
    r"|(?P<time>\b(?P<h>[01]?\d|2[0-3]):(?P<m>[0-5]\d)\b(?:\s*(?P<ap>[ap])\.?\s?m\b\.?)?)"
    r"|(?P<pct>(?P<p>\d+(?:\.\d+)?)\s?%)"
    r"|(?P<dec>\d+\.\d+)"
    r"|(?P<int>\d[\d,]*\d|\d)",
    re.IGNORECASE,
)
_LETTER = re.compile(r"[A-Za-z؀-ۿ]")


def _lang_at(text: str, start: int, end: int) -> str:
    """'en' if the nearest letter before the number (else after it) is Latin, otherwise 'ar'."""
    before = _LETTER.findall(text[max(0, start - 30):start])
    if before:
        return "en" if before[-1].isascii() else "ar"
    after = _LETTER.findall(text[end:end + 30])
    return "en" if after and after[0].isascii() else "ar"


def _digits_one_by_one(s: str, lang: str) -> str:
    words = [(ar_words if lang == "ar" else en_words)(int(c)) for c in s if c.isdigit()]
    return "، ".join(words) if lang == "ar" else ", ".join(words)


def normalize(text: str) -> str:
    """The reply with every number written out as words, ready for TTS."""
    text = text.translate(_ARABIC_DIGITS)

    def repl(mt: re.Match) -> str:
        lang = _lang_at(text, mt.start(), mt.end())
        if mt["money"]:
            dollars = int(mt["d"].replace(",", ""))
            cents = int((mt["c"] or "0").ljust(2, "0"))
            return _ar_money(dollars, cents) if lang == "ar" else _en_money(dollars, cents)
        if mt["time"]:
            ap = (mt["ap"] or "").lower()
            suffix = {"a": "am", "p": "pm"}.get(ap)
            h, m = int(mt["h"]), int(mt["m"])
            return _ar_time(h, m, suffix) if lang == "ar" else _en_time(h, m, suffix)
        if mt["pct"]:
            num = _number(mt["p"], lang)
            return f"{num} في المية" if lang == "ar" else f"{num} percent"
        if mt["dec"]:
            return _number(mt["dec"], lang)
        raw = mt["int"]
        plain = raw.replace(",", "")
        if (len(plain) >= 5 and "," not in raw) or (len(plain) > 1 and plain.startswith("0")):
            return _digits_one_by_one(plain, lang)      # phone numbers, codes, ids
        return (ar_words if lang == "ar" else en_words)(int(plain))

    return _TOKEN.sub(repl, text)


def _number(s: str, lang: str) -> str:
    if "." not in s:
        return (ar_words if lang == "ar" else en_words)(int(s))
    whole, frac = s.split(".", 1)
    w = int(whole or "0")
    if lang == "ar":
        if frac.rstrip("0") == "5":
            return f"{ar_words(w)} ونص"
        return f"{ar_words(w)} فاصلة {_digits_one_by_one(frac, 'ar')}"
    return f"{en_words(w)} point {_digits_one_by_one(frac, 'en').replace(',', '')}"
