#!/usr/bin/env python3

"""
MLB Statcast Probable Starter Discord Alert

Data rules:
- MLB Stats API: schedule + probable pitcher identification
- Baseball Savant Statcast via pybaseball: pitching statistics
- No FanGraphs pitching stats
- No Baseball Reference pitching stats
- No MLB pitching-stat endpoints

Features:
- Today's probable MLB starters
- Last 3 starting appearances
- IP / H / HR / BB / K
- K%
- BB%
- HR/9
- LHB vs RHB splits
- AVG / SLG / wOBA / xwOBA
- Pitcher's throwing hand
- MLB player headshot
- Discord webhook
- Persistent duplicate protection
- --dry-run
- Robust Statcast pitcher downloads
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

        return int(float(value))

    except (TypeError, ValueError):

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


# ============================================================
# STATE
# ============================================================

def default_state() -> dict:

    return {
        "version": 2,
        "posted": {},
    }


def load_state() -> dict:

    DATA_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    LOG.info(
        "Duplicate state file: %s",
        STATE_FILE,
    )

    if not STATE_FILE.exists():

        LOG.info(
            "No existing duplicate state file found."
        )

        return default_state()

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

        if not isinstance(
            state["posted"],
            dict,
        ):

            state["posted"] = {}

        LOG.info(
            "Loaded %d previously posted alerts.",
            len(state["posted"]),
        )

        return state

    except Exception as exc:

        LOG.warning(
            "Could not load state file: %s",
            exc,
        )

        return default_state()


def save_state(
    state: dict,
) -> None:

    DATA_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary = STATE_FILE.with_name(
        STATE_FILE.name + ".tmp"
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

        f.flush()

        os.fsync(
            f.fileno()
        )

    temporary.replace(
        STATE_FILE
    )

    LOG.info(
        "Saved duplicate state: %s",
        STATE_FILE,
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

    remove_keys = []

    for key, record in list(
        posted.items()
    ):

        try:

            posted_at = record.get(
                "posted_at",
                "",
            )

            posted_date = (
                datetime
                .fromisoformat(
                    posted_at
                )
                .date()
            )

        except Exception:

            continue

        if posted_date < cutoff:

            remove_keys.append(
                key
            )

    for key in remove_keys:

        posted.pop(
            key,
            None,
        )

    if remove_keys:

        LOG.info(
            "Pruned %d old duplicate records.",
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

def get_throwing_hand(
    probable_pitcher: dict,
) -> str:

    """
    Try to obtain throwing hand from the
    probable pitcher object returned by the
    MLB schedule endpoint.

    Returns:
        R
        L
        ?
    """

    hand = (
        probable_pitcher
        .get("pitchHand", {})
        .get("code")
    )

    if hand in {"R", "L"}:

        return hand

    # Some MLB responses use:
    # pitchHand: {"code": "R", "description": "Right"}

    description = str(
        probable_pitcher
        .get("pitchHand", {})
        .get("description", "")
    ).lower()

    if "right" in description:

        return "R"

    if "left" in description:

        return "L"

    return "?"


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
                    "game_pk": safe_int(
                        game_pk
                    ),

                    "game_date": game_date,

                    "venue": venue,

                    "team": away_team,

                    "opponent": home_team,

                    "home": False,

                    "pitcher_id": safe_int(
                        away_pitcher["id"]
                    ),

                    "pitcher_name": (
                        away_pitcher.get(
                            "fullName",
                            "Unknown",
                        )
                    ),

                    "throws": get_throwing_hand(
                        away_pitcher
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
                    "game_pk": safe_int(
                        game_pk
                    ),

                    "game_date": game_date,

                    "venue": venue,

                    "team": home_team,

                    "opponent": away_team,

                    "home": True,

                    "pitcher_id": safe_int(
                        home_pitcher["id"]
                    ),

                    "pitcher_name": (
                        home_pitcher.get(
                            "fullName",
                            "Unknown",
                        )
                    ),

                    "throws": get_throwing_hand(
                        home_pitcher
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

    except Exception as exc:

        LOG.error(
            "pybaseball Statcast error for %s: %s",
            pitcher_id,
            exc,
        )

        return pd.DataFrame()

    if df is None or df.empty:

        return pd.DataFrame()

    df = df.copy()

    # --------------------------------------------------------
    # Normalize important columns
    # --------------------------------------------------------

    for column in [
        "pitcher",
        "game_pk",
        "at_bat_number",
        "pitch_number",
        "inning",
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

    # Safety filter.
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

    value = str(
        event
    ).strip().lower()

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

    if not valid_event(
        event
    ):

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

    if df is None or df.empty:

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
    # Sort so final pitch of each PA is retained.
    # --------------------------------------------------------

    sort_columns = []

    for column in [
        "game_pk",
        "at_bat_number",
        "pitch_number",
    ]:

        if column in pa.columns:

            sort_columns.append(
                column
            )

    if sort_columns:

        pa = pa.sort_values(
            sort_columns,
            na_position="last",
        )

    # --------------------------------------------------------
    # One row per plate appearance.
    # --------------------------------------------------------

    if {
        "game_pk",
        "at_bat_number",
    }.issubset(pa.columns):

        pa = (
            pa.drop_duplicates(
                [
                    "game_pk",
                    "at_bat_number",
                ],
                keep="last",
            )
        )

    return pa


# ============================================================
# BATTING / PITCHING STATISTICS
# ============================================================

def calculate_batting_stats(
    df: pd.DataFrame,
) -> dict:

    pa = plate_appearance_rows(
        df
    )

    empty_result = {
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

    if pa.empty:

        return empty_result

    events = (
        pa["events"]
        .astype(str)
        .str.lower()
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

    # Sacrifice flies / bunts are AB exceptions.
    sacrifices = int(
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
        - sacrifices
    )

    at_bats = max(
        0,
        at_bats,
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
    # Statcast wOBA
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
    # Statcast xwOBA
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
    "sac_fly": 1,
    "sac_bunt": 1,
    "sac_fly_double_play": 2,
    "fielders_choice": 0,
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

    outs = 0

    for event in events:

        outs += OUT_EVENTS.get(
            event,
            0,
        )

    return outs


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
    Identify starting appearances.

    A Statcast game is considered a start when the
    pitcher's first recorded pitch in that game occurs
    during the first inning.

    Returns newest first.
    """

    if df is None or df.empty:

        return []

    if "pitcher" not in df.columns:

        return []

    pitcher_df = df[
        pd.to_numeric(
            df["pitcher"],
            errors="coerce",
        )
        == pitcher_id
    ].copy()

    if pitcher_df.empty:

        return []

    required_columns = [
        "game_pk",
        "inning",
        "game_date",
    ]

    missing = [
        column
        for column in required_columns
        if column not in pitcher_df.columns
    ]

    if missing:

        LOG.warning(
            "Missing Statcast columns for %s: %s",
            pitcher_id,
            missing,
        )

        return []

    appearances = []

    for game_pk, game_df in pitcher_df.groupby(
        "game_pk",
        dropna=True,
    ):

        if game_df.empty:

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
                sort_columns,
                na_position="last",
            )

        first_inning = safe_int(
            game_df[
                "inning"
            ].iloc[0],
            default=99,
        )

        if first_inning != 1:

            continue

        game_date = game_df[
            "game_date"
        ].iloc[0]

        appearances.append(
            {
                "game_pk": safe_int(
                    game_pk
                ),
                "game_date": game_date,
                "data": game_df,
            }
        )

    appearances.sort(
        key=lambda item: (
            pd.Timestamp(
                item["game_date"]
            )
            if pd.notna(
                item["game_date"]
            )
            else pd.Timestamp.min
        ),
        reverse=True,
    )

    return appearances


# ============================================================
# HANDEDNESS SPLITS
# ============================================================

def get_batter_hand_splits(
    df: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:

    """
    Split completed plate appearances into:

    LHB = batter_stands == L
    RHB = batter_stands == R

    We filter the original Statcast pitch data first,
    then calculate PA statistics from those rows.
    """

    if df is None or df.empty:

        return (
            pd.DataFrame(),
            pd.DataFrame(),
        )

    if "batter_stands" not in df.columns:

        LOG.warning(
            "Statcast data has no batter_stands column."
        )

        return (
            pd.DataFrame(),
            pd.DataFrame(),
        )

    stands = (
        df["batter_stands"]
        .astype("string")
        .str.upper()
        .str.strip()
    )

    lhb_df = df[
        stands == "L"
    ].copy()

    rhb_df = df[
        stands == "R"
    ].copy()

    return (
        lhb_df,
        rhb_df,
    )


# ============================================================
# DISCORD EMBED
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

    # --------------------------------------------------------
    # LHB / RHB
    # --------------------------------------------------------

    lhb_df, rhb_df = (
        get_batter_hand_splits(
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
            f"IP {report['ip_display']} | "
            f"H {report['h']} | "
            f"HR {report['hr']} | "
            f"BB {report['bb']} | "
            f"K {report['k']}"
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
            f"HR **{report['hr']}**\n"
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
        f"**{starter['team']}** @ "
        f"**{starter['opponent']}**\n"
        f"{starter['venue']}\n"
        f"Throws **{starter.get('throws', '?')}**\n\n"

        f"### Last 3 Starts\n"
        f"{starts_text}\n\n"

        f"### Last 3 Starts — Combined\n"
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
                "Baseball Savant Statcast via pybaseball"
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
# DISCORD
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

    """
    Stable duplicate key.

    Example:

    2026-08-17:777123:695076
    """

    game_date = str(
        starter.get(
            "game_date",
            ""
        )
    )[:10]

    game_pk = starter.get(
        "game_pk"
    )

    pitcher_id = starter.get(
        "pitcher_id"
    )

    return (
        f"{game_date}:"
        f"{game_pk}:"
        f"{pitcher_id}"
    )


# ============================================================
# PROCESS ONE PITCHER
# ============================================================

def process_starter(
    starter: dict,
    lookback_days: int,
) -> tuple[pd.DataFrame, list[dict]]:

    pitcher_name = starter[
        "pitcher_name"
    ]

    pitcher_id = starter[
        "pitcher_id"
    ]

    LOG.info(
        "%s: throws %s",
        pitcher_name,
        starter.get(
            "throws",
            "?",
        ),
    )

    df = download_statcast(
        pitcher_id,
        lookback_days,
    )

    if df.empty:

        raise RuntimeError(
            f"No Statcast data for {pitcher_name}"
        )

    starts = find_starting_appearances(
        df,
        pitcher_id,
    )

    if not starts:

        raise RuntimeError(
            f"No starts identified for {pitcher_name}"
        )

    starts = starts[
        :DEFAULT_STARTS
    ]

    LOG.info(
        "%s: found %d recent starts",
        pitcher_name,
        len(starts),
    )

    return (
        df,
        starts,
    )


# ============================================================
# MAIN RUN
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

        alert_key = make_alert_key(
            starter
        )

        LOG.info(
            "Alert key for %s: %s",
            pitcher_name,
            alert_key,
        )

        # ----------------------------------------------------
        # DUPLICATE CHECK
        # ----------------------------------------------------

        if (
            alert_key
            in state["posted"]
        ):

            LOG.info(
                "SKIP duplicate: %s",
                pitcher_name,
            )

            continue

        try:

            # ------------------------------------------------
            # Statcast
            # ------------------------------------------------

            _, starts = process_starter(
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

                print()
                print("=" * 80)
                print(
                    pitcher_name
                )
                print("=" * 80)

                print(
                    json.dumps(
                        payload,
                        indent=2,
                    )
                )

                print("=" * 80)
                print()

                # IMPORTANT:
                #
                # Do NOT save the alert as posted.
                #
                # This means you can run:
                #
                # python statcast_alert.py --dry-run
                #
                # repeatedly without consuming
                # your duplicate state.

                continue

            # ------------------------------------------------
            # Discord
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
            # ONLY mark posted AFTER Discord succeeds.
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

            LOG.info(
                "Completed: %s",
                pitcher_name,
            )

            # Small delay.
            time.sleep(1)

        except Exception as exc:

            LOG.exception(
                "ERROR processing %s: %s",
                pitcher_name,
                exc,
            )

    LOG.info(
        "Run complete. State contains %d posted alerts.",
        len(
            state.get(
                "posted",
                {}
            )
        ),
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
            "posting to Discord. "
            "Dry runs do not modify duplicate state."
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
            "history to download."
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
