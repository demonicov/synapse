import os
from typing import Iterable, List

import logging

from synapse.events import EventBase
import types


class Overra:
    key: str = os.environ.get("SYNAPSE_EVENT_UNSIGNED_KEY", '')

    @staticmethod
    def filter_events(events: Iterable[EventBase], user_id: str) -> List[EventBase]:
        logging.warning("Running hpm filter_events")
        return [
            event for event in events if event and (
                    event.type != 'm.room.message'
                    or not event.unsigned.get(Overra.key, '')
                    or event.sender == user_id
                    or user_id in event.unsigned.get(Overra.key, {}).get("visible_to", [])
            )
        ]
