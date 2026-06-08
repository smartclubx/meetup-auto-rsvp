#!/usr/bin/env python3
"""Automatically RSVP to new Meetup events for a configured group."""

import json
import os
import random
import sys
from pathlib import Path

import requests

GRAPHQL_URL = "https://api.meetup.com/gql"
SEEN_EVENTS_FILE = Path("seen_events.json")

FETCH_EVENTS_QUERY = """
{
  groupByUrlname(urlname: "%s") {
    unifiedEvents(input: { first: 20, status: UPCOMING }) {
      edges {
        node {
          id
          title
          dateTime
          eventUrl
        }
      }
    }
  }
}
"""

RSVP_MUTATION = """
mutation {
  rsvp(input: { eventId: "%s", rsvp: YES, guestCount: %d }) {
    rsvp {
      id
      status
      guestCount
    }
  }
}
"""


def load_cookies() -> dict[str, str]:
    raw = os.environ.get("MEETUP_COOKIES")
    if not raw:
        print("Error: MEETUP_COOKIES environment variable is required.", file=sys.stderr)
        sys.exit(1)
    try:
        cookies = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"Error: MEETUP_COOKIES is not valid JSON: {exc}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(cookies, dict):
        print("Error: MEETUP_COOKIES must be a JSON object.", file=sys.stderr)
        sys.exit(1)
    return cookies


def load_seen_events() -> list[str]:
    if not SEEN_EVENTS_FILE.exists():
        SEEN_EVENTS_FILE.write_text("[]", encoding="utf-8")
        return []
    with SEEN_EVENTS_FILE.open(encoding="utf-8") as handle:
        data = json.load(handle)
    return data if isinstance(data, list) else []


def save_seen_events(seen_events: list[str]) -> None:
    with SEEN_EVENTS_FILE.open("w", encoding="utf-8") as handle:
        json.dump(seen_events, handle, indent=2)


def gql_request(session: requests.Session, query: str) -> dict:
    response = session.post(GRAPHQL_URL, json={"query": query}, timeout=30)
    response.raise_for_status()
    return response.json()


def main() -> None:
    cookies = load_cookies()
    group_urlname = os.environ.get("MEETUP_GROUP_URLNAME", "opencircleclub")

    session = requests.Session()
    for name, value in cookies.items():
        session.cookies.set(name, value)
    session.headers.update(
        {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
    )
    if "MEETUP_CSRF" in cookies:
        session.headers["x-csrf-token"] = cookies["MEETUP_CSRF"]

    seen_events = load_seen_events()
    seen_set = set(seen_events)

    try:
        result = gql_request(session, FETCH_EVENTS_QUERY % group_urlname)
    except requests.RequestException as exc:
        print(f"Error fetching events: {exc}", file=sys.stderr)
        sys.exit(1)

    if "errors" in result:
        for error in result["errors"]:
            print(f"Error fetching events: {error.get('message', error)}", file=sys.stderr)
        sys.exit(1)

    group = result.get("data", {}).get("groupByUrlname")
    if not group:
        print(f"No group found for urlname: {group_urlname}", file=sys.stderr)
        sys.exit(1)

    edges = group.get("unifiedEvents", {}).get("edges", [])
    events = [edge["node"] for edge in edges if edge.get("node")]
    new_events = [event for event in events if event["id"] not in seen_set]

    if not new_events:
        print("No new events to RSVP.")
        return

    for event in new_events:
        event_id = event["id"]
        title = event.get("title", "Unknown")
        guest_count = random.randint(1, 5)

        try:
            rsvp_result = gql_request(session, RSVP_MUTATION % (event_id, guest_count))
        except requests.RequestException as exc:
            print(f"RSVP failed for '{title}': {exc}")
            continue

        if "errors" in rsvp_result:
            for error in rsvp_result["errors"]:
                print(f"RSVP failed for '{title}': {error.get('message', error)}")
            continue

        rsvp = rsvp_result.get("data", {}).get("rsvp", {}).get("rsvp", {})
        status = rsvp.get("status", "unknown")
        actual_guest_count = rsvp.get("guestCount", guest_count)

        print(f"RSVP'd to '{title}' — status: {status}, guest count: {actual_guest_count}")

        seen_events.append(event_id)
        save_seen_events(seen_events)
        seen_set.add(event_id)


if __name__ == "__main__":
    main()
