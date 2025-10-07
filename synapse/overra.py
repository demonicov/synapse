import inspect
import logging
import json
import os
import re
import time

from cachetools import cached, TTLCache
from pony.orm import Database, db_session
from typing import Any, Dict, Iterable, List, cast
from functools import wraps

KEY: str = os.environ.get("SYNAPSE_SPECIAL_KEY", '')
AI_RESPONSE_KEY: str = KEY + '_ai_response'
METADATA_KEY: str = KEY + '_metadata'
VISIBLE_TO = "visible_to"
# "\u2042"
ZREFIX_DELIMITER = "⁂"

HS = None


###################################################
###
### Filter Section
###
###################################################

def is_visible(event: Dict, user_id: str = None) -> bool:
    """
    Gets a single event and returns true if visible to user
    """
    # False if event is None or not set
    if not event:
        return False

    # convert to dict, we check everything in dict mode to have one code for all checks
    if not isinstance(event, dict):
        event = event.get_dict()

    # True if event is not room message
    if event['type'] != 'm.room.message':
        return True

    # get metadata and AI response
    metadata = event['unsigned'].get(METADATA_KEY, {})
    ai_response = event['unsigned'].get(AI_RESPONSE_KEY, {})

    # True if none of AI response and metadata are set
    if not metadata and not ai_response:
        return True

    # if caller does not care about the logged-in user
    if user_id is None:
        return False

    # True if user is the sender
    if user_id == event['sender']:
        return True

    # True if user admin
    if user_id in get_channel_admins(event['room_id']):
        return True

    # True if user is in visible_to set
    if metadata and user_id in metadata.get(VISIBLE_TO, []):
        return True

    # false otherwise
    return False


def filter_events(events: Iterable[Any], user_id: str) -> List[Any]:
    """
    Gets a list of events and removes the ones that are not supposed to be visible to users
    """
    return [event for event in events if is_visible(event, user_id)]


def filter_event_dicts(event_dicts: Iterable[Dict[str, Any]], user_id: str) -> List[Dict[str, Any]]:
    """
    Gets a list of event_dicts and removes the ones that are not supposed to be visible to users
    """
    return [event_dict for event_dict in event_dicts if is_visible(event_dict, user_id)]


def filter_search_events(results: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Updates search results
    todo: refactor
    todo: visible_to is not implemented here¬
    """
    results = [
        r for r in results if
        r["result"].get("type", "") != "m.room.message"
        or not r["result"].get("unsigned", {}).get(AI_RESPONSE_KEY, "")
        or r["result"].get("sender", "") == requester.user.to_string()
    ]

    for result in results:
        if result["context"].get("events_before", ""):
            result["context"]["events_before"] = [
                e for e in result["context"]["events_before"] if
                e["type"] != "m.room.message"
                or not e.get("unsigned", {}).get(AI_RESPONSE_KEY, "")
                or e["sender"] == requester.user.to_string()
            ]

        if result["context"].get("events_after", ""):
            result["context"]["events_after"] = [
                e for e in result["context"]["events_after"] if
                e["type"] != "m.room.message"
                or not e.get("unsigned", {}).get(AI_RESPONSE_KEY, "")
                or e["sender"] == requester.user.to_string()
            ]

    return results


def set_zrefix(event_dict: Dict[str, Any]) -> None:
    """
    Finds the prefix in body, splitting by "\u2042" which is ⁂
    """
    # extract zrefix
    try:
        parts = event_dict.get('content', {}).get('body', '').split(ZREFIX_DELIMITER, 1)

        if len(parts) == 2:
            # todo: remove in the future
            event_dict['zrefix'] = parts[0].strip()
            event_dict['content']['body'] = parts[1].strip()
            if event_dict['unsigned'] is None:
                event_dict['unsigned'] = {}
            event_dict['unsigned']['zrefix'] = parts[0].strip()
            return

        parts = strip_html_tags(event_dict.get('content', {}).get('formatted_body', '')).split(ZREFIX_DELIMITER, 1)

        if len(parts) == 2:
            # todo: remove in the future
            event_dict['zrefix'] = parts[0].strip()
            if event_dict['unsigned'] is None:
                event_dict['unsigned'] = {}
            event_dict['unsigned']['zrefix'] = parts[0].strip()
            event_dict['content']['formatted_body'] = '' if not parts[1].strip() else event_dict['content']['formatted_body'].replace(
                parts[0] + ZREFIX_DELIMITER,
                ''
            ).strip()
            return
    except Exception as e:
        logging.warning(f"Ignored exception {type(e)} {event_dict}")


###################################################
###
### DB Section
###
###################################################
@cached(cache=TTLCache(maxsize=128, ttl=10))
@db_session
def fetch_from_room(query: str, room_id: str):
    """
    This is written to do fetch_from_room queries
    Returns something like:
    [
        ['real_room_id', integer],
        ['real_room_id', integer],
    ]
    """
    if not hasattr(fetch_from_room, 'db'):
        fetch_from_room.db = Database()
        db_config = HS.config.database.databases[0].config['args']
        fetch_from_room.db.bind(provider='postgres', user=db_config['user'], password=db_config['password'], host=db_config['host'], database=db_config['dbname'])

    # query to get results
    return fetch_from_room.db.execute(query, {'room_id': room_id}).fetchall()


def is_room_channel(room_id):
    """
    Checks if a room is a channel
    """
    rows = fetch_from_room(f"""
        SELECT 
            (ej.json::jsonb)->'content'->'{KEY}_channel'
        FROM 
            current_state_events cse
        JOIN 
            event_json ej 
        ON 
            cse.event_id = ej.event_id
        WHERE 
            cse.room_id = $room_id
            AND cse.type = 'm.room.{KEY}.channel'
        """, room_id)

    return rows and (rows[0][0] == True or rows[0][0] == 'true')


def is_room_public(room_id):
    rows = fetch_from_room("""
        SELECT 
            is_public
        FROM 
            rooms
        WHERE 
            room_id = $room_id
        """, room_id)

    return rows and (row[0][0] == True or rows[0][0] == 'true')


def get_channel_admins(room_id: str):
    # todo: find out why new channels are not public
    # if not is_room_public(room_id):
    #     return []
    rows = fetch_from_room("""
        SELECT 
            (ej.json::jsonb)->'content'->'users' AS users
        FROM 
            current_state_events cse
        JOIN 
            event_json ej 
        ON 
            cse.event_id = ej.event_id
        WHERE 
            cse.room_id = $room_id
            AND cse.type = 'm.room.power_levels'
        """, room_id)

    # if no admins are found - is that possible?
    if not rows:
        return []

    # only users with 100 permission should see the message
    # ({"@a00043:localhost": 100},)
    data = json.loads(rows[0][0])  # Convert string to dict
    return [user_id for user_id, role in data.items() if role == 100]


###################################################
###
### Utils Section
###
###################################################

def print_caller():
    """
    Shows which function is calling the method of this file
    WARNING: This method makes everything super-slow
    """
    stack = inspect.stack()
    frame1 = stack[1]  # Get the caller at the given stack level
    frame2 = stack[2]  # Get the caller at the given stack level
    logging.warning(f"{frame1.function} called from {frame2.function} ({frame2.filename}:{frame2.lineno})")


def strip_html_tags(text: str):
    """
    Removes html tags from string
    """
    if not text:
        return ''
    return re.sub(r'<.*?>', '', text).strip()
