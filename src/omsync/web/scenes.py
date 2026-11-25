"""The sign-in scenes' ids, in picker order.

The scenes come from the omsync-nursery package, which follows the nursery's release branch
(uv.lock records the commit; upgrade it with scripts/upgrade-nursery.sh). The nursery's picker draws
them in the browser. The pages need the ids before first paint, to restore the saved scene, so a
new scene reaches the sign-in page and the user menu without a template change.
"""

import functools
import json

from nursery.assets import SCENES_JSON


@functools.cache
def nursery_scene_ids() -> tuple[str, ...]:
    """Each scene's id; the ids match SCENES in nursery.js."""
    return tuple(s["id"] for s in json.loads(SCENES_JSON.read_text()))
