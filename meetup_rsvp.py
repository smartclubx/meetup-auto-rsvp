#!/usr/bin/env python3
"""Auto-RSVP members to new Meetup events using organiser editRsvp mutation."""

import json
import os
import random
import sys
from datetime import datetime, timezone, timedelta

import requests
from supabase import create_client

GRAPHQL_URL = "https://www.meetup.com/gql2"

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
        print("Error: MEETUP_COOKIES required.", file=sys.stderr)
        sys.exit(1)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"Error: MEETUP_COOKIES invalid JSON: {exc}", file=sys.stderr)
        sys.exit(1)


def load_member_ids(organiser_id: str) -> list:
    raw = os.environ.get("MEETUP_MEMBER_IDS", "")
    ids = [m.strip() for m in raw.split(",") if m.strip() and m.strip() != organiser_id]
    if not ids:
        print("Error: MEETUP_MEMBER_IDS required.", file=sys.stderr)
        sys.exit(1)
    return ids


def load_event_tiers(sb) -> dict:
    response = sb.table("meetup_seen_events").select("event_id, tier_applied").execute()
    return {row["event_id"]: row["tier_applied"] for row in response.data}


def save_event_tier(sb, event_id: str, tier: str) -> None:
    sb.table("meetup_seen_events").upsert({
        "event_id": event_id,
        "tier_applied": tier,
        "last_updated": datetime.now(timezone.utc).isoformat()
    }).execute()


def get_target_range(dt_str: str) -> tuple:
    """Returns (low, high, tier_label)"""
    if not dt_str:
        return (10, 15, "14+")
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        days = (dt - datetime.now(timezone.utc)).days
        if days >= 14:
            return (10, 15, "14+")
        elif days >= 7:
            return (15, 25, "7-13")
        elif days >= 1:
            return (25, 35, "1-6")
        else:
            return (10, 15, "14+")
    except ValueError:
        return (10, 15, "14+")


def assign_guest_counts(num_members: int, target_range: tuple) -> list:
    low, high = target_range[:2]
    organiser_count = 8

    # Pick a random target total within the range
    target_total = random.randint(low, high)
    member_total_needed = target_total - organiser_count

    # Clamp to achievable range
    min_possible = num_members * 1
    max_possible = num_members * 5
    member_total_needed = max(min_possible, min(max_possible, member_total_needed))

    # Start everyone at 1 then distribute remainder randomly
    counts = [1] * num_members
    remaining = member_total_needed - num_members

    attempts = 0
    while remaining > 0 and attempts < 100:
        i = random.randint(0, num_members - 1)
        if counts[i] < 5:
            counts[i] += 1
            remaining -= 1
        attempts += 1

    random.shuffle(counts)
    return counts


def gql_request(session: requests.Session, payload: dict) -> dict:
    response = session.post(GRAPHQL_URL, json=payload, timeout=30)
    response.raise_for_status()
    return response.json()


def main() -> None:
    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_KEY")
    if not supabase_url or not supabase_key:
        print("Error: SUPABASE_URL and SUPABASE_KEY required.", file=sys.stderr)
        sys.exit(1)
    sb = create_client(supabase_url, supabase_key)

    cookies = load_cookies()
    group_urlname = os.environ.get("MEETUP_GROUP_URLNAME", "opencircleclub")
    organiser_id = os.environ.get("MEETUP_ORGANISER_ID", "469236254")
    member_ids = load_member_ids(organiser_id)
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

    event_tiers = load_event_tiers(sb)

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
        dt_str = e.get("dateTime")
        low, high, tier = get_target_range(dt_str)

        # Skip if beyond 4-week window
        if dt_str:
            try:
                dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                if dt > cutoff:
                    continue
            except ValueError:
                pass

        # Process if new OR tier has changed
        current_tier = event_tiers.get(e["id"])
        if current_tier != tier:
            e["_tier"] = tier
            e["_target_range"] = (low, high)
            new_events.append(e)

    print(f"Total: {len(events)}, tracked: {len(event_tiers)}, to process: {len(new_events)}")
    print(f"Server time: {datetime.now(timezone.utc).isoformat()}")

    if not new_events:
        print("No new or updated events.")
        sys.exit(0)

    for event in new_events:
        event_id = event["id"]
        title = event.get("title", "Unknown")
        tier = event["_tier"]
        target_range = event["_target_range"]
        guest_counts = assign_guest_counts(len(member_ids), target_range)
        total = 8 + sum(guest_counts)

        print(f"Processing '{title}' — tier: {tier}, target range: {target_range}, total guests: {total}")

        all_ok = True

        # RSVP organiser with 8 guests
        try:
            r = gql_request(session, {
                "query": RSVP_MUTATION,
                "variables": {"input": {"eventId": event_id, "memberId": organiser_id, "guestsCount": 8, "response": "YES"}}
            })
            if "errors" in r:
                for err in r["errors"]:
                    print(f"Organiser RSVP failed for '{title}': {err.get('message', err)}")
                all_ok = False
            else:
                print(f"✓ RSVPd organiser to '{title}' with 8 guests")
        except requests.RequestException as exc:
            print(f"Organiser RSVP failed for '{title}': {exc}")
            all_ok = False

        # RSVP each member
        for member_id, guest_count in zip(member_ids, guest_counts):
            try:
                r = gql_request(session, {
                    "query": RSVP_MUTATION,
                    "variables": {"input": {"eventId": event_id, "memberId": member_id, "guestsCount": guest_count, "response": "YES"}}
                })
                if "errors" in r:
                    for err in r["errors"]:
                        print(f"RSVP failed for member {member_id} on '{title}': {err.get('message', err)}")
                    all_ok = False
                else:
                    print(f"✓ RSVPd member {member_id} to '{title}' with {guest_count} guests")
            except requests.RequestException as exc:
                print(f"RSVP failed for member {member_id} on '{title}': {exc}")
                all_ok = False

        if all_ok:
            save_event_tier(sb, event_id, tier)
            print(f"✓ Saved '{title}' — tier: {tier}, total: {total}")


if __name__ == "__main__":
    main()
    sys.exit(0)
