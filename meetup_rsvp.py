#!/usr/bin/env python3
"""Auto-RSVP members to new Meetup events using organiser editRsvp mutation."""

import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

GRAPHQL_URL = "https://www.meetup.com/gql2"
SEEN_EVENTS_FILE = Path("seen_events.json")

RSVP_MUTATION = """
mutation editRsvp($input: EditRsvpInput!) {
  editRsvp(input: $input) {
    __typename
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


def load_member_ids() -> list:
    raw = os.environ.get("MEETUP_MEMBER_IDS", "")
    ids = [m.strip() for m in raw.split(",") if m.strip()]
    if not ids:
        print("Error: MEETUP_MEMBER_IDS environment variable is required.", file=sys.stderr)
        sys.exit(1)
    return ids


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


def get_target_range(event_date_str: str | None) -> tuple[int, int]:
    if not event_date_str:
        return (10, 15)
    try:
        event_dt = datetime.fromisoformat(event_date_str.replace("Z", "+00:00"))
        days_until = (event_dt - datetime.now(timezone.utc)).days
    except ValueError:
        return (10, 15)

    if days_until >= 14:
        return (10, 15)
    if days_until >= 7:
        return (15, 25)
    if days_until >= 1:
        return (25, 35)
    return (10, 15)


def assign_guest_counts(num_members: int, target_range: tuple[int, int]) -> list[int]:
    pool = [1, 2, 3, 4, 5]
    counts = []
    for _ in range(num_members):
        if not pool:
            pool = [1, 2, 3, 4, 5]
        counts.append(pool.pop(0))

    min_total, max_total = target_range
    organiser_guests = 8

    def total() -> int:
        return organiser_guests + sum(counts)

    while total() < min_total:
        min_val = min(counts)
        idx = counts.index(min_val)
        if counts[idx] >= 5:
            break
        counts[idx] += 1

    while total() > max_total:
        max_val = max(counts)
        idx = counts.index(max_val)
        if counts[idx] <= 1:
            break
        counts[idx] -= 1

    return counts


def main() -> None:
    cookies = load_cookies()
    group_urlname = os.environ.get("MEETUP_GROUP_URLNAME", "opencircleclub")
    member_ids = load_member_ids()
    organiser_id = os.environ.get("MEETUP_ORGANISER_ID", "469236254")
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

    group = result.get("data", {}).get("groupByUrlname") or {}
    edges = []
    for key in ["events", "unifiedEvents", "upcomingEvents"]:
        candidate = group.get(key, {})
        if candidate and candidate.get("edges"):
            edges = candidate["edges"]
            break

    events = [e["node"] for e in edges if e.get("node")]

    cutoff = datetime.now(timezone.utc) + timedelta(weeks=4)
    new_events = []
    for e in events:
        if e["id"] in seen_set:
            continue
        dt_str = e.get("dateTime")
        if dt_str:
            try:
                dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                if dt > cutoff:
                    continue
            except ValueError:
                pass
        new_events.append(e)

    print(f"Total events fetched: {len(events)}, already seen: {len(seen_set)}, new to RSVP: {len(new_events)}")

    if not new_events:
        print("No new events to RSVP.")
        sys.exit(0)

    for event in new_events:
        event_id = event["id"]
        title = event.get("title", "Unknown")
        all_ok = True

        target_range = get_target_range(event.get("dateTime"))
        member_guest_counts = assign_guest_counts(len(member_ids), target_range)
        rsvp_plan = [(organiser_id, 8)] + list(zip(member_ids, member_guest_counts))

        for member_id, guest_count in rsvp_plan:
            rsvp_payload = {
                "query": RSVP_MUTATION,
                "variables": {
                    "input": {
                        "eventId": event_id,
                        "memberId": member_id,
                        "guestsCount": guest_count,
                        "response": "YES"
                    }
                }
            }

            try:
                rsvp_result = gql_request(session, rsvp_payload)
            except requests.RequestException as exc:
                print(f"RSVP failed for member {member_id} on '{title}': {exc}")
                all_ok = False
                continue

            if "errors" in rsvp_result:
                for error in rsvp_result["errors"]:
                    print(f"RSVP failed for member {member_id} on '{title}': {error.get('message', error)}")
                all_ok = False
                continue

            print(f"✓ RSVPd member {member_id} to '{title}' with {guest_count} guests")

        if all_ok:
            seen_events.append(event_id)
            save_seen_events(seen_events)
            seen_set.add(event_id)


if __name__ == "__main__":
    main()
    sys.exit(0)
