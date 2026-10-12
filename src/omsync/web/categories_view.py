"""How the Categories table is viewed: its sort, its type filter, and whether hidden categories
show. The state is not stored. It travels in the query string of every request the table makes, so
a save, a bulk change, a hide, or a reload keeps the table as it was.

Registered in app.py: `category_type_options` and `category_type_labels` for the templates.
"""

from dataclasses import dataclass, replace
from urllib.parse import urlencode

from omsync.ledger.categories import TYPE_LABELS
from omsync.ledger.categories_admin import CATEGORY_SORTS
from omsync.ledger.models import CategoryType

# (value, label) in the order of CategoryType, for a dropdown.
TYPE_OPTIONS: tuple[tuple[str, str], ...] = tuple(
    (category_type.value, TYPE_LABELS[category_type]) for category_type in CategoryType
)
DEFAULT_SORT = "name"


@dataclass(frozen=True)
class TableView:
    show_hidden: bool = False
    type: CategoryType | None = None  # show only this type, or every type
    sort: str = DEFAULT_SORT
    descending: bool = False

    @classmethod
    def parse(cls, show_hidden: int, category_type: str, sort: str, direction: str) -> "TableView":
        """The view a request asks for. A value it does not know falls back to the default, so a
        stale or edited link still shows the table."""
        return cls(
            show_hidden=bool(show_hidden),
            type=(
                CategoryType(category_type)
                if category_type in CategoryType._value2member_map_
                else None
            ),
            sort=sort if sort in CATEGORY_SORTS else DEFAULT_SORT,
            descending=direction == "desc",
        )

    @property
    def query(self) -> str:
        """The view as a query string, without the values that are the default."""
        values: dict[str, str] = {}
        if self.show_hidden:
            values["show_hidden"] = "1"
        if self.type is not None:
            values["type"] = self.type.value
        if self.sort != DEFAULT_SORT or self.descending:
            values["sort"] = self.sort
        if self.descending:
            values["dir"] = "desc"
        return urlencode(values)

    @property
    def filtered(self) -> bool:
        return self.type is not None

    def cleared(self) -> "TableView":
        """The same view with no type filter."""
        return replace(self, type=None)

    def sorted_by(self, column: str) -> "TableView":
        """The view after clicking a column's header: its other direction when it already sorts the
        table, otherwise ascending."""
        if column == self.sort:
            return replace(self, descending=not self.descending)
        return replace(self, sort=column, descending=False)

    def aria_sort(self, column: str) -> str:
        if column != self.sort:
            return "none"
        return "descending" if self.descending else "ascending"
