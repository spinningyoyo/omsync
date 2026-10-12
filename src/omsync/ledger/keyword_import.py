"""Keyword files: read a JSON file of category names and patterns, and import it as keywords.

A keyword file is a JSON object that maps each category name to a list of regular expressions.
A trailing comma before ] or } is tolerated, with a warning. Errors name the line and column,
or the category and pattern index. They never repeat a pattern's text, because patterns can be
personal.

plan_keyword_import writes nothing. apply_keyword_import writes its plan as one batch.
"""

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from omsync.core.batch_models import Batch
from omsync.core.batches import (
    BatchError,
    Change,
    Preview,
    applied_batch_for_key,
    apply,
    make_preview,
    next_primary_key,
)
from omsync.ledger.categorize import compile_pattern, is_list_key
from omsync.ledger.models import Category, CategoryType, Keyword

# A string literal, or a comma directly before ] or }. Strings are matched first, so a comma
# inside a pattern such as "[a-z,]" is never a candidate.
_STRING_OR_TRAILING_COMMA = re.compile(r'("(?:[^"\\\n]|\\.)*")|,(\s*[\]}])')


class KeywordFileError(ValueError):
    """A keyword file cannot be imported. The message never repeats a pattern's text."""


@dataclass(frozen=True)
class KeywordImportPlan:
    """What importing a keyword file would do. Nothing is written until apply_keyword_import."""

    preview: Preview
    list_key: str
    new_categories: int
    new_keywords: int
    skipped_duplicates: int


def read_keyword_file(path: Path) -> tuple[dict[str, list[str]], list[str]]:
    """Read a keyword file into {category name: [patterns]}, and the warnings about its repair.

    Raises KeywordFileError for unreadable text, invalid JSON, a wrong shape, a non-string or
    empty pattern, or a pattern that is not a valid regular expression.
    """
    try:
        data = path.read_bytes()
    except OSError:
        raise KeywordFileError(f"cannot read {path.name}") from None
    return parse_keyword_file(data, path.name)


def parse_keyword_file(data: bytes, name: str) -> tuple[dict[str, list[str]], list[str]]:
    """Read the bytes of a keyword file called `name`, as read_keyword_file does a path."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise KeywordFileError(f"{name} is not UTF-8 text") from None

    warnings: list[str] = []
    text = _remove_trailing_commas(text, warnings)
    repeated: list[str] = []
    try:
        parsed = json.loads(text, object_pairs_hook=_combining_object(repeated))
    except json.JSONDecodeError as exc:
        raise KeywordFileError(
            f"{name} is not valid JSON: {exc.msg} at line {exc.lineno}, column {exc.colno}"
        ) from None
    if not isinstance(parsed, dict):
        raise KeywordFileError(
            f"{name} must be a JSON object that maps category names to lists of patterns"
        )

    mapping: dict[str, list[str]] = {}
    for raw_name, patterns in parsed.items():
        category = raw_name.strip()
        if not category:
            raise KeywordFileError(f"{name} has a category with an empty name")
        if not isinstance(patterns, list):
            raise KeywordFileError(f'category "{category}" must map to a list of patterns')
        for index, pattern in enumerate(patterns):
            _check_pattern(category, index, pattern)
        if category in mapping:
            repeated.append(category)
        mapping.setdefault(category, []).extend(patterns)

    for category in dict.fromkeys(name.strip() for name in repeated):
        warnings.append(f'category "{category}" appears more than once; its patterns are combined')
    return mapping, warnings


def parse_keyword_mapping(text: str) -> dict[str, list[str]]:
    """A mapping from read_keyword_file that was carried as JSON text: category names that map to
    lists of strings. Raises KeywordFileError for any other shape. plan_keyword_import checks the
    patterns."""
    try:
        parsed = json.loads(text)
    except ValueError:
        raise KeywordFileError("the keyword mapping is not valid JSON") from None
    if not isinstance(parsed, dict) or not all(
        isinstance(name, str)
        and isinstance(patterns, list)
        and all(isinstance(pattern, str) for pattern in patterns)
        for name, patterns in parsed.items()
    ):
        raise KeywordFileError("the keyword mapping must map category names to lists of patterns")
    return parsed


def plan_keyword_import(
    session: Session,
    mapping: Mapping[str, Sequence[str]],
    *,
    list_key: str,
) -> KeywordImportPlan:
    """Plan adding a keyword file's categories and patterns to one list. Writes nothing.

    Missing categories are created unassigned. A pattern that already exists in this list, or
    repeats within the file, is skipped. Raises ValueError for a bad list key, and
    KeywordFileError for a bad pattern.
    """
    _check_list_key(list_key)

    category_ids: dict[str, int] = dict(session.execute(select(Category.name, Category.id)).all())
    existing = set(session.scalars(select(Keyword.pattern).where(Keyword.list_key == list_key)))
    next_category = next_primary_key(session, Category.__table__)
    next_keyword = next_primary_key(session, Keyword.__table__)

    changes: list[Change] = []
    planned: set[str] = set()
    new_categories = new_keywords = skipped = 0
    for category, patterns in mapping.items():
        if not category.strip():
            raise KeywordFileError("a category has an empty name")
        if category not in category_ids:
            category_ids[category] = next_category
            changes.append(
                Change(
                    "category",
                    str(next_category),
                    None,
                    {
                        "id": next_category,
                        "name": category,
                        "type": CategoryType.unassigned.value,
                        "hidden": False,
                    },
                )
            )
            next_category += 1
            new_categories += 1
        for index, pattern in enumerate(patterns):
            _check_pattern(category, index, pattern)
            if pattern in existing or pattern in planned:
                skipped += 1
                continue
            planned.add(pattern)
            changes.append(
                Change(
                    "keyword",
                    str(next_keyword),
                    None,
                    {
                        "id": next_keyword,
                        "list_key": list_key,
                        "pattern": pattern,
                        "is_regex": True,
                        "category_id": category_ids[category],
                        "priority": 0,
                        "enabled": True,
                    },
                )
            )
            next_keyword += 1
            new_keywords += 1

    preview = make_preview(
        session,
        "keyword_import",
        changes,
        summary={
            "list": list_key,
            "new_categories": new_categories,
            "new_keywords": new_keywords,
            "skipped_duplicates": skipped,
        },
    )
    return KeywordImportPlan(
        preview=preview,
        list_key=list_key,
        new_categories=new_categories,
        new_keywords=new_keywords,
        skipped_duplicates=skipped,
    )


def apply_keyword_import(
    session: Session, plan: KeywordImportPlan, *, idempotency_key: str
) -> Batch:
    """Write a keyword import as one batch. The same key applies at most once.

    Raises BatchError if nothing would be added.
    """
    replay = applied_batch_for_key(session, idempotency_key)
    if replay is not None:
        return replay
    if not plan.preview.changes:
        raise BatchError("nothing to import: every pattern is already a keyword in this list")
    return apply(session, plan.preview, idempotency_key=idempotency_key)


def _check_list_key(list_key: str) -> None:
    if is_list_key(list_key):
        return
    raise ValueError("the list must be unique, generic, or a lowercase vendor key such as amazon")


def _check_pattern(category: str, index: int, pattern: object) -> None:
    """Refuse a pattern that is not a string, is blank, or is not a valid regex.

    Messages name the category and index, never the pattern text.
    """
    where = f'category "{category}", pattern index {index}'
    if not isinstance(pattern, str):
        raise KeywordFileError(f"{where} is not a string")
    if not pattern.strip():
        raise KeywordFileError(f"{where} is empty")
    try:
        compile_pattern(pattern, is_regex=True)
    except re.error:
        raise KeywordFileError(f"{where} is not a valid regular expression") from None


def _remove_trailing_commas(text: str, warnings: list[str]) -> str:
    """Replace each comma that comes just before ] or } with a space, and record a warning.

    A space keeps every later character at its line and column, so JSON errors still point at
    the right place in the file.
    """

    def repair(match: re.Match[str]) -> str:
        if match.group(1) is not None:
            return match.group(1)
        line = text.count("\n", 0, match.start()) + 1
        warnings.append(f"line {line}: removed a trailing comma")
        return " " + match.group(2)

    return _STRING_OR_TRAILING_COMMA.sub(repair, text)


def _combining_object(repeated: list[str]) -> Callable[[list[tuple[str, Any]]], dict[str, Any]]:
    """An object hook that joins the lists of a repeated key, and records the key.

    Without it, json keeps only the last list, and the patterns of the earlier one are lost.
    """

    def build(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                repeated.append(key)
                if isinstance(result[key], list) and isinstance(value, list):
                    value = result[key] + value
            result[key] = value
        return result

    return build
