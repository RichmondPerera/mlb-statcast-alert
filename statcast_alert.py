#!/usr/bin/env python3

"""
MLB Statcast Probable Starter Discord Alert

GOAL
----
Generate a Discord alert for every probable MLB starter with:

    🔥 Pitcher — Statcast Pitching Alert

    Team @ Opponent
    Venue
    Throws: L/R

    LAST 3 STARTS
    Date — IP | H | HR | BB | K
    Date — IP | H | HR | BB | K
    Date — IP | H | HR | BB | K

    COMBINED
    IP | H | HR | BB | K
    K% | BB% | HR/9

    CONTACT QUALITY
    AVG
    SLG
    wOBA
    xwOBA
    xBA
    xSLG
    Hard Hit%
    Barrel%

    VS LHB
    PA | AVG | SLG
    K% | BB%
    wOBA | xwOBA

    VS RHB
    PA | AVG | SLG
    K% | BB%
    wOBA | xwOBA


DATA SOURCES
------------
MLB Stats API
    - Today's schedule
    - Probable pitchers
    - Team
    - Opponent
    - Venue

Baseball Savant via pybaseball
    - Statcast pitch-level data
    - Last 3 starts
    - Pitching results
    - Batter handedness
    - Contact quality
    - Expected statistics


INSTALL
-------
pip install pybaseball pandas requests


ENVIRONMENT
-----------
Discord webhook:

    Windows PowerShell:
        $env:DISCORD_WEBHOOK_URL="YOUR_WEBHOOK"

    Windows CMD:
        set DISCORD_WEBHOOK_URL=YOUR_WEBHOOK

    Linux/macOS:
        export DISCORD_WEBHOOK_URL="YOUR_WEBHOOK"


RUN
---
Dry run:

    python statcast_alert.py --dry-run

Normal:

    python statcast_alert.py

Custom starting lookback:

    python statcast_alert.py --lookback-days 60


STATE
-----
data/discord_state.json


IMPORTANT
---------
All report statistics are calculated from the SAME last-3-start
Statcast sample.

The initial lookback automatically expands if fewer than 3 starts
are found.
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
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from pybaseball import statcast_pitcher


# ============================================================
# CONFIGURATION
# ============================================================

ROOT = Path(__file__).resolve().parent

DATA_DIR = ROOT / "data"
STATE_FILE = DATA_DIR / "discord_state.json"

MLB_API = "https://statsapi.mlb.com/api/v1"

ET = ZoneInfo("America/New_York")

USER_AGENT = "MLB-Statcast-Alert/4.0"

# Initial Statcast history.
DEFAULT_LOOKBACK_DAYS = 60

# Desired number of starts.
DEFAULT_STARTS = 3

# Automatically expand if fewer than 3 starts are found.
MIN_LOOKBACK_DAYS = 30
MAX_LOOKBACK_DAYS = 365

# HTTP timeout.
REQUEST_TIMEOUT = 45

# Delay between pitchers.
PITCHER_DELAY = 1.0

# Discord retry settings.
DISCORD_MAX_RETRIES = 5

# State retention.
STATE_KEEP_DAYS = 14

HEADSHOT_URL = (
    "https://img.mlbstatic.com/mlb-photos/image/upload/"
    "w_213,d_people:generic:headshots:120:current.png/"
    "q_auto:good/"
    "v1/people/{player_id}/headshot/67/current"
)

DISCORD_MLB_URL = (
    "https://www.mlb.com/player/{player_id}"
)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

LOG = logging.getLogger("mlb-statcast-alert")


# ============================================================
# TIME
# ============================================================

def now_et() -> datetime:
    return datetime.now(ET)


def today_et() -> date:
    return now_et().date()


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


def optional_float(
    value: Any,
) -> float | None:

    try:

        if value is None:
            return None

        if pd.isna(value):
            return None

        return float(value)

    except (TypeError, ValueError):

        return None


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


def normalize_string(
    value: Any,
) -> str:

    if value is None:
        return ""

    try:

        if pd.isna(value):
            return ""

    except Exception:
        pass

    return str(value).strip().lower()


def fmt_pct(
    value: float | None,
) -> str:

    if value is None:
        return "—"

    return f"{value * 100:.1f}%"


def fmt_avg(
    value: float | None,
) -> str:

    if value is None:
        return "—"

    return f"{value:.3f}"


def fmt_one(
    value: float | None,
) -> str:

    if value is None:
        return "—"

    return f"{value:.2f}"


def format_ip(
    outs: int,
) -> str:

    innings = outs // 3
    remainder = outs % 3

    return f"{innings}.{remainder}"


# ============================================================
# STATE
# ============================================================

def empty_state() -> dict:

    return {
        "version": 4,
        "posted": {},
    }


def load_state() -> dict:

    DATA_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    if not STATE_FILE.exists():

        LOG.info(
            "No state file found. Creating new state."
        )

        return empty_state()

    try:

        with STATE_FILE.open(
            "r",
            encoding="utf-8",
        ) as f:

            state = json.load(f)

        if not isinstance(state, dict):

            raise ValueError(
                "State file is not a JSON object."
            )

        if not isinstance(
            state.get("posted"),
            dict,
        ):

            state["posted"] = {}

        state["version"] = 4

        return state

    except Exception as exc:

        LOG.warning(
            "Could not load state file: %s",
            exc,
        )

        return empty_state()


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
    keep_days: int = STATE_KEEP_DAYS,
) -> None:

    posted = state.setdefault(
        "posted",
        {},
    )

    cutoff = (
        today_et()
        - timedelta(days=keep_days)
    )

    remove_keys = []

    for key, record in list(
        posted.items()
    ):

        if not isinstance(
            record,
            dict,
        ):

            remove_keys.append(key)
            continue

        posted_at = record.get(
            "posted_at"
        )

        if not posted_at:
            continue

        try:

            posted_date = (
                datetime.fromisoformat(
                    posted_at
                ).date()
            )

        except Exception:

            continue

        if posted_date < cutoff:

            remove_keys.append(key)

    for key in remove_keys:

        posted.pop(
            key,
            None,
        )

    if remove_keys:

        LOG.info(
            "Pruned %d old state entries.",
            len(remove_keys),
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
        timeout=REQUEST_TIMEOUT,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )

    response.raise_for_status()

    payload = response.json()

    if not isinstance(payload, dict):

        raise ValueError(
            "MLB API returned unexpected JSON."
        )

    return payload


def get_today_schedule() -> list[dict]:

    today = today_et().isoformat()

    LOG.info(
        "Getting MLB schedule for %s...",
        today,
    )

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

        # ----------------------------------------------------
        # AWAY STARTER
        # ----------------------------------------------------

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
                    "pitcher_name": (
                        away_pitcher.get(
                            "fullName",
                            "Unknown",
                        )
                    ),
                }
            )

        # ----------------------------------------------------
        # HOME STARTER
        # ----------------------------------------------------

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
                    "pitcher_name": (
                        home_pitcher.get(
                            "fullName",
                            "Unknown",
                        )
                    ),
                }
            )

    return starters


# ============================================================
# STATCAST DOWNLOAD
# ============================================================

def download_statcast(
    pitcher_id: int,
    lookback_days: int,
) -> pd.DataFrame:

    end_date = today_et()

    start_date = (
        end_date
        - timedelta(days=lookback_days)
    )

    LOG.info(
        "Downloading Statcast for pitcher %s: %s -> %s",
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

    except TypeError:

        try:

            df = statcast_pitcher(
                start_date.isoformat(),
                end_date.isoformat(),
                pitcher_id,
            )

        except Exception as exc:

            LOG.error(
                "Statcast download failed for pitcher %s: %s",
                pitcher_id,
                exc,
            )

            return pd.DataFrame()

    except Exception as exc:

        LOG.error(
            "Statcast download failed for pitcher %s: %s",
            pitcher_id,
            exc,
        )

        return pd.DataFrame()

    if df is None or df.empty:

        LOG.warning(
            "No Statcast data returned for pitcher %s.",
            pitcher_id,
        )

        return pd.DataFrame()

    df = df.copy()

    # --------------------------------------------------------
    # Normalize pitcher ID
    # --------------------------------------------------------

    if "pitcher" in df.columns:

        df["pitcher"] = pd.to_numeric(
            df["pitcher"],
            errors="coerce",
        )

        df = df[
            df["pitcher"] == pitcher_id
        ].copy()

    if df.empty:
        return df

    # --------------------------------------------------------
    # Dates
    # --------------------------------------------------------

    if "game_date" in df.columns:

        df["game_date"] = pd.to_datetime(
            df["game_date"],
            errors="coerce",
        )

    # --------------------------------------------------------
    # Numeric columns
    # --------------------------------------------------------

    numeric_columns = [
        "game_pk",
        "at_bat_number",
        "pitch_number",
        "inning",
        "outs_when_up",
        "batter",
        "pitcher",
    ]

    for column in numeric_columns:

        if column in df.columns:

            df[column] = pd.to_numeric(
                df[column],
                errors="coerce",
            )

    # --------------------------------------------------------
    # Sort
    # --------------------------------------------------------

    sort_columns = [
        column
        for column in [
            "game_date",
            "game_pk",
            "at_bat_number",
            "pitch_number",
        ]
        if column in df.columns
    ]

    if sort_columns:

        df = df.sort_values(
            sort_columns
        ).reset_index(
            drop=True
        )

    LOG.info(
        "Downloaded %d Statcast rows for pitcher %s.",
        len(df),
        pitcher_id,
    )

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

    value = normalize_string(
        event
    )

    return value not in {
        "",
        "nan",
        "none",
        "null",
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

    # --------------------------------------------------------
    # Each completed PA is represented by the final pitch.
    # --------------------------------------------------------

    if {
        "game_pk",
        "at_bat_number",
    }.issubset(pa.columns):

        sort_columns = [
            column
            for column in [
                "game_pk",
                "at_bat_number",
                "pitch_number",
            ]
            if column in pa.columns
        ]

        pa = (
            pa.sort_values(
                sort_columns
            )
            .drop_duplicates(
                [
                    "game_pk",
                    "at_bat_number",
                ],
                keep="last",
            )
        )

    return pa.reset_index(
        drop=True
    )


# ============================================================
# BATTING EVENT DEFINITIONS
# ============================================================

AB_EXCLUDED_EVENTS = {
    "walk",
    "intent_walk",
    "hit_by_pitch",
    "sac_fly",
    "sac_bunt",
    "catcher_interf",
}

HIT_EVENTS = {
    "single",
    "double",
    "triple",
    "home_run",
}

STRIKEOUT_EVENTS = {
    "strikeout",
    "strikeout_double_play",
}

WALK_EVENTS = {
    "walk",
    "intent_walk",
}


# ============================================================
# CONTACT QUALITY
# ============================================================

def calculate_contact_quality(
    pa: pd.DataFrame,
) -> dict:

    if pa.empty:

        return {
            "avg": None,
            "slg": None,
            "woba": None,
            "xwoba": None,
            "xba": None,
            "xslg": None,
            "hard_hit_pct": None,
            "barrel_pct": None,
        }

    events = (
        pa["events"]
        .astype("string")
        .str.lower()
        .str.strip()
    )

    singles = int(
        (events == "single").sum()
    )

    doubles = int(
        (events == "double").sum()
    )

    triples = int(
        (events == "triple").sum()
    )

    home_runs = int(
        (events == "home_run").sum()
    )

    hits = (
        singles
        + doubles
        + triples
        + home_runs
    )

    at_bats = int(
        (~events.isin(
            AB_EXCLUDED_EVENTS
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

    # --------------------------------------------------------
    # wOBA
    #
    # woba_value is a PA-level weighted value in Statcast.
    # woba_denom identifies valid denominator rows.
    # --------------------------------------------------------

    woba = None

    if "woba_value" in pa.columns:

        woba_values = pd.to_numeric(
            pa["woba_value"],
            errors="coerce",
        )

        if "woba_denom" in pa.columns:

            denom = pd.to_numeric(
                pa["woba_denom"],
                errors="coerce",
            )

            valid = (
                woba_values.notna()
                & denom.notna()
                & (denom > 0)
            )

            if valid.any():

                woba = (
                    woba_values[valid].sum()
                    / denom[valid].sum()
                )

        else:

            valid = (
                woba_values.notna()
            )

            if valid.any():

                woba = float(
                    woba_values[valid].mean()
                )

    # --------------------------------------------------------
    # xwOBA
    # --------------------------------------------------------

    xwoba = None

    if (
        "estimated_woba_using_speedangle"
        in pa.columns
    ):

        values = pd.to_numeric(
            pa[
                "estimated_woba_using_speedangle"
            ],
            errors="coerce",
        ).dropna()

        if not values.empty:

            xwoba = float(
                values.mean()
            )

    # --------------------------------------------------------
    # xBA
    # --------------------------------------------------------

    xba = None

    if (
        "estimated_ba_using_speedangle"
        in pa.columns
    ):

        values = pd.to_numeric(
            pa[
                "estimated_ba_using_speedangle"
            ],
            errors="coerce",
        ).dropna()

        if not values.empty:

            xba = float(
                values.mean()
            )

    # --------------------------------------------------------
    # xSLG
    # --------------------------------------------------------

    xslg = None

    if (
        "estimated_slg_using_speedangle"
        in pa.columns
    ):

        values = pd.to_numeric(
            pa[
                "estimated_slg_using_speedangle"
            ],
            errors="coerce",
        ).dropna()

        if not values.empty:

            xslg = float(
                values.mean()
            )

    # --------------------------------------------------------
    # Hard Hit %
    #
    # Statcast hard hit = exit velocity >= 95 mph.
    # Calculated on balls in play with exit velocity.
    # --------------------------------------------------------

    hard_hit_pct = None

    if "launch_speed" in pa.columns:

        exit_velocity = pd.to_numeric(
            pa["launch_speed"],
            errors="coerce",
        )

        valid_ev = (
            exit_velocity.notna()
        )

        if valid_ev.any():

            hard_hits = (
                exit_velocity[valid_ev]
                >= 95.0
            ).sum()

            hard_hit_pct = (
                hard_hits
                / valid_ev.sum()
            )

    # --------------------------------------------------------
    # Barrel %
    #
    # Statcast barrel classification is stored in the
    # launch_speed_angle column as "barrel".
    # --------------------------------------------------------

    barrel_pct = None

    if "launch_speed_angle" in pa.columns:

        batted = pa[
            pa["launch_speed_angle"]
            .notna()
        ].copy()

        if not batted.empty:

            values = (
                batted[
                    "launch_speed_angle"
                ]
                .astype(str)
                .str.lower()
                .str.strip()
            )

            barrel_count = int(
                values.eq("barrel").sum()
            )

            barrel_pct = (
                barrel_count
                / len(batted)
            )

    return {
        "avg": avg,
        "slg": slg,
        "woba": woba,
        "xwoba": xwoba,
        "xba": xba,
        "xslg": xslg,
        "hard_hit_pct": hard_hit_pct,
        "barrel_pct": barrel_pct,
    }


# ============================================================
# PITCHING RESULT STATS
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
    "sac_fly": 1,
    "sac_bunt": 1,
    "sac_fly_double_play": 2,
    "field_error": 0,
    "fielders_choice": 0,
    "walk": 0,
    "intent_walk": 0,
    "hit_by_pitch": 0,
    "single": 0,
    "double": 0,
    "triple": 0,
    "home_run": 0,
}


def calculate_outs(
    pa: pd.DataFrame,
) -> int:

    if pa.empty:
        return 0

    events = (
        pa["events"]
        .astype("string")
        .str.lower()
        .str.strip()
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


def calculate_pitching_results(
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
            "outs": 0,
            "ip": 0.0,
            "ip_display": "0.0",
            "k_pct": None,
            "bb_pct": None,
            "hr9": None,
        }

    events = (
        pa["events"]
        .astype("string")
        .str.lower()
        .str.strip()
    )

    singles = int(
        (events == "single").sum()
    )

    doubles = int(
        (events == "double").sum()
    )

    triples = int(
        (events == "triple").sum()
    )

    home_runs = int(
        (events == "home_run").sum()
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
        (events == "hit_by_pitch").sum()
    )

    strikeouts = int(
        events.isin(
            STRIKEOUT_EVENTS
        ).sum()
    )

    pa_count = len(pa)

    at_bats = int(
        (~events.isin(
            AB_EXCLUDED_EVENTS
        )).sum()
    )

    total_bases = (
        singles
        + (2 * doubles)
        + (3 * triples)
        + (4 * home_runs)
    )

    outs = calculate_outs(
        pa
    )

    ip = outs / 3.0

    k_pct = None

    if pa_count > 0:

        k_pct = (
            strikeouts
            / pa_count
        )

    bb_pct = None

    if pa_count > 0:

        bb_pct = (
            walks
            / pa_count
        )

    hr9 = None

    if ip > 0:

        hr9 = (
            home_runs
            * 9.0
            / ip
        )

    return {
        "pa": pa_count,
        "ab": at_bats,
        "h": hits,
        "hr": home_runs,
        "bb": walks,
        "k": strikeouts,
        "hbp": hbp,
        "tb": total_bases,
        "outs": outs,
        "ip": ip,
        "ip_display": format_ip(outs),
        "k_pct": k_pct,
        "bb_pct": bb_pct,
        "hr9": hr9,
    }


# ============================================================
# COMPLETE REPORT
# ============================================================

def calculate_report(
    df: pd.DataFrame,
) -> dict:

    pitching = calculate_pitching_results(
        df
    )

    pa = plate_appearance_rows(
        df
    )

    contact = calculate_contact_quality(
        pa
    )

    return {
        **pitching,
        **contact,
    }


# ============================================================
# PITCHER HAND
# ============================================================

def get_pitcher_hand(
    df: pd.DataFrame,
) -> str:

    if df.empty:
        return "?"

    if "p_throws" not in df.columns:
        return "?"

    values = (
        df["p_throws"]
        .dropna()
        .astype(str)
        .str.upper()
        .str.strip()
    )

    values = values[
        values.isin(
            ["L", "R"]
        )
    ]

    if values.empty:
        return "?"

    mode = values.mode()

    if mode.empty:
        return "?"

    return str(
        mode.iloc[0]
    )


# ============================================================
# START IDENTIFICATION
# ============================================================

def find_starting_appearances(
    df: pd.DataFrame,
    pitcher_id: int,
) -> list[dict]:

    """
    Identify starts from Statcast.

    Primary rule:
        The pitcher's first recorded pitch of the game
        occurred in the first inning.

    This is designed specifically to separate starts from
    relief appearances.
    """

    if df.empty:
        return []

    required = {
        "game_pk",
        "inning",
    }

    if not required.issubset(
        df.columns
    ):

        LOG.warning(
            "Missing columns needed to identify starts."
        )

        return []

    pitcher_df = df.copy()

    if "pitcher" in pitcher_df.columns:

        pitcher_df = pitcher_df[
            pitcher_df["pitcher"] == pitcher_id
        ].copy()

    if pitcher_df.empty:
        return []

    appearances = []

    for game_pk, game_df in pitcher_df.groupby(
        "game_pk"
    ):

        if pd.isna(game_pk):
            continue

        sort_columns = [
            column
            for column in [
                "inning",
                "at_bat_number",
                "pitch_number",
            ]
            if column in game_df.columns
        ]

        if sort_columns:

            game_df = game_df.sort_values(
                sort_columns
            )

        if game_df.empty:
            continue

        first_inning = safe_int(
            game_df["inning"].iloc[0],
            99,
        )

        if first_inning != 1:
            continue

        game_date = None

        if "game_date" in game_df.columns:

            game_date = game_df[
                "game_date"
            ].iloc[0]

        if game_date is None:
            continue

        if pd.isna(game_date):
            continue

        appearances.append(
            {
                "game_pk": int(game_pk),
                "game_date": game_date,
                "data": game_df.copy(),
            }
        )

    appearances.sort(
        key=lambda item: pd.Timestamp(
            item["game_date"]
        ),
        reverse=True,
    )

    return appearances


# ============================================================
# FIND LAST N STARTS
# ============================================================

def get_last_starts(
    pitcher_id: int,
    initial_lookback: int,
    desired_starts: int = DEFAULT_STARTS,
) -> tuple[
    pd.DataFrame,
    list[dict],
    int,
]:

    lookback = max(
        MIN_LOOKBACK_DAYS,
        initial_lookback,
    )

    while lookback <= MAX_LOOKBACK_DAYS:

        LOG.info(
            "Searching for %d starts using %d-day lookback...",
            desired_starts,
            lookback,
        )

        df = download_statcast(
            pitcher_id,
            lookback,
        )

        if df.empty:

            lookback *= 2

            continue

        starts = find_starting_appearances(
            df,
            pitcher_id,
        )

        LOG.info(
            "Found %d starts in %d-day window.",
            len(starts),
            lookback,
        )

        if len(starts) >= desired_starts:

            return (
                df,
                starts[:desired_starts],
                lookback,
            )

        if lookback == MAX_LOOKBACK_DAYS:
            break

        next_lookback = min(
            MAX_LOOKBACK_DAYS,
            lookback * 2,
        )

        if next_lookback == lookback:
            break

        lookback = next_lookback

    return (
        df if "df" in locals() else pd.DataFrame(),
        starts if "starts" in locals() else [],
        lookback,
    )


# ============================================================
# HAND SPLITS
# ============================================================

def get_hand_splits(
    df: pd.DataFrame,
) -> tuple[
    pd.DataFrame,
    pd.DataFrame,
]:

    if df.empty:
        return (
            pd.DataFrame(),
            pd.DataFrame(),
        )

    pa = plate_appearance_rows(
        df
    )

    if pa.empty:
        return (
            pd.DataFrame(),
            pd.DataFrame(),
        )

    if "stand" not in pa.columns:

        LOG.warning(
            "Statcast data missing stand column."
        )

        return (
            pd.DataFrame(),
            pd.DataFrame(),
        )

    stand = (
        pa["stand"]
        .astype("string")
        .str.upper()
        .str.strip()
    )

    lhb = pa[
        stand == "L"
    ].copy()

    rhb = pa[
        stand == "R"
    ].copy()

    return lhb, rhb


# ============================================================
# START SUMMARY
# ============================================================

def summarize_starts(
    starts: list[dict],
) -> list[dict]:

    summaries = []

    for start in starts:

        report = calculate_report(
            start["data"]
        )

        summaries.append(
            {
                "game_pk": start["game_pk"],
                "game_date": start["game_date"],
                "report": report,
            }
        )

    return summaries


# ============================================================
# DISCORD FORMATTING
# ============================================================

def format_split(
    report: dict,
) -> str:

    if report["pa"] == 0:

        return (
            "No Statcast plate appearances."
        )

    return (
        f"PA **{report['pa']}** | "
        f"AVG **{fmt_avg(report['avg'])}** | "
        f"SLG **{fmt_avg(report['slg'])}**\n"
        f"K% **{fmt_pct(report['k_pct'])}** | "
        f"BB% **{fmt_pct(report['bb_pct'])}**\n"
        f"wOBA **{fmt_avg(report['woba'])}** | "
        f"xwOBA **{fmt_avg(report['xwoba'])}**"
    )


def make_discord_embed(
    starter: dict,
    starts: list[dict],
) -> dict:

    if not starts:

        raise ValueError(
            "No starting appearances found."
        )

    # --------------------------------------------------------
    # Combine ONLY the last 3 starts.
    # --------------------------------------------------------

    recent_df = pd.concat(
        [
            start["data"]
            for start in starts
        ],
        ignore_index=True,
    )

    overall = calculate_report(
        recent_df
    )

    pitcher_hand = get_pitcher_hand(
        recent_df
    )

    lhb_df, rhb_df = get_hand_splits(
        recent_df
    )

    lhb = calculate_report(
        lhb_df
    )

    rhb = calculate_report(
        rhb_df
    )

    start_summaries = summarize_starts(
        starts
    )

    # --------------------------------------------------------
    # Last 3 starts
    # --------------------------------------------------------

    start_lines = []

    for item in start_summaries:

        report = item["report"]

        start_date = pd.Timestamp(
            item["game_date"]
        )

        start_lines.append(
            f"**{start_date.strftime('%b %d')}** — "
            f"{report['ip_display']} IP | "
            f"{report['h']} H | "
            f"{report['hr']} HR | "
            f"{report['bb']} BB | "
            f"{report['k']} K"
        )

    starts_text = "\n".join(
        start_lines
    )

    number_of_starts = len(
        starts
    )

    # --------------------------------------------------------
    # Combined
    # --------------------------------------------------------

    combined_text = (
        f"{overall['ip_display']} IP | "
        f"{overall['h']} H | "
        f"{overall['hr']} HR | "
        f"{overall['bb']} BB | "
        f"{overall['k']} K\n"
        f"K% **{fmt_pct(overall['k_pct'])}** | "
        f"BB% **{fmt_pct(overall['bb_pct'])}** | "
        f"HR/9 **{fmt_one(overall['hr9'])}**"
    )

    # --------------------------------------------------------
    # Contact quality
    # --------------------------------------------------------

    contact_text = (
        f"AVG **{fmt_avg(overall['avg'])}**\n"
        f"SLG **{fmt_avg(overall['slg'])}**\n"
        f"wOBA **{fmt_avg(overall['woba'])}**\n"
        f"xwOBA **{fmt_avg(overall['xwoba'])}**\n"
        f"xBA **{fmt_avg(overall['xba'])}**\n"
        f"xSLG **{fmt_avg(overall['xslg'])}**\n"
        f"Hard Hit **{fmt_pct(overall['hard_hit_pct'])}**\n"
        f"Barrel **{fmt_pct(overall['barrel_pct'])}**"
    )

    # --------------------------------------------------------
    # Description
    # --------------------------------------------------------

    description = (
        f"🔥 **{starter['pitcher_name']}** — "
        f"Statcast Pitching Alert\n\n"

        f"**{starter['team']}** @ "
        f"**{starter['opponent']}**\n"
        f"{starter['venue']}\n"
        f"Throws: **{pitcher_hand}**\n\n"

        f"### LAST {number_of_starts} STARTS\n"
        f"{starts_text}\n\n"

        f"### COMBINED\n"
        f"{combined_text}\n\n"

        f"### CONTACT QUALITY\n"
        f"{contact_text}\n\n"

        f"### VS LHB\n"
        f"{format_split(lhb)}\n\n"

        f"### VS RHB\n"
        f"{format_split(rhb)}"
    )

    # Discord embed description limit is 4096.
    # Stay safely below it.
    if len(description) > 4000:

        description = (
            description[:3985]
            + "\n..."
        )

    return {
        "title": (
            f"🔥 {starter['pitcher_name']} "
            f"— Statcast Pitching Alert"
        ),

        "description": description,

        "url": DISCORD_MLB_URL.format(
            player_id=starter[
                "pitcher_id"
            ]
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
                "Baseball Savant Statcast "
                "via pybaseball"
            )
        },
    }


# ============================================================
# DISCORD PAYLOAD
# ============================================================

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

    if not webhook_url:

        raise ValueError(
            "Discord webhook URL is empty."
        )

    last_exception = None

    for attempt in range(
        1,
        DISCORD_MAX_RETRIES + 1,
    ):

        try:

            response = requests.post(
                webhook_url,
                json=payload,
                timeout=REQUEST_TIMEOUT,
                headers={
                    "User-Agent": USER_AGENT,
                },
            )

            # ------------------------------------------------
            # Success
            # ------------------------------------------------

            if 200 <= response.status_code < 300:

                return

            # ------------------------------------------------
            # Retryable
            # ------------------------------------------------

            if response.status_code in {
                429,
                500,
                502,
                503,
                504,
            }:

                retry_after = response.headers.get(
                    "Retry-After"
                )

                if retry_after:

                    try:

                        delay = float(
                            retry_after
                        )

                    except ValueError:

                        delay = 2 ** attempt

                else:

                    delay = 2 ** attempt

                LOG.warning(
                    "Discord HTTP %s. "
                    "Retrying in %.1f seconds "
                    "(attempt %d/%d).",
                    response.status_code,
                    delay,
                    attempt,
                    DISCORD_MAX_RETRIES,
                )

                time.sleep(
                    min(
                        delay,
                        30,
                    )
                )

                continue

            response.raise_for_status()

        except Exception as exc:

            last_exception = exc

            if attempt >= DISCORD_MAX_RETRIES:
                break

            delay = min(
                2 ** attempt,
                30,
            )

            LOG.warning(
                "Discord request failed: %s. "
                "Retrying in %d seconds "
                "(attempt %d/%d).",
                exc,
                delay,
                attempt,
                DISCORD_MAX_RETRIES,
            )

            time.sleep(
                delay
            )

    if last_exception:

        raise RuntimeError(
            "Discord posting failed after retries."
        ) from last_exception

    raise RuntimeError(
        "Discord posting failed after retries."
    )


# ============================================================
# DUPLICATE PROTECTION
# ============================================================

def make_alert_key(
    starter: dict,
) -> str:

    game_date = str(
        starter.get(
            "game_date",
            "",
        )
    )[:10]

    return (
        f"{game_date}:"
        f"{starter['game_pk']}:"
        f"{starter['pitcher_id']}"
    )


def was_already_posted(
    state: dict,
    alert_key: str,
) -> bool:

    return (
        alert_key
        in state.get(
            "posted",
            {},
        )
    )


# ============================================================
# PROCESS ONE STARTER
# ============================================================

def process_starter(
    starter: dict,
    lookback_days: int,
) -> list[dict]:

    pitcher_name = starter[
        "pitcher_name"
    ]

    pitcher_id = starter[
        "pitcher_id"
    ]

    (
        df,
        starts,
        actual_lookback,
    ) = get_last_starts(
        pitcher_id=pitcher_id,
        initial_lookback=lookback_days,
        desired_starts=DEFAULT_STARTS,
    )

    if df.empty:

        raise RuntimeError(
            f"No Statcast data for "
            f"{pitcher_name}."
        )

    if len(starts) < DEFAULT_STARTS:

        raise RuntimeError(
            f"Only found {len(starts)} "
            f"starts for {pitcher_name} "
            f"after searching "
            f"{actual_lookback} days."
        )

    LOG.info(
        "%s: using last %d starts "
        "(lookback %d days).",
        pitcher_name,
        len(starts),
        actual_lookback,
    )

    return starts


# ============================================================
# DRY RUN
# ============================================================

def print_dry_run(
    starter: dict,
    payload: dict,
) -> None:

    embed = payload[
        "embeds"
    ][0]

    print()
    print("=" * 90)
    print(
        f"{starter['pitcher_name']} | "
        f"{starter['team']} @ "
        f"{starter['opponent']}"
    )
    print("=" * 90)

    print(
        embed["title"]
    )

    print()

    print(
        embed["description"]
    )

    print()

    print("=" * 90)
    print()


# ============================================================
# RUN
# ============================================================

def run(
    dry_run: bool,
    lookback_days: int,
) -> int:

    webhook_url = os.getenv(
        "DISCORD_WEBHOOK_URL",
        "",
    ).strip()

    if not dry_run and not webhook_url:

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
        state
    )

    save_state(
        state
    )

    # --------------------------------------------------------
    # Schedule
    # --------------------------------------------------------

    try:

        games = get_today_schedule()

    except Exception as exc:

        LOG.exception(
            "Unable to retrieve MLB schedule: %s",
            exc,
        )

        return 1

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

    successful = 0
    failed = 0
    skipped = 0

    for index, starter in enumerate(
        starters,
        start=1,
    ):

        pitcher_name = starter[
            "pitcher_name"
        ]

        LOG.info(
            "[%d/%d] Processing %s...",
            index,
            len(starters),
            pitcher_name,
        )

        alert_key = make_alert_key(
            starter
        )

        # ----------------------------------------------------
        # Duplicate
        # ----------------------------------------------------

        if was_already_posted(
            state,
            alert_key,
        ):

            LOG.info(
                "SKIP duplicate: %s",
                pitcher_name,
            )

            skipped += 1

            continue

        try:

            # ------------------------------------------------
            # Statcast
            # ------------------------------------------------

            starts = process_starter(
                starter,
                lookback_days,
            )

            # ------------------------------------------------
            # Payload
            # ------------------------------------------------

            payload = make_discord_payload(
                starter,
                starts,
            )

            # ------------------------------------------------
            # Dry run
            # ------------------------------------------------

            if dry_run:

                print_dry_run(
                    starter,
                    payload,
                )

                LOG.info(
                    "DRY RUN complete: %s "
                    "(not marked as posted).",
                    pitcher_name,
                )

                successful += 1

                continue

            # ------------------------------------------------
            # Post
            # ------------------------------------------------

            LOG.info(
                "Posting %s to Discord...",
                pitcher_name,
            )

            send_to_discord(
                webhook_url,
                payload,
            )

            # ------------------------------------------------
            # ONLY mark posted AFTER successful Discord POST
            # ------------------------------------------------

            state.setdefault(
                "posted",
                {},
            )

            state["posted"][
                alert_key
            ] = {
                "posted_at": (
                    now_et()
                    .isoformat(
                        timespec="seconds"
                    )
                ),
                "pitcher_id": starter[
                    "pitcher_id"
                ],
                "pitcher_name": pitcher_name,
                "game_pk": starter[
                    "game_pk"
                ],
                "game_date": str(
                    starter[
                        "game_date"
                    ]
                )[:10],
            }

            save_state(
                state
            )

            successful += 1

            LOG.info(
                "Completed: %s",
                pitcher_name,
            )

            # Give Savant/Discord a little breathing room.
            time.sleep(
                PITCHER_DELAY
            )

        except Exception as exc:

            failed += 1

            LOG.exception(
                "ERROR processing %s: %s",
                pitcher_name,
                exc,
            )

            # ------------------------------------------------
            # Failed alerts are NOT marked posted.
            # The next run can try again.
            # ------------------------------------------------

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    LOG.info(
        "Run complete | "
        "successful=%d | "
        "failed=%d | "
        "skipped=%d",
        successful,
        failed,
        skipped,
    )

    return 0


# ============================================================
# COMMAND LINE
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
            "Generate alerts without posting "
            "to Discord. Dry runs are never "
            "recorded as posted."
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
            "Initial number of days of Statcast "
            "history to download. The script "
            "automatically expands this window "
            "when necessary."
        ),
    )

    args = parser.parse_args()

    lookback_days = max(
        MIN_LOOKBACK_DAYS,
        args.lookback_days,
    )

    LOG.info(
        "Initial Statcast lookback: %d days",
        lookback_days,
    )

    LOG.info(
        "Maximum Statcast lookback: %d days",
        MAX_LOOKBACK_DAYS,
    )

    LOG.info(
        "Required recent starts: %d",
        DEFAULT_STARTS,
    )

    LOG.info(
        "Dry run: %s",
        args.dry_run,
    )

    return run(
        dry_run=args.dry_run,
        lookback_days=lookback_days,
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    sys.exit(
        main()
    )
