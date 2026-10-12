"""Enum columns stored as their string values, with a CHECK constraint on the allowed values."""

import re
from enum import StrEnum

from sqlalchemy import Enum


def string_enum(enum_cls: type[StrEnum]) -> Enum:
    """Return a column type that stores a StrEnum as its value, e.g. "credit_card".

    native_enum=False keeps the column portable: a VARCHAR, not a database ENUM type.
    The CHECK constraint rejects strings that are not members of the enum.
    """
    constraint_name = re.sub(r"(?<!^)(?=[A-Z])", "_", enum_cls.__name__).lower()
    return Enum(
        enum_cls,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        values_callable=lambda members: [member.value for member in members],
        name=constraint_name,
    )
