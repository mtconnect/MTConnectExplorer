"""One APIRouter per service — one Swagger tag each (docs/api.md §1)."""

from . import capture, datasets, events, play, simulate, system

# Included in this order; also the tag order in /api/docs.
routers = [
    system.router,
    datasets.router,
    capture.router,
    play.router,
    simulate.router,
    events.router,
]
