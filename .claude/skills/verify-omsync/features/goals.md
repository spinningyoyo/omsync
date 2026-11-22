# Goals

Savings goals with their progress, and each goal's page.

## Sub-features

- list: `/plan/goals`, "Add goal".
- goal-page: `/plan/goals/<id>`: progress, "Edit", "Archive", "Delete", "Back to goals".
- archive: "Archive goal" / "Restore".
- delete: "Delete" asks in a dialog; the dialog's "Delete".

## How to get to it (user POV)

- The rail's Plan → Goals, or Home's "All goals".

## Driving it with verify.py

Preconditions: any mode.

- Add: "Add goal" → fill → "Add goal"; it is listed; reload.
- Edit: open it → "Edit" → change → "Save"; reload.
- Archive: "Archive" → it leaves the active list; "Restore".
- Delete: "Delete" → dialog → "Delete"; it is gone; the toast's Undo brings it back.

## Gotchas

- Delete on the goal page asks first; the button is plain, not red.
