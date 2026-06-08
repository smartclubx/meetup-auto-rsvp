#!/usr/bin/env python3
"""Automatically RSVP to new Meetup events for a configured group."""

import json
import os
import random
import sys
from pathlib import Path

import requests

GRAPHQL_URL = "https://www.meetup.com/gql2"
SEEN_EVENTS_FILE = Path("seen_events.json")

FETCH_EVENTS_QUERY = """
query($urlname: String!) {
  groupByUrlname(urlname: $urlname) {
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


def gql_request(session: requests.Session, query: str, variables: dict) -> dict:
    response = session.post(
        GRAPHQL_URL,
        json={"query": query, "variables": variables},
        timeout=30
    )
    response.raise_for_status()
    return response.json()


def main() -> None:
    cookies = load_cookies()
    group_urlname = os.environ.get("MEETUP_GROUP_URLNAME", "opencircleclub")

    # Extract CSRF tokens from cookies
    csrf = cookies.get("MEETUP_CSRF", "")
    next_csrf = cookies.get("__Host-NEXT_MEETUP_CSRF", "")

    session = requests.Session()

    # Set all cookies
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

    try:
        result = gql_request(session, FETCH_EVENTS_QUERY, {"urlname": group_urlname})
    except requests.RequestException as exc:
        print(f"Error fetching events: {exc}", file=sys.stderr)
        sys.exit(1)

    if "errors" in result:
        for error in result["errors"]:
            print(f"GraphQL error: {error.get('message', error)}", file=sys.stderr)
        sys.exit(1)

    group = result.get("data", {}).get("groupByUrlname")
    if not group:
        print(f"No group found for: {group_urlname}", file=sys.stderr)
        sys.exit(1)

    edges = group.get("unifiedEvents", {}).get("edges", [])
    events = [e["node"] for e in edges if e.get("node")]
    new_events = [e for e in events if e["id"] not in seen_set]

    if not new_events:
        print("No new events to RSVP.")
        return

    for event in new_events:
        event_id = event["id"]
        title = event.get("title", "Unknown")
        guest_count = random.randint(1, 5)

        try:
            rsvp_result = gql_request(session, RSVP_MUTATION, {
                "eventId": event_id,
                "guestCount": guest_count
            })
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
