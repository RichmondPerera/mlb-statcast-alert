#!/usr/bin/env python3

"""
MLB Statcast Probable Starter Discord Alert

DATA SOURCES
------------
MLB Stats API:
    - Today's schedule
    - Probable pitcher identification
    - Team / opponent / venue

Baseball Savant via pybaseball:
    - All pitching performance statistics
    - Last 3 starts
    - HR allowed
    - K / BB / H
    - AVG / SLG / wOBA / xwOBA
    - LHB / RHB splits

NOT USED
--------
- MLB pitching-stat endpoints
- FanGraphs pitching statistics
- Baseball Reference pitching statistics

FEATURES
--------
- Today's probable starters
- Last 3 starting appearances
- Combined last 3 starts
- LHB / RHB splits
- K / BB / H / HR
- AVG / SLG / wOBA / xwOBA
- K%
- BB%
- HR/9
- MLB headshot
- Discord webhook
- Persistent duplicate protection
- pybaseball Statcast caching
- --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from pybaseball import cache, statcast_pitcher


# ============================================================
# CONFIGURATION
# ============================================================

ROOT = Path(__file__).resolve().parent

DATA_DIR = ROOT / "data"

STATE_FILE = DATA_DIR / "discord_state.json"

MLB_API = "https://statsapi.mlb.com/api/v1"

USER_AGENT = "MLB-Statcast-Alert/2.0"

DEFAULT_LOOKBACK_DAYS = 45

DEFAULT_STARTS = 3

DISCORD_WEBHOOK_URL = os.getenv(
    "DISCORD_WEBHOOK_URL",
    "",
).strip()

HEADSHOT_URL = (
    "https://img.mlbstatic.com/mlb-photos/image/upload/"
    "w_213,d_people:generic:headshots:120:current.png/"
    "q_auto:good/"
    "v1/people/{player_id}/headshot/67/current"
)

# Enable pybaseball local caching.
#
# This is particularly useful because Statcast downloads can
# be large and pybaseball itself recommends caching for large
# Statcast requests.
cache.enable()


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

LOG = logging.getLogger("mlb-statcast-alert")


# ============================================================
# BASIC HELPERS
# ============================================================

def safe_float(
    value: Any,
    default: float = 0.0,
) -> float:

    try:

        if value is None:
            return default

        if pd.isna(value):
            return default

        return float(value)

    except (TypeError, ValueError):

        return default


def safe_int(
    value: Any,
    default: int = 0,
) -> int:

    try:

        if value is None:
            return default

        if pd.isna(value):
            return default

        return int(value)

    except (TypeError, ValueError):

        return default


def fmt_avg(
    value: float | None,
) -> str:

    if value is None:
        return "—"

    return f"{value:.3f}"


def fmt_pct(
    value: float | None,
) -> str:

    if value is None:
        return "—"

    return f"{value * 100:.1f}%"


def fmt_one(
    value: float | None,
) -> str:

    if value is None:
        return "—"

    return f"{value:.1f}"


def format_ip(
    outs: int,
) -> str:

    innings = outs // 3

    remainder = outs % 3

    return f"{innings}.{remainder}"


# ============================================================
# STATE
# ============================================================

def load_state() -> dict:

    DATA_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    if not STATE_FILE.exists():

        return {
            "version": 2,
            "posted": {},
        }

    try:

        with STATE_FILE.open(
            "r",
            encoding="utf-8",
        ) as f:

            state = json.load(f)

        if not isinstance(
            state,
            dict,
        ):

            raise ValueError(
                "State file is not an object"
            )

        state.setdefault(
            "version",
            2,
        )

        state.setdefault(
            "posted",
            {},
        )

        return state

    except Exception as exc:

        LOG.warning(
            "Could not load state: %s",
            exc,
        )

        return {
            "version": 2,
            "posted": {},
        }


def save_state(
    state: dict,
) -> None:

    DATA_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary = STATE_FILE.with_suffix(
        ".tmp"
    )

    with temporary.open(
        "w",
        encoding="utf-8",
    ) as f:

        json.dump(
            state,
            f,
            indent=2,
            sort_keys=True,
        )

    temporary.replace(
        STATE_FILE
    )


def prune_state(
    state: dict,
    keep_days: int = 14,
) -> None:

    posted = state.setdefault(
        "posted",
        {},
    )

    cutoff = (
        date.today()
        - timedelta(days=keep_days)
    )

    remove = []

    for key, record in posted.items():

        try:

            posted_date = (
                datetime.fromisoformat(
                    record["posted_at"]
                ).date()
            )

        except Exception:

            continue

        if posted_date < cutoff:

            remove.append(key)

    for key in remove:

        posted.pop(
            key,
            None,
        )


# ============================================================
# MLB API
# ============================================================

def get_json(
    url: str,
    params: dict | None = None,
) -> dict:

    response = requests.get(
        url,
        params=params,
        timeout=30,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )

    response.raise_for_status()

    return response.json()


def get_today_schedule() -> list[dict]:

    today = date.today().isoformat()

    payload = get_json(
        f"{MLB_API}/schedule",
        {
            "sportId": 1,
            "date": today,
            "hydrate": (
                "team,"
                "probablePitcher,"
                "venue"
            ),
        },
    )

    games = []

    for schedule_date in payload.get(
        "dates",
        [],
    ):

        games.extend(
            schedule_date.get(
                "games",
                [],
            )
        )

    return games


# ============================================================
# PROBABLE STARTERS
# ============================================================

def get_probable_starters(
    games: list[dict],
) -> list[dict]:

    starters = []

    for game in games:

        game_pk = game.get(
            "gamePk"
        )

        game_date = game.get(
            "gameDate"
        )

        venue = (
            game
            .get("venue", {})
            .get(
                "name",
                "Unknown Venue",
            )
        )

        teams = game.get(
            "teams",
            {},
        )

        away = teams.get(
            "away",
            {},
        )

        home = teams.get(
            "home",
            {},
        )

        away_team = (
            away
            .get("team", {})
            .get(
                "name",
                "Away",
            )
        )

        home_team = (
            home
            .get("team", {})
            .get(
                "name",
                "Home",
            )
        )

        away_pitcher = away.get(
            "probablePitcher"
        )

        if (
            away_pitcher
            and away_pitcher.get("id")
        ):

            starters.append(
                {
                    "game_pk": game_pk,
                    "game_date": game_date,
                    "venue": venue,
                    "team": away_team,
                    "opponent": home_team,
                    "home": False,
                    "pitcher_id": int(
                        away_pitcher["id"]
                    ),
                    "pitcher_name": away_pitcher.get(
                        "fullName",
                        "Unknown",
                    ),
                }
            )

        home_pitcher = home.get(
            "probablePitcher"
        )

        if (
            home_pitcher
            and home_pitcher.get("id")
        ):

            starters.append(
                {
                    "game_pk": game_pk,
                    "game_date": game_date,
                    "venue": venue,
                    "team": home_team,
                    "opponent": away_team,
                    "home": True,
                    "pitcher_id": int(
                        home_pitcher["id"]
                    ),
                    "pitcher_name": home_pitcher.get(
                        "fullName",
                        "Unknown",
                    ),
                }
            )

    return starters


# ============================================================
# STATCAST
# ============================================================

def download_statcast(
    pitcher_id: int,
    lookback_days: int,
) -> pd.DataFrame:

    end_date = date.today()

    start_date = (
        end_date
        - timedelta(
            days=lookback_days
        )
    )

    LOG.info(
        "Statcast pitcher %s: %s -> %s",
        pitcher_id,
        start_date,
        end_date,
    )

    try:

        df = statcast_pitcher(
            start_dt=start_date.isoformat(),
            end_dt=end_date.isoformat(),
            player_id=pitcher_id,
        )

    except Exception:

        LOG.exception(
            "Statcast download failed for pitcher %s",
            pitcher_id,
        )

        raise

    if df is None or df.empty:

        return pd.DataFrame()

    df = df.copy()

    # --------------------------------------------------------
    # Normalize numeric columns
    # --------------------------------------------------------

    numeric_columns = [
        "pitcher",
        "game_pk",
        "at_bat_number",
        "pitch_number",
        "inning",
    ]

    for column in numeric_columns:

        if column in df.columns:

            df[column] = pd.to_numeric(
                df[column],
                errors="coerce",
            )

    # --------------------------------------------------------
    # Dates
    # --------------------------------------------------------

    if "game_date" in df.columns:

        df["game_date"] = pd.to_datetime(
            df["game_date"],
            errors="coerce",
        )

    # --------------------------------------------------------
    # Final pitcher filter.
    #
    # This is mostly defensive because statcast_pitcher()
    # already queries by pitcher.
    # --------------------------------------------------------

    if "pitcher" in df.columns:

        df = df[
            df["pitcher"] == pitcher_id
        ].copy()

    return df


# ============================================================
# EVENT HELPERS
# ============================================================

def valid_event(
    event: Any,
) -> bool:

    if event is None:
        return False

    try:

        if pd.isna(event):
            return False

    except Exception:

        pass

    value = str(event).strip().lower()

    return value not in {
        "",
        "nan",
        "none",
        "null",
    }


def event_is(
    event: Any,
    *names: str,
) -> bool:

    if not valid_event(event):
        return False

    value = str(
        event
    ).strip().lower()

    return value in {
        name.lower()
        for name in names
    }


# ============================================================
# PLATE APPEARANCES
# ============================================================

def plate_appearance_rows(
    df: pd.DataFrame,
) -> pd.DataFrame:

    if df.empty:
        return pd.DataFrame()

    if "events" not in df.columns:
        return pd.DataFrame()

    pa = df[
        df["events"].apply(
            valid_event
        )
    ].copy()

    if pa.empty:
        return pa

    required = {
        "game_pk",
        "at_bat_number",
        "pitch_number",
    }

    if required.issubset(
        pa.columns
    ):

        pa = (
            pa.sort_values(
                [
                    "game_pk",
                    "at_bat_number",
                    "pitch_number",
                ],
                na_position="last",
            )
            .drop_duplicates(
                [
                    "game_pk",
                    "at_bat_number",
                ],
                keep="last",
            )
        )

    return pa


# ============================================================
# BATTING EVENT CLASSIFICATION
# ============================================================

SINGLE_EVENTS = {
    "single",
}

DOUBLE_EVENTS = {
    "double",
}

TRIPLE_EVENTS = {
    "triple",
}

HOME_RUN_EVENTS = {
    "home_run",
}

WALK_EVENTS = {
    "walk",
    "intent_walk",
}

HBP_EVENTS = {
    "hit_by_pitch",
}

STRIKEOUT_EVENTS = {
    "strikeout",
    "strikeout_double_play",
}

SAC_FLY_EVENTS = {
    "sac_fly",
    "sac_fly_double_play",
}

SAC_BUNT_EVENTS = {
    "sac_bunt",
}

AB_EXCLUDED_EVENTS = (
    WALK_EVENTS
    | HBP_EVENTS
    | SAC_FLY_EVENTS
)


# ============================================================
# wOBA
# ============================================================

def calculate_woba(
    pa: pd.DataFrame,
) -> float | None:

    if pa.empty:
        return None

    # Baseball Savant provides woba_value at the PA level.
    #
    # We DO NOT simply average every non-null row.
    # Instead we calculate the weighted numerator and
    # denominator from the event values.
    #
    # woba_value already contains the event's wOBA weight.
    # woba_denom contains the appropriate denominator flag.

    if (
        "woba_value" not in pa.columns
    ):

        return None

    values = pd.to_numeric(
        pa["woba_value"],
        errors="coerce",
    )

    if "woba_denom" in pa.columns:

        denom = pd.to_numeric(
            pa["woba_denom"],
            errors="coerce",
        )

        valid = (
            values.notna()
            & denom.notna()
            & (denom > 0)
        )

        if not valid.any():

            return None

        numerator = values[
            valid
        ].sum()

        denominator = denom[
            valid
        ].sum()

        if denominator <= 0:

            return None

        return float(
            numerator
            / denominator
        )

    # Fallback for older Statcast data
    # where woba_denom may not be present.
    #
    # Use the mean only when every qualifying PA
    # has a valid woba_value.
    valid = values.dropna()

    if valid.empty:
        return None

    return float(
        valid.mean()
    )


# ============================================================
# xwOBA
# ============================================================

def calculate_xwoba(
    pa: pd.DataFrame,
) -> float | None:

    if pa.empty:
        return None

    column = (
        "estimated_woba_using_speedangle"
    )

    if column not in pa.columns:

        return None

    values = pd.to_numeric(
        pa[column],
        errors="coerce",
    )

    values = values.dropna()

    if values.empty:

        return None

    return float(
        values.mean()
    )


# ============================================================
# BATTING STATS
# ============================================================

def calculate_batting_stats(
    df: pd.DataFrame,
) -> dict:

    pa = plate_appearance_rows(
        df
    )

    if pa.empty:

        return {
            "pa": 0,
            "ab": 0,
            "h": 0,
            "hr": 0,
            "bb": 0,
            "k": 0,
            "hbp": 0,
            "tb": 0,
            "avg": None,
            "slg": None,
            "woba": None,
            "xwoba": None,
        }

    events = (
        pa["events"]
        .astype(str)
        .str.lower()
    )

    singles = int(
        events.isin(
            SINGLE_EVENTS
        ).sum()
    )

    doubles = int(
        events.isin(
            DOUBLE_EVENTS
        ).sum()
    )

    triples = int(
        events.isin(
            TRIPLE_EVENTS
        ).sum()
    )

    home_runs = int(
        events.isin(
            HOME_RUN_EVENTS
        ).sum()
    )

    hits = (
        singles
        + doubles
        + triples
        + home_runs
    )

    walks = int(
        events.isin(
            WALK_EVENTS
        ).sum()
    )

    hbp = int(
        events.isin(
            HBP_EVENTS
        ).sum()
    )

    strikeouts = int(
        events.isin(
            STRIKEOUT_EVENTS
        ).sum()
    )

    plate_appearances = len(
        pa
    )

    # --------------------------------------------------------
    # Correct AB calculation.
    #
    # Sac flies and sac bunts are not AB.
    # Walks and HBP are not AB.
    # --------------------------------------------------------

    excluded_from_ab = (
        WALK_EVENTS
        | HBP_EVENTS
        | SAC_FLY_EVENTS
        | SAC_BUNT_EVENTS
    )

    at_bats = int(
        (~events.isin(
            excluded_from_ab
        )).sum()
    )

    total_bases = (
        singles
        + (2 * doubles)
        + (3 * triples)
        + (4 * home_runs)
    )

    avg = None

    if at_bats > 0:

        avg = (
            hits
            / at_bats
        )

    slg = None

    if at_bats > 0:

        slg = (
            total_bases
            / at_bats
        )

    return {
        "pa": plate_appearances,
        "ab": at_bats,
        "h": hits,
        "hr": home_runs,
        "bb": walks,
        "k": strikeouts,
        "hbp": hbp,
        "tb": total_bases,
        "avg": avg,
        "slg": slg,
        "woba": calculate_woba(pa),
        "xwoba": calculate_xwoba(pa),
    }


# ============================================================
# OUTS
# ============================================================

OUT_EVENTS = {
    "field_out": 1,
    "force_out": 1,
    "strikeout": 1,
    "strikeout_double_play": 2,
    "grounded_into_double_play": 2,
    "double_play": 2,
    "triple_play": 3,
    "fielders_choice_out": 1,
    "fielders_choice": 0,
    "sac_fly": 1,
    "sac_bunt": 1,
    "sac_fly_double_play": 2,
}


def calculate_outs(
    df: pd.DataFrame,
) -> int:

    pa = plate_appearance_rows(
        df
    )

    if pa.empty:
        return 0

    events = (
        pa["events"]
        .astype(str)
        .str.lower()
    )

    return int(
        sum(
            OUT_EVENTS.get(
                event,
                0,
            )
            for event in events
        )
    )


# ============================================================
# PITCHING REPORT
# ============================================================

def calculate_pitching_report(
    df: pd.DataFrame,
) -> dict:

    stats = calculate_batting_stats(
        df
    )

    outs = calculate_outs(
        df
    )

    ip = outs / 3

    k_pct = None

    if stats["pa"] > 0:

        k_pct = (
            stats["k"]
            / stats["pa"]
        )

    bb_pct = None

    if stats["pa"] > 0:

        bb_pct = (
            stats["bb"]
            / stats["pa"]
        )

    hr9 = None

    if ip > 0:

        hr9 = (
            stats["hr"]
            * 9
            / ip
        )

    return {
        **stats,
        "outs": outs,
        "ip": ip,
        "ip_display": format_ip(
            outs
        ),
        "k_pct": k_pct,
        "bb_pct": bb_pct,
        "hr9": hr9,
    }


# ============================================================
# START DETECTION
# ============================================================

def find_starting_appearances(
    df: pd.DataFrame,
    pitcher_id: int,
) -> list[dict]:

    """
    Identify starting appearances from Statcast.

    Primary rule:
        Pitcher's first pitch of the game occurred
        during the first inning.

    Additional safeguard:
        Require the first pitch of the pitcher's
        appearance to have inning == 1.

    This intentionally uses Statcast only.
    """

    if df.empty:
        return []

    if "pitcher" not in df.columns:
        return []

    pitcher_df = df[
        df["pitcher"] == pitcher_id
    ].copy()

    if pitcher_df.empty:
        return []

    appearances = []

    for game_pk, game_df in pitcher_df.groupby(
        "game_pk"
    ):

        if pd.isna(game_pk):
            continue

        game_df = game_df.sort_values(
            [
                "inning",
                "at_bat_number",
                "pitch_number",
            ],
            na_position="last",
        )

        if game_df.empty:
            continue

        first_pitch = game_df.iloc[0]

        first_inning = safe_int(
            first_pitch.get(
                "inning"
            ),
            99,
        )

        if first_inning != 1:
            continue

        game_date = first_pitch.get(
            "game_date"
        )

        if pd.isna(game_date):
            continue

        appearances.append(
            {
                "game_pk": int(
                    game_pk
                ),
                "game_date": game_date,
                "data": game_df,
            }
        )

    appearances.sort(
        key=lambda item: item[
            "game_date"
        ],
        reverse=True,
    )

    return appearances


# ============================================================
# PITCHER HANDEDNESS
# ============================================================

def get_pitcher_hand(
    df: pd.DataFrame,
) -> str:

    if df.empty:
        return "?"

    for column in (
        "p_throws",
    ):

        if column not in df.columns:
            continue

        values = (
            df[column]
            .dropna()
            .astype(str)
            .str.upper()
        )

        if not values.empty:

            value = values.iloc[0]

            if value == "L":
                return "L"

            if value == "R":
                return "R"

    return "?"


# ============================================================
# DISCORD
# ============================================================

def make_discord_embed(
    starter: dict,
    starts: list[dict],
) -> dict:

    if not starts:

        raise ValueError(
            "No starting appearances found"
        )

    recent_df = pd.concat(
        [
            start["data"]
            for start in starts
        ],
        ignore_index=True,
    )

    overall = calculate_pitching_report(
        recent_df
    )

    pitcher_hand = get_pitcher_hand(
        recent_df
    )

    # --------------------------------------------------------
    # LHB / RHB
    # --------------------------------------------------------

    if "batter_stands" in recent_df.columns:

        batter_stands = (
            recent_df[
                "batter_stands"
            ]
            .astype("string")
            .str.upper()
        )

        lhb_df = recent_df[
            batter_stands == "L"
        ].copy()

        rhb_df = recent_df[
            batter_stands == "R"
        ].copy()

    else:

        lhb_df = pd.DataFrame()

        rhb_df = pd.DataFrame()

    lhb = calculate_pitching_report(
        lhb_df
    )

    rhb = calculate_pitching_report(
        rhb_df
    )

    # --------------------------------------------------------
    # Last 3 starts
    # --------------------------------------------------------

    start_lines = []

    for start in starts:

        report = calculate_pitching_report(
            start["data"]
        )

        start_date = pd.Timestamp(
            start["game_date"]
        )

        start_lines.append(
            f"**{start_date.strftime('%b %d')}** — "
            f"IP **{report['ip_display']}** | "
            f"H **{report['h']}** | "
            f"HR **{report['hr']}** | "
            f"BB **{report['bb']}** | "
            f"K **{report['k']}**"
        )

    starts_text = "\n".join(
        start_lines
    )

    # --------------------------------------------------------
    # Split formatter
    # --------------------------------------------------------

    def split_text(
        report: dict,
    ) -> str:

        return (
            f"PA **{report['pa']}** | "
            f"H **{report['h']}** | "
            f"HR **{report['hr']}** | "
            f"BB **{report['bb']}** | "
            f"K **{report['k']}**\n"
            f"AVG **{fmt_avg(report['avg'])}** | "
            f"SLG **{fmt_avg(report['slg'])}**\n"
            f"wOBA **{fmt_avg(report['woba'])}** | "
            f"xwOBA **{fmt_avg(report['xwoba'])}**"
        )

    # --------------------------------------------------------
    # Description
    # --------------------------------------------------------

    description = (
        f"**{starter['team']}** "
        f"{'vs' if starter['home'] else '@'} "
        f"**{starter['opponent']}**\n"
        f"{starter['venue']}\n"
        f"Throws **{pitcher_hand}**\n\n"

        f"### Last {len(starts)} Starts\n"
        f"{starts_text}\n\n"

        f"### Last {len(starts)} Starts — Combined\n"
        f"IP **{overall['ip_display']}** | "
        f"H **{overall['h']}** | "
        f"HR **{overall['hr']}** | "
        f"BB **{overall['bb']}** | "
        f"K **{overall['k']}**\n"
        f"K% **{fmt_pct(overall['k_pct'])}** | "
        f"BB% **{fmt_pct(overall['bb_pct'])}** | "
        f"HR/9 **{fmt_one(overall['hr9'])}**\n\n"

        f"### vs LHB\n"
        f"{split_text(lhb)}\n\n"

        f"### vs RHB\n"
        f"{split_text(rhb)}"
    )

    return {
        "title": (
            f"{starter['pitcher_name']} "
            f"— Statcast Pitching Alert"
        ),

        "description": description,

        "url": (
            "https://www.mlb.com/player/"
            f"{starter['pitcher_id']}"
        ),

        "thumbnail": {
            "url": HEADSHOT_URL.format(
                player_id=starter[
                    "pitcher_id"
                ]
            )
        },

        "footer": {
            "text": (
                "Pitching statistics from "
                "Baseball Savant Statcast "
                "via pybaseball"
            )
        },
    }


def make_discord_payload(
    starter: dict,
    starts: list[dict],
) -> dict:

    return {
        "username": "MLB Statcast Alert",
        "embeds": [
            make_discord_embed(
                starter,
                starts,
            )
        ],
    }


# ============================================================
# DISCORD POST
# ============================================================

def send_to_discord(
    webhook_url: str,
    payload: dict,
) -> None:

    response = requests.post(
        webhook_url,
        json=payload,
        timeout=30,
        headers={
            "User-Agent": USER_AGENT,
        },
    )

    response.raise_for_status()


# ============================================================
# DUPLICATE KEY
# ============================================================

def make_alert_key(
    starter: dict,
) -> str:

    game_date = str(
        starter["game_date"]
    )[:10]

    return (
        f"{game_date}:"
        f"{starter['game_pk']}:"
        f"{starter['pitcher_id']}"
    )


# ============================================================
# MAIN
# ============================================================

def run(
    dry_run: bool,
    lookback_days: int,
) -> int:

    webhook_url = os.getenv(
        "DISCORD_WEBHOOK_URL",
        "",
    ).strip()

    if (
        not dry_run
        and not webhook_url
    ):

        LOG.error(
            "DISCORD_WEBHOOK_URL is not set."
        )

        LOG.error(
            "Use --dry-run for testing."
        )

        return 1

    # --------------------------------------------------------
    # State
    # --------------------------------------------------------

    state = load_state()

    prune_state(
        state,
        keep_days=14,
    )

    # --------------------------------------------------------
    # Schedule
    # --------------------------------------------------------

    LOG.info(
        "Getting today's MLB schedule..."
    )

    games = get_today_schedule()

    if not games:

        LOG.info(
            "No MLB games found today."
        )

        return 0

    starters = get_probable_starters(
        games
    )

    if not starters:

        LOG.info(
            "No probable starters found."
        )

        return 0

    LOG.info(
        "Found %d probable starters.",
        len(starters),
    )

    # --------------------------------------------------------
    # Process
    # --------------------------------------------------------

    for starter in starters:

        pitcher_name = starter[
            "pitcher_name"
        ]

        pitcher_id = starter[
            "pitcher_id"
        ]

        alert_key = make_alert_key(
            starter
        )

        if alert_key in state[
            "posted"
        ]:

            LOG.info(
                "SKIP duplicate: %s",
                pitcher_name,
            )

            continue

        try:

            # ------------------------------------------------
            # Download pitcher-specific Statcast
            # ------------------------------------------------

            df = download_statcast(
                pitcher_id,
                lookback_days,
            )

            if df.empty:

                LOG.warning(
                    "No Statcast data: %s",
                    pitcher_name,
                )

                continue

            # ------------------------------------------------
            # Identify starts
            # ------------------------------------------------

            starts = (
                find_starting_appearances(
                    df,
                    pitcher_id,
                )
            )

            if not starts:

                LOG.warning(
                    "No starts identified: %s",
                    pitcher_name,
                )

                continue

            starts = starts[
                :DEFAULT_STARTS
            ]

            LOG.info(
                "%s: found %d recent starts",
                pitcher_name,
                len(starts),
            )

            # ------------------------------------------------
            # Build payload
            # ------------------------------------------------

            payload = make_discord_payload(
                starter,
                starts,
            )

            # ------------------------------------------------
            # Dry run
            # ------------------------------------------------

            if dry_run:

                print()
                print(
                    "=" * 90
                )

                print(
                    pitcher_name
                )

                print(
                    "=" * 90
                )

                print(
                    json.dumps(
                        payload,
                        indent=2,
                    )
                )

                print(
                    "=" * 90
                )

                print()

            else:

                LOG.info(
                    "Posting %s to Discord...",
                    pitcher_name,
                )

                send_to_discord(
                    webhook_url,
                    payload,
                )

                time.sleep(1)

            # ------------------------------------------------
            # Duplicate state
            # ------------------------------------------------

            state[
                "posted"
            ][alert_key] = {

                "posted_at": (
                    datetime.now()
                    .isoformat(
                        timespec="seconds"
                    )
                ),

                "pitcher_id": pitcher_id,

                "pitcher_name": (
                    pitcher_name
                ),

                "game_pk": starter[
                    "game_pk"
                ],
            }

            save_state(
                state
            )

            LOG.info(
                "Completed: %s",
                pitcher_name,
            )

        except Exception as exc:

            LOG.exception(
                "ERROR processing %s: %s",
                pitcher_name,
                exc,
            )

    return 0


# ============================================================
# CLI
# ============================================================

def main() -> int:

    parser = argparse.ArgumentParser(
        description=(
            "MLB Statcast probable starter "
            "Discord alert"
        )
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Generate alerts without "
            "posting to Discord"
        ),
    )

    parser.add_argument(
        "--lookback-days",
        type=int,
        default=int(
            os.getenv(
                "STATCAST_LOOKBACK_DAYS",
                DEFAULT_LOOKBACK_DAYS,
            )
        ),
        help=(
            "Number of days of Statcast "
            "history to retrieve"
        ),
    )

    args = parser.parse_args()

    lookback_days = max(
        10,
        args.lookback_days,
    )

    return run(
        dry_run=args.dry_run,
        lookback_days=lookback_days,
    )


if __name__ == "__main__":

    sys.exit(
        main()
    )
