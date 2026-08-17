#!/usr/bin/env python3

"""
MLB Statcast Probable Starter Discord Alert

DATA RULES
----------
1. MLB Stats API:
   - Today's schedule
   - Probable pitcher identification
   - Venue / teams

2. Baseball Savant through pybaseball:
   - All pitching statistics
   - Last 3 starts
   - LHB/RHB splits
   - Statcast handedness
   - wOBA / xwOBA
   - HR / K / BB / H
   - innings / workload

3. No FanGraphs pitching statistics.
4. No Baseball Reference pitching statistics.
5. No MLB pitching-stat endpoints.

FEATURES
--------
- Today's probable MLB starters
- Last 3 starting appearances
- HR allowed
- H / BB / K
- LHB vs RHB splits
- AVG / SLG / wOBA / xwOBA
- K%
- BB%
- HR/9
- Pitcher throwing hand
- MLB player headshot
- Discord webhook
- Persistent duplicate protection
- --dry-run
- Pitcher-specific Statcast downloads
- Retry handling
- Graceful handling of missing/no-start pitchers

INSTALL
-------
pip install -r requirements.txt

RUN
---
python statcast_alert.py

TEST WITHOUT DISCORD
--------------------
python statcast_alert.py --dry-run

OPTIONAL
--------
python statcast_alert.py --dry-run --lookback-days 60
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

from pybaseball import statcast_pitcher


# ============================================================
# CONFIGURATION
# ============================================================

ROOT = Path(__file__).resolve().parent

DATA_DIR = ROOT / "data"

STATE_FILE = DATA_DIR / "discord_state.json"

MLB_API = "https://statsapi.mlb.com/api/v1"

USER_AGENT = "MLB-Statcast-Alert/2.0"

DEFAULT_LOOKBACK_DAYS = 45

DEFAULT_RECENT_STARTS = 3

STATE_KEEP_DAYS = 14

HTTP_TIMEOUT = 30

DISCORD_TIMEOUT = 30

DISCORD_DELAY = 1.0

MAX_HTTP_RETRIES = 4

MAX_DISCORD_RETRIES = 4


HEADSHOT_URL = (
    "https://img.mlbstatic.com/mlb-photos/image/upload/"
    "w_213,d_people:generic:headshots:120:current.png/"
    "q_auto:good/"
    "v1/people/{player_id}/headshot/67/current"
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
# HTTP SESSION
# ============================================================

SESSION = requests.Session()

SESSION.headers.update(
    {
        "User-Agent": USER_AGENT,
        "Accept": "application/json,text/csv,*/*",
    }
)


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

    except (
        TypeError,
        ValueError,
    ):
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

    except (
        TypeError,
        ValueError,
    ):
        return default


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

    return f"{value:.1f}"


def format_ip(
    outs: int,
) -> str:

    innings = outs // 3

    remainder = outs % 3

    return f"{innings}.{remainder}"


def normalize_hand(
    value: Any,
) -> str:

    if value is None:
        return "?"

    try:

        if pd.isna(value):
            return "?"

    except Exception:
        pass

    value = str(value).strip().upper()

    if value in {"L", "LEFT"}:
        return "L"

    if value in {"R", "RIGHT"}:
        return "R"

    return "?"


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

        if not isinstance(state, dict):

            raise ValueError(
                "State file is not a JSON object"
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
            "Could not load state file: %s",
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
    keep_days: int = STATE_KEEP_DAYS,
) -> None:

    posted = state.setdefault(
        "posted",
        {},
    )

    cutoff = (
        date.today()
        - timedelta(days=keep_days)
    )

    remove_keys = []

    for key, record in posted.items():

        try:

            posted_date = datetime.fromisoformat(
                record["posted_at"]
            ).date()

        except Exception:

            continue

        if posted_date < cutoff:

            remove_keys.append(key)

    for key in remove_keys:

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

    last_error: Exception | None = None

    for attempt in range(
        1,
        MAX_HTTP_RETRIES + 1,
    ):

        try:

            response = SESSION.get(
                url,
                params=params,
                timeout=HTTP_TIMEOUT,
            )

            if response.status_code == 429:

                wait = min(
                    2 ** attempt,
                    15,
                )

                LOG.warning(
                    "MLB API rate limited. "
                    "Waiting %ss...",
                    wait,
                )

                time.sleep(wait)

                continue

            response.raise_for_status()

            return response.json()

        except Exception as exc:

            last_error = exc

            if attempt >= MAX_HTTP_RETRIES:
                break

            wait = min(
                2 ** attempt,
                10,
            )

            LOG.warning(
                "MLB request failed "
                "(attempt %d/%d): %s",
                attempt,
                MAX_HTTP_RETRIES,
                exc,
            )

            time.sleep(wait)

    raise RuntimeError(
        f"MLB API request failed: {last_error}"
    )


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

        # ----------------------------------------------------
        # Away
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
        # Home
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

def download_pitcher_statcast(
    pitcher_id: int,
    lookback_days: int,
) -> pd.DataFrame:

    end_date = date.today()

    start_date = (
        end_date
        - timedelta(days=lookback_days)
    )

    LOG.info(
        "Downloading Statcast for pitcher %s: "
        "%s -> %s",
        pitcher_id,
        start_date,
        end_date,
    )

    try:

        # IMPORTANT:
        #
        # Use statcast_pitcher() instead of statcast()
        # and then filtering all MLB pitches.
        #
        # This directly asks Baseball Savant for
        # this pitcher.
        #
        df = statcast_pitcher(
            start_dt=start_date.isoformat(),
            end_dt=end_date.isoformat(),
            player_id=pitcher_id,
        )

    except Exception as exc:

        raise RuntimeError(
            f"Statcast request failed for "
            f"pitcher {pitcher_id}: {exc}"
        ) from exc

    if df is None:

        return pd.DataFrame()

    if df.empty:

        return pd.DataFrame()

    df = df.copy()

    # --------------------------------------------------------
    # Normalize important columns
    # --------------------------------------------------------

    for column in [
        "pitcher",
        "batter",
        "game_pk",
        "at_bat_number",
        "pitch_number",
        "inning",
        "outs_when_up",
    ]:

        if column in df.columns:

            df[column] = pd.to_numeric(
                df[column],
                errors="coerce",
            )

    if "game_date" in df.columns:

        df["game_date"] = pd.to_datetime(
            df["game_date"],
            errors="coerce",
        )

    # --------------------------------------------------------
    # Make absolutely sure this is the requested pitcher.
    # --------------------------------------------------------

    if "pitcher" in df.columns:

        df = df[
            df["pitcher"]
            == pitcher_id
        ].copy()

    return df.reset_index(
        drop=True
    )


# ============================================================
# PITCHER HAND
# ============================================================

def get_pitcher_hand(
    df: pd.DataFrame,
) -> str:

    if df.empty:

        return "?"

    # Baseball Savant normally supplies p_throws.
    if "p_throws" in df.columns:

        values = (
            df["p_throws"]
            .dropna()
            .astype(str)
            .str.upper()
        )

        for value in values:

            if value in {
                "L",
                "R",
            }:

                return value

    return "?"


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

    value = (
        str(event)
        .strip()
        .lower()
    )

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

    # One completed event per PA.
    #
    # The final pitch row of the PA contains
    # the completed event.
    #
    if {
        "game_pk",
        "at_bat_number",
    }.issubset(pa.columns):

        sort_columns = [
            "game_pk",
            "at_bat_number",
        ]

        if "pitch_number" in pa.columns:

            sort_columns.append(
                "pitch_number"
            )

        pa = (
            pa.sort_values(
                sort_columns,
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

    return pa.reset_index(
        drop=True
    )


# ============================================================
# BATTING STATISTICS
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
        .str.strip()
    )

    singles = int(
        (
            events
            == "single"
        ).sum()
    )

    doubles = int(
        (
            events
            == "double"
        ).sum()
    )

    triples = int(
        (
            events
            == "triple"
        ).sum()
    )

    home_runs = int(
        (
            events
            == "home_run"
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
            [
                "walk",
                "intent_walk",
            ]
        ).sum()
    )

    hbp = int(
        (
            events
            == "hit_by_pitch"
        ).sum()
    )

    strikeouts = int(
        events.isin(
            [
                "strikeout",
                "strikeout_double_play",
            ]
        ).sum()
    )

    plate_appearances = len(
        pa
    )

    # AB excludes BB/HBP.
    #
    # Sacrifice flies/bunts are also not AB,
    # but the Statcast event data requires
    # explicit handling.
    sacrifice_events = int(
        events.isin(
            [
                "sac_fly",
                "sac_bunt",
                "sac_fly_double_play",
            ]
        ).sum()
    )

    at_bats = (
        plate_appearances
        - walks
        - hbp
        - sacrifice_events
    )

    total_bases = (
        singles
        + (
            2
            * doubles
        )
        + (
            3
            * triples
        )
        + (
            4
            * home_runs
        )
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
    # --------------------------------------------------------

    woba = None

    if "woba_value" in pa.columns:

        values = pd.to_numeric(
            pa["woba_value"],
            errors="coerce",
        )

        values = values.dropna()

        if not values.empty:

            woba = float(
                values.mean()
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
        )

        values = values.dropna()

        if not values.empty:

            xwoba = float(
                values.mean()
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
        "woba": woba,
        "xwoba": xwoba,
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

    if "events" not in pa.columns:

        return 0

    events = (
        pa["events"]
        .astype(str)
        .str.lower()
        .str.strip()
    )

    outs = 0

    for event in events:

        outs += OUT_EVENTS.get(
            event,
            0,
        )

    return int(outs)


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

    ip = (
        outs
        / 3
    )

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
# START IDENTIFICATION
# ============================================================

def find_starting_appearances(
    df: pd.DataFrame,
    pitcher_id: int,
) -> list[dict]:

    """
    Identify starting appearances from Statcast.

    Primary rule:
        The pitcher's first Statcast pitch in a game
        occurred during inning 1.

    Additional protections:
        - Valid game_pk
        - Valid game_date
        - Sort by actual pitch sequence
        - Only include the requested pitcher
    """

    if df.empty:

        return []

    if "pitcher" not in df.columns:

        return []

    pitcher_df = df[
        df["pitcher"]
        == pitcher_id
    ].copy()

    if pitcher_df.empty:

        return []

    appearances = []

    for game_pk, game_df in pitcher_df.groupby(
        "game_pk",
        dropna=True,
    ):

        if pd.isna(game_pk):

            continue

        sort_columns = []

        if "game_date" in game_df.columns:
            sort_columns.append(
                "game_date"
            )

        if "inning" in game_df.columns:
            sort_columns.append(
                "inning"
            )

        if "at_bat_number" in game_df.columns:
            sort_columns.append(
                "at_bat_number"
            )

        if "pitch_number" in game_df.columns:
            sort_columns.append(
                "pitch_number"
            )

        if sort_columns:

            game_df = (
                game_df
                .sort_values(
                    sort_columns,
                    na_position="last",
                )
            )

        if game_df.empty:

            continue

        first_inning = safe_int(
            game_df["inning"].iloc[0]
            if "inning" in game_df.columns
            else None,
            99,
        )

        if first_inning != 1:

            continue

        game_date = None

        if "game_date" in game_df.columns:

            game_date = (
                game_df["game_date"]
                .iloc[0]
            )

        if pd.isna(game_date):

            continue

        appearances.append(
            {
                "game_pk": int(
                    safe_int(
                        game_pk
                    )
                ),
                "game_date": game_date,
                "data": game_df.copy(),
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
# ROBUST HAND SPLITS
# ============================================================

def get_batter_hand_column(
    df: pd.DataFrame,
) -> str | None:

    # Normal Statcast column.
    if "batter_stands" in df.columns:

        values = (
            df["batter_stands"]
            .astype("string")
            .str.upper()
        )

        if values.isin(
            ["L", "R"]
        ).any():

            return "batter_stands"

    # Fallback used by some data versions.
    if "stand" in df.columns:

        values = (
            df["stand"]
            .astype("string")
            .str.upper()
        )

        if values.isin(
            ["L", "R"]
        ).any():

            return "stand"

    return None


def split_by_batter_hand(
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

    column = get_batter_hand_column(
        df
    )

    if column is None:

        return (
            pd.DataFrame(),
            pd.DataFrame(),
        )

    hands = (
        df[column]
        .astype("string")
        .str.upper()
    )

    lhb = df[
        hands == "L"
    ].copy()

    rhb = df[
        hands == "R"
    ].copy()

    return lhb, rhb


# ============================================================
# DISCORD START LINES
# ============================================================

def make_start_lines(
    starts: list[dict],
) -> str:

    lines = []

    for start in starts:

        report = calculate_pitching_report(
            start["data"]
        )

        start_date = pd.Timestamp(
            start["game_date"]
        )

        lines.append(
            f"**{start_date.strftime('%b %d')}** — "
            f"IP {report['ip_display']} | "
            f"H {report['h']} | "
            f"HR {report['hr']} | "
            f"BB {report['bb']} | "
            f"K {report['k']}"
        )

    return "\n".join(
        lines
    )


# ============================================================
# SPLIT FORMATTER
# ============================================================

def format_split(
    report: dict,
) -> str:

    if report["pa"] == 0:

        return (
            "PA **0** | "
            "H **0** | "
            "HR **0**\n"
            "BB **0** | "
            "K **0**\n"
            "AVG **—** | "
            "SLG **—**\n"
            "wOBA **—** | "
            "xwOBA **—**"
        )

    return (
        f"PA **{report['pa']}** | "
        f"H **{report['h']}** | "
        f"HR **{report['hr']}**\n"
        f"BB **{report['bb']}** | "
        f"K **{report['k']}**\n"
        f"AVG **{fmt_avg(report['avg'])}** | "
        f"SLG **{fmt_avg(report['slg'])}**\n"
        f"wOBA **{fmt_avg(report['woba'])}** | "
        f"xwOBA **{fmt_avg(report['xwoba'])}**"
    )


# ============================================================
# DISCORD EMBED
# ============================================================

def make_discord_embed(
    starter: dict,
    starts: list[dict],
    pitcher_hand: str,
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

    # --------------------------------------------------------
    # LHB / RHB
    # --------------------------------------------------------

    lhb_df, rhb_df = (
        split_by_batter_hand(
            recent_df
        )
    )

    lhb = calculate_pitching_report(
        lhb_df
    )

    rhb = calculate_pitching_report(
        rhb_df
    )

    # --------------------------------------------------------
    # Last starts
    # --------------------------------------------------------

    starts_text = make_start_lines(
        starts
    )

    # --------------------------------------------------------
    # Description
    # --------------------------------------------------------

    location = (
        "Home"
        if starter["home"]
        else "Away"
    )

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
        f"{format_split(lhb)}\n\n"

        f"### vs RHB\n"
        f"{format_split(rhb)}"
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


# ============================================================
# DISCORD PAYLOAD
# ============================================================

def make_discord_payload(
    starter: dict,
    starts: list[dict],
    pitcher_hand: str,
) -> dict:

    embed = make_discord_embed(
        starter,
        starts,
        pitcher_hand,
    )

    return {
        "username": "MLB Statcast Alert",
        "embeds": [
            embed
        ],
    }


# ============================================================
# DISCORD POST
# ============================================================

def send_to_discord(
    webhook_url: str,
    payload: dict,
) -> None:

    last_error: Exception | None = None

    for attempt in range(
        1,
        MAX_DISCORD_RETRIES + 1,
    ):

        try:

            response = SESSION.post(
                webhook_url,
                json=payload,
                timeout=DISCORD_TIMEOUT,
            )

            if response.status_code == 429:

                retry_after = 2

                try:

                    retry_after = float(
                        response.json().get(
                            "retry_after",
                            2,
                        )
                    )

                except Exception:

                    pass

                retry_after = min(
                    retry_after,
                    30,
                )

                LOG.warning(
                    "Discord rate limited. "
                    "Waiting %.1fs...",
                    retry_after,
                )

                time.sleep(
                    retry_after
                )

                continue

            response.raise_for_status()

            return

        except Exception as exc:

            last_error = exc

            if attempt >= MAX_DISCORD_RETRIES:

                break

            wait = min(
                2 ** attempt,
                10,
            )

            LOG.warning(
                "Discord request failed "
                "(attempt %d/%d): %s",
                attempt,
                MAX_DISCORD_RETRIES,
                exc,
            )

            time.sleep(wait)

    raise RuntimeError(
        f"Discord post failed: "
        f"{last_error}"
    )


# ============================================================
# ALERT KEY
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
# PROCESS ONE STARTER
# ============================================================

def process_starter(
    starter: dict,
    lookback_days: int,
) -> tuple[
    pd.DataFrame,
    list[dict],
    str,
]:

    pitcher_id = starter[
        "pitcher_id"
    ]

    pitcher_name = starter[
        "pitcher_name"
    ]

    df = download_pitcher_statcast(
        pitcher_id,
        lookback_days,
    )

    if df.empty:

        raise RuntimeError(
            f"No Statcast data for "
            f"{pitcher_name}"
        )

    pitcher_hand = get_pitcher_hand(
        df
    )

    LOG.info(
        "%s: throws %s",
        pitcher_name,
        pitcher_hand,
    )

    starts = find_starting_appearances(
        df,
        pitcher_id,
    )

    if not starts:

        raise RuntimeError(
            f"No starts identified for "
            f"{pitcher_name}"
        )

    starts = starts[
        :DEFAULT_RECENT_STARTS
    ]

    LOG.info(
        "%s: found %d recent starts",
        pitcher_name,
        len(starts),
    )

    return (
        df,
        starts,
        pitcher_hand,
    )


# ============================================================
# MAIN RUN
# ============================================================

def run(
    dry_run: bool,
    lookback_days: int,
) -> int:

    webhook_url = (
        os.getenv(
            "DISCORD_WEBHOOK_URL",
            "",
        )
        .strip()
    )

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
        keep_days=STATE_KEEP_DAYS,
    )

    save_state(state)

    # --------------------------------------------------------
    # Schedule
    # --------------------------------------------------------

    LOG.info(
        "Getting today's MLB schedule..."
    )

    try:

        games = get_today_schedule()

    except Exception as exc:

        LOG.exception(
            "Could not get MLB schedule: %s",
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

    completed = 0

    skipped = 0

    errors = 0

    # --------------------------------------------------------
    # Process pitchers
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

        # ----------------------------------------------------
        # Duplicate protection
        # ----------------------------------------------------

        if (
            alert_key
            in state["posted"]
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

            (
                df,
                starts,
                pitcher_hand,
            ) = process_starter(
                starter,
                lookback_days,
            )

            # ------------------------------------------------
            # Payload
            # ------------------------------------------------

            payload = make_discord_payload(
                starter,
                starts,
                pitcher_hand,
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
                        ensure_ascii=False,
                    )
                )

                print(
                    "=" * 90
                )
                print()

            # ------------------------------------------------
            # Discord
            # ------------------------------------------------

            else:

                LOG.info(
                    "Posting %s to Discord...",
                    pitcher_name,
                )

                send_to_discord(
                    webhook_url,
                    payload,
                )

                time.sleep(
                    DISCORD_DELAY
                )

            # ------------------------------------------------
            # Save state
            # ------------------------------------------------

            state["posted"][
                alert_key
            ] = {
                "posted_at": (
                    datetime.now()
                    .isoformat(
                        timespec="seconds"
                    )
                ),
                "pitcher_id": (
                    pitcher_id
                ),
                "pitcher_name": (
                    pitcher_name
                ),
                "game_pk": (
                    starter["game_pk"]
                ),
                "throws": pitcher_hand,
            }

            save_state(
                state
            )

            LOG.info(
                "Completed: %s",
                pitcher_name,
            )

            completed += 1

        except Exception as exc:

            errors += 1

            LOG.exception(
                "ERROR processing %s: %s",
                pitcher_name,
                exc,
            )

            # IMPORTANT:
            #
            # We intentionally DO NOT write the
            # pitcher to posted state when processing
            # fails.
            #
            # This means a later run can try again.

            continue

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    LOG.info(
        "Run complete | completed=%d "
        "skipped=%d errors=%d",
        completed,
        skipped,
        errors,
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
            "history to download"
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


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    sys.exit(
        main()
    )
