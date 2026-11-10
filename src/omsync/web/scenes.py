"""The sign-in scenes' ids and picker labels, in picker order.

The scenes come from the omsync-nursery package, which follows the nursery's release branch
(uv.lock records the commit; upgrade it with scripts/upgrade-nursery.sh). Its scenes.json names
them, so a new scene reaches the sign-in page and the user menu without a template change.
"""

import functools
import json

from nursery.assets import SCENES_JSON


@functools.cache
def nursery_scenes() -> tuple[tuple[str, str], ...]:
    """Each scene as (id, label); the ids match SCENES in nursery.js."""
    return tuple((s["id"], s["label"]) for s in json.loads(SCENES_JSON.read_text()))
