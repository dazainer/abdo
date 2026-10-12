"""Shared money, as pure functions over integer cents.

Floats never touch money: amounts are parsed with Decimal into integer cents, splits are
exact to the cent, and balances always sum to zero. The model never does this arithmetic;
it reads the results from the tools.
"""
from decimal import Decimal, InvalidOperation

MAX_CENTS = 10_000_00      # $10,000: a typo guard, not a policy


class MoneyError(ValueError):
    """An amount we won't record. The message is safe to show."""


def parse_amount(raw) -> int:
    """'42', '42.5', '$42.50', '1,200', 42.5 -> cents. Rejects ≤ 0, sub-cent, and absurd values."""
    text = str(raw).strip().replace("$", "").replace(",", "").replace("CAD", "").strip()
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise MoneyError(f"'{raw}' isn't an amount")
    if not value.is_finite() or value <= 0:
        raise MoneyError("the amount has to be more than $0")
    if value != value.quantize(Decimal("0.01")):
        raise MoneyError("amounts go to the cent at most")
    cents = int(value * 100)
    if cents > MAX_CENTS:
        raise MoneyError("that's over $10,000; double-check the amount")
    return cents


def fmt(cents: int) -> str:
    """1050 -> '$10.50' (always positive; callers say who owes whom)."""
    return f"${abs(cents) // 100:,}.{abs(cents) % 100:02d}"


def split(amount_cents: int, member_ids: list[int]) -> dict[int, int]:
    """Equal shares that sum exactly: everyone gets amount // n, and the first
    amount % n people (lowest id first, so it's stable) get one extra cent."""
    ids = sorted(set(member_ids))
    if not ids:
        raise MoneyError("an expense has to be split with at least one person")
    base, extra = divmod(amount_cents, len(ids))
    return {m: base + (1 if i < extra else 0) for i, m in enumerate(ids)}


def balances(member_ids, expenses, shares, settlements) -> dict[int, int]:
    """Net per member in cents: positive = the suite owes them, negative = they owe.

    expenses: rows with paid_by, amount_cents. shares: rows with member_id, share_cents.
    settlements: rows with from_member, to_member, amount_cents. (Deleted rows already
    filtered out by the caller.) Paying someone back raises your net and lowers theirs.
    """
    net = {m: 0 for m in member_ids}
    for e in expenses:
        net[e["paid_by"]] = net.get(e["paid_by"], 0) + e["amount_cents"]
    for s in shares:
        net[s["member_id"]] = net.get(s["member_id"], 0) - s["share_cents"]
    for t in settlements:
        net[t["from_member"]] = net.get(t["from_member"], 0) + t["amount_cents"]
        net[t["to_member"]] = net.get(t["to_member"], 0) - t["amount_cents"]
    return net


def settle_up(net: dict[int, int]) -> list[tuple[int, int, int]]:
    """Fewest-transfers plan [(from, to, cents)]: the biggest debtor pays the biggest
    creditor, repeatedly. At most n-1 transfers; afterwards everyone is at zero."""
    debtors = sorted(((-v, m) for m, v in net.items() if v < 0), reverse=True)
    creditors = sorted(((v, m) for m, v in net.items() if v > 0), reverse=True)
    plan = []
    i = j = 0
    debt = [list(d) for d in debtors]
    cred = [list(c) for c in creditors]
    while i < len(debt) and j < len(cred):
        pay = min(debt[i][0], cred[j][0])
        plan.append((debt[i][1], cred[j][1], pay))
        debt[i][0] -= pay
        cred[j][0] -= pay
        if debt[i][0] == 0:
            i += 1
        if cred[j][0] == 0:
            j += 1
    return plan
