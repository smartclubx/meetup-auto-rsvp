#!/usr/bin/env python3
"""Automatically RSVP to new Meetup events for a configured group."""

import json
import os
import random
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

GRAPHQL_URL = "https://www.meetup.com/gql2"
SEEN_EVENTS_FILE = Path("seen_events.json")

RSVP_MUTATION = """
mutation($eventId: ID!, $guestCount: Int!) {
  rsvp(input: { eventId: $eventId, rsvp: YES, guestCount: $guestCount }) {
    rsvp {
      id
      status
      guestCount
    }
  }
}
"""


def load_cookies() -> dict:
    raw = os.environ.get("MEETUP_COOKIES")
    if not raw:
        print("Error: MEETUP_COOKIES environment variable is required.", file=sys.stderr)
        sys.exit(1)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"Error: MEETUP_COOKIES is not valid JSON: {exc}", file=sys.stderr)
        sys.exit(1)


def load_seen_events() -> list:
    if not SEEN_EVENTS_FILE.exists():
        SEEN_EVENTS_FILE.write_text("[]", encoding="utf-8")
        return []
    with SEEN_EVENTS_FILE.open(encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else []


def save_seen_events(seen_events: list) -> None:
    with SEEN_EVENTS_FILE.open("w", encoding="utf-8") as f:
        json.dump(seen_events, f, indent=2)


def gql_request(session: requests.Session, payload: dict) -> dict:
    response = session.post(GRAPHQL_URL, json=payload, timeout=30)
    response.raise_for_status()
    return response.json()


def main() -> None:
    cookies = load_cookies()
    group_urlname = os.environ.get("MEETUP_GROUP_URLNAME", "opencircleclub")
    next_csrf = cookies.get("__Host-NEXT_MEETUP_CSRF", "")

    session = requests.Session()
    for name, value in cookies.items():
        session.cookies.set(name, value, domain="www.meetup.com")

    session.headers.update({
        "Content-Type": "application/json",
        "Accept": "*/*",
        "apollographql-client-name": "nextjs-web",
        "x-meetup-csrf": next_csrf,
        "Origin": "https://www.meetup.com",
        "Referer": f"https://www.meetup.com/{group_urlname}/events/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:151.0) Gecko/20100101 Firefox/151.0",
    })

    seen_events = load_seen_events()
    seen_set = set(seen_events)

    # Fetch upcoming events using persisted query
    after_datetime = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    fetch_payload = {
        "operationName": "getUpcomingGroupEvents",
        "variables": {
            "urlname": group_urlname,
            "afterDateTime": after_datetime
        },
        "extensions": {
            "persistedQuery": {
                "sha256Hash": "066e3709c68718d5ce9dd909e979ac70f99835fb3722cef77756ded808d5ca08",
                "version": 1
            }
        }
    }

    try:
        result = gql_request(session, fetch_payload)
    except requests.RequestException as exc:
        print(f"Error fetching events: {exc}", file=sys.stderr)
        sys.exit(1)

    if "errors" in result:
        for error in result["errors"]:
            print(f"GraphQL error: {error.get('message', error)}", file=sys.stderr)
        sys.exit(1)

    # Log full response for debugging on first run
    print("Fetch response:", json.dumps(result, indent=2)[:2000])

    # Parse events — adjust path once we see actual response shape
    data = result.get("data", {})
    group = data.get("groupByUrlname") or data.get("group") or {}
    edges = (
        group.get("unifiedEvents", {}).get("edges")
        or group.get("upcomingEvents", {}).get("edges")
        or []
    )
    events = [e["node"] for e in edges if e.get("node")]
    cutoff = datetime.now(timezone.utc) + timedelta(weeks=4)
    new_events = [
        e for e in events
        if e["id"] not in seen_set
        and datetime.fromisoformat(e["dateTime"].replace("Z", "+00:00")) <= cutoff
    ]

    if not new_events:
        print("No new events to RSVP.")
        return

    for event in new_events:
        event_id = event["id"]
        title = event.get("title", "Unknown")
        guest_count = random.randint(1, 5)

        rsvp_payload = {
            "query": RSVP_MUTATION,
            "variables": {
                "eventId": event_id,
                "guestCount": guest_count
            }
        }

        try:
            rsvp_result = gql_request(session, rsvp_payload)
        except requests.RequestException as exc:
            print(f"RSVP request failed for '{title}': {exc}")
            continue

        if "errors" in rsvp_result:
            for error in rsvp_result["errors"]:
                print(f"RSVP failed for '{title}': {error.get('message', error)}")
            continue

        rsvp = rsvp_result.get("data", {}).get("rsvp", {}).get("rsvp", {})
        status = rsvp.get("status", "unknown")
        actual_guests = rsvp.get("guestCount", guest_count)
        print(f"✓ RSVP'd to '{title}' — status: {status}, guests: {actual_guests}")

        seen_events.append(event_id)
        save_seen_events(seen_events)
        seen_set.add(event_id)


if __name__ == "__main__":
    main()
