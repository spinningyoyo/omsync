"""Money as integer cents. Floats are rejected everywhere."""

import re
from collections.abc import Sequence
from decimal import Decimal

# "1,234.56" with thousands separators, or "1234.56" without.
_GROUPED = re.compile(r"[+-]?[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?")
_PLAIN = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)")


def to_cents(value: str | Decimal | int) -> int:
    """Convert a money value to integer cents.

    An int is already cents. A str or Decimal is dollars: at most two significant decimal
    places ("1.500" is fine, "1.005" is not). Commas are allowed as thousands separators in
    strings. Floats are rejected with TypeError.
    """
    if isinstance(value, float):
        raise TypeError("money must not be a float; pass a str, Decimal, or int cents")
    if isinstance(value, bool) or not isinstance(value, int | str | Decimal):
        raise TypeError(f"money must be a str, Decimal, or int, not {type(value).__name__}")
    if isinstance(value, int):
        return value
    decimal = _parse(value) if isinstance(value, str) else value
    return _decimal_to_cents(decimal)


def format_cents(cents: int, symbol: str = "$") -> str:
    """Format cents for display, e.g. -123456 becomes "-$1,234.56"."""
    _require_int(cents)
    sign = "-" if cents < 0 else ""
    dollars, remainder = divmod(abs(cents), 100)
    return f"{sign}{symbol}{dollars:,}.{remainder:02d}"


def allocate(cents: int, weights: Sequence[int]) -> list[int]:
    """Split cents in proportion to weights, using the largest-remainder method.

    The shares always sum exactly to `cents`. Ties go to the earlier index, so the result is
    deterministic. Negative cents yield negative shares of the same magnitude split.
    Zero weights get zero. Raises ValueError for empty or all-zero weights.
    """
    _require_int(cents)
    if not weights:
        raise ValueError("weights must not be empty")
    for weight in weights:
        if isinstance(weight, bool) or not isinstance(weight, int):
            raise TypeError(f"weights must be ints, got {weight!r}")
        if weight < 0:
            raise ValueError(f"weights must not be negative, got {weight}")
    total = sum(weights)
    if total == 0:
        raise ValueError("weights must not all be zero")

    magnitude = abs(cents)
    shares = [magnitude * weight // total for weight in weights]
    remainders = [magnitude * weight % total for weight in weights]
    leftover = magnitude - sum(shares)  # always less than len(weights)
    by_remainder = sorted(range(len(weights)), key=lambda i: (-remainders[i], i))
    for index in by_remainder[:leftover]:
        shares[index] += 1

    sign = -1 if cents < 0 else 1
    return [sign * share for share in shares]


def _parse(text: str) -> Decimal:
    """Parse a dollar string, allowing thousands separators but no exponents or symbols."""
    cleaned = text.strip()
    if "," in cleaned:
        if not _GROUPED.fullmatch(cleaned):
            raise ValueError(f"misplaced thousands separator in {text!r}")
        cleaned = cleaned.replace(",", "")
    if not _PLAIN.fullmatch(cleaned):
        raise ValueError(f"not a money amount: {text!r}")
    return Decimal(cleaned)


def _decimal_to_cents(value: Decimal) -> int:
    """Convert a Decimal to cents exactly, without relying on the decimal context."""
    if not value.is_finite():
        raise ValueError("money must be a finite number")
    _sign, digits, exponent = value.as_tuple()
    assert isinstance(exponent, int)  # finite values always have an integer exponent
    coefficient = int("".join(map(str, digits)))
    shift = exponent + 2  # cents = coefficient * 10**shift
    if shift >= 0:
        cents = coefficient * 10**shift
    else:
        divisor = 10**-shift
        if coefficient % divisor:
            raise ValueError(f"more than 2 decimal places: {value}")
        cents = coefficient // divisor
    return -cents if value.is_signed() else cents


def _require_int(cents: int) -> None:
    if isinstance(cents, bool) or not isinstance(cents, int):
        raise TypeError(f"cents must be an int, not {type(cents).__name__}")
