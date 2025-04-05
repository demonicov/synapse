import inspect
import logging
import json
import os

from synapse.events import EventBase
from synapse.app.homeserver import SynapseHomeServer
from synapse.storage.database import LoggingDatabaseConnection
from synapse.module_api import ModuleApi
import time
from typing import Any, Dict, Iterable, List, cast
from functools import wraps

UNSIGNED_KEY: str = os.environ.get("SYNAPSE_EVENT_UNSIGNED_KEY", '')
KEY: str = UNSIGNED_KEY.split('_')[0]
VISIBLE_TO = "visible_to"
DELIMITER = "⁂"
HS: SynapseHomeServer


def print_caller():
    """
    Shows which function is calling the method of this file
    WARNING: This method makes everything super-slow
    """
    stack = inspect.stack()
    frame1 = stack[1]  # Get the caller at the given stack level
    frame2 = stack[2]  # Get the caller at the given stack level
    logging.warning(f"{frame1.function} called from {frame2.function} ({frame2.filename}:{frame2.lineno})")


def filter_events(events: Iterable[EventBase], user_id: str) -> List[EventBase]:
    """
    Gets a list of events and removes the ones that are not supposed to be visible to users
    """
    return [
        event for event in events if event and (
                event.type != 'm.room.message'
                or not event.unsigned.get(UNSIGNED_KEY, '')
                or event.sender == user_id
                or user_id in get_channel_admins(event.room_id)
                or (isinstance(event.unsigned[UNSIGNED_KEY], dict) and user_id in event.unsigned[UNSIGNED_KEY].get(VISIBLE_TO, []))
        )
    ]


def filter_event_dicts(event_dicts: Iterable[Dict[str, Any]], user_id: str) -> List[Dict[str, Any]]:
    """
    Gets a list of event_dicts and removes the ones that are not supposed to be visible to users
    """
    return [
        event_dict for event_dict in event_dicts if event_dict and (
                event_dict['type'] != 'm.room.message'
                or not event_dict['unsigned'].get(UNSIGNED_KEY, '')
                or event_dict['sender'] == user_id
                or user_id in event_dict['unsigned'][UNSIGNED_KEY].get(VISIBLE_TO, [])
                or user_id in get_channel_admins(event_dict['room_id'])
        )
    ]


def filter_search_events(results: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Updates search results
    todo: refactor
    todo: visible_to is not implemented here¬
    """
    results = [
        r for r in results if
        r["result"].get("type", "") != "m.room.message"
        or not r["result"].get("unsigned", {}).get(UNSIGNED_KEY, "")
        or r["result"].get("sender", "") == requester.user.to_string()
    ]

    for result in results:
        if result["context"].get("events_before", ""):
            result["context"]["events_before"] = [
                e for e in result["context"]["events_before"] if
                e["type"] != "m.room.message"
                or not e.get("unsigned", {}).get(UNSIGNED_KEY, "")
                or e["sender"] == requester.user.to_string()
            ]

        if result["context"].get("events_after", ""):
            result["context"]["events_after"] = [
                e for e in result["context"]["events_after"] if
                e["type"] != "m.room.message"
                or not e.get("unsigned", {}).get(UNSIGNED_KEY, "")
                or e["sender"] == requester.user.to_string()
            ]

    return results


def is_visible(event: EventBase, user_id: str = None) -> bool:
    """
    Gets a single event and returns true if visible to user
    """
    return event and (
            event.type != 'm.room.message'
            or not event.unsigned.get(UNSIGNED_KEY, '')
            or event.sender == user_id
            or user_id in get_channel_admins(event.room_id)
            or (isinstance(event.unsigned[UNSIGNED_KEY], dict) and user_id in event.unsigned[UNSIGNED_KEY].get(VISIBLE_TO, []))
    )


def set_room(room_id, value):
    db_pool = HS.get_datastores().main.db_pool
    db_conn = LoggingDatabaseConnection(
        db_pool._db_pool.connect(),
        db_pool.engine,
        "overra",
    )

    cur = db_conn.cursor()
    cur.execute(
        "Update rooms set is_public = ? where room_id = ?",
        (value, room_id,)
    )

    cur.close()


def is_room_channel(room_id):
    """
    Checks if a room is a channel
    """
    db_pool = HS.get_datastores().main.db_pool
    db_conn = LoggingDatabaseConnection(
        db_pool._db_pool.connect(),
        db_pool.engine,
        "overra",
    )

    cur = db_conn.cursor()
    cur.execute(f"""
        SELECT 
            (ej.json::jsonb)->'content'->'{KEY}_channel'
        FROM 
            current_state_events cse
        JOIN 
            event_json ej 
        ON 
            cse.event_id = ej.event_id
        WHERE 
            cse.room_id = ?
            AND cse.type = 'm.room.{KEY}.channel'
        """, (room_id,))

    # for channels ('true',)
    # otherwise None
    row = cast(tuple[bool], cur.fetchone())

    cur.close()

    return row and (row[0] == True)


def is_room_public(room_id):
    db_pool = HS.get_datastores().main.db_pool
    db_conn = LoggingDatabaseConnection(
        db_pool._db_pool.connect(),
        db_pool.engine,
        "overra",
    )

    cur = db_conn.cursor()
    cur.execute("""
        SELECT 
            is_public
        FROM 
            rooms
        WHERE 
            room_id = ?
        """, (room_id,))
    row = cur.fetchone()

    return row and (row[0] == True)


def get_channel_admins(room_id: str):
    db_pool = HS.get_datastores().main.db_pool
    db_conn = LoggingDatabaseConnection(
        db_pool._db_pool.connect(),
        db_pool.engine,
        "overra",
    )

    cur = db_conn.cursor()
    cur.execute("""
        SELECT 
            (ej.json::jsonb)->'content'->'users' AS users
        FROM 
            current_state_events cse
        JOIN 
            event_json ej 
        ON 
            cse.event_id = ej.event_id
        WHERE 
            cse.room_id = ?
            AND cse.type = 'm.room.power_levels'
        """, (room_id,))
    row = cast(tuple[Dict[str, int]], cur.fetchone())

    cur.close()

    # if no admins are found - is that possible?
    if not row:
        return []

    # only users with 100 permission should see the message
    # ({"@a00043:localhost": 100},)
    data = json.loads(row[0])  # Convert string to dict
    return [user_id for user_id, role in data.items() if role == 100]


def set_zrefix(event_dict: Dict[str, Any]) -> None:
    """
    Finds the prefix in body, splitting by \uFFFC
    """
    # extract zrefix
    body_parts = event_dict.get('content', {}).get('body', '').split(DELIMITER, 1)

    if len(body_parts) != 2:
        return

    event_dict['zrefix'] = body_parts[0]
    event_dict['content']['body'] = body_parts[1]
