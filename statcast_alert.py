#!/usr/bin/env python3
"""
MLB Statcast Probable Starter Discord Alert

Pipeline:
    MLB Stats API -> today's probable starters
    pybaseball.statcast_pitcher() -> pitcher Statcast
    Statcast -> last 3 actual starts
    Statcast -> pitching/contact/split report
    Discord webhook -> formatted alert

Features:
    - Adaptive 60 / 120 / 240 / 365 day search
    - Last 3 actual starts
    - IP / H / HR / BB / K per start
    - Combined K% / BB% / HR/9
    - AVG / SLG / wOBA / xwOBA / xBA / xSLG
    - Hard Hit% / Barrel%
    - LHB / RHB splits
    - MLB headshot
    - Game date/time
    - Duplicate protection
    - --dry-run
    - Graceful skip for missing/insufficient Statcast
    - Suppresses pybaseball "Gathering Player Data" output
"""

from __future__ import annotations

import argparse
import contextlib
import io
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
# CONFIG
# ============================================================

ROOT = Path(__file__).resolve().parent

DATA_DIR = ROOT / "data"
STATE_FILE = DATA_DIR / "discord_state.json"

MLB_API = "https://statsapi.mlb.com/api/v1"

ET = ZoneInfo("America/New_York")

USER_AGENT = "MLB-Statcast-Alert/5.0"

REQUEST_TIMEOUT = 45

INITIAL_LOOKBACK_DAYS = 60
MAX_LOOKBACK_DAYS = 365

LOOKBACK_STEPS = [60, 120, 240, 365]

REQUIRED_STARTS = 3

STATE_KEEP_DAYS = 14

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
# TIME / BASIC HELPERS
# ============================================================

def now_et() -> datetime:
    return datetime.now(ET)


def today_et() -> date:
    return now_et().date()


def safe_float(
    value: Any,
    default: float | None = None,
) -> float | None:
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


def normalize(
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


# ============================================================
# STATE
# ============================================================

def empty_state() -> dict:
    return {
        "version": 5,
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
                "State file is not a JSON object"
            )

        if not isinstance(
            state.get("posted"),
            dict,
        ):
            state["posted"] = {}

        state.setdefault(
            "version",
            5,
        )

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
            "MLB API returned unexpected JSON"
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
# PYBASEBALL OUTPUT SUPPRESSION
# ============================================================

@contextlib.contextmanager
def suppress_pybaseball_output():
    stdout = io.StringIO()
    stderr = io.StringIO()

    with contextlib.redirect_stdout(
        stdout
    ), contextlib.redirect_stderr(
        stderr
    ):
        yield


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
        with suppress_pybaseball_output():
            try:
                df = statcast_pitcher(
                    start_dt=start_date.isoformat(),
                    end_dt=end_date.isoformat(),
                    player_id=pitcher_id,
                )

            except TypeError:
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

    if df is None or df.empty:
        LOG.warning(
            "No Statcast data returned for pitcher %s.",
            pitcher_id,
        )

        return pd.DataFrame()

    df = df.copy()

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

    if "game_date" in df.columns:
        df["game_date"] = pd.to_datetime(
            df["game_date"],
            errors="coerce",
        )

    numeric_columns = [
        "game_pk",
        "at_bat_number",
        "pitch_number",
        "inning",
        "outs_when_up",
        "batter",
        "pitcher",
        "launch_speed",
        "launch_angle",
        "estimated_ba_using_speedangle",
        "estimated_slg_using_speedangle",
        "estimated_woba_using_speedangle",
        "woba_value",
    ]

    for column in numeric_columns:
        if column in df.columns:
            df[column] = pd.to_numeric(
                df[column],
                errors="coerce",
            )

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

    return normalize(event) not in {
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
# START IDENTIFICATION
# ============================================================

def find_starting_appearances(
    df: pd.DataFrame,
    pitcher_id: int,
) -> list[dict]:

    if df.empty:
        return []

    required = {
        "game_pk",
        "inning",
    }

    if not required.issubset(
        df.columns
    ):
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


def find_recent_starts(
    pitcher_id: int,
) -> tuple[list[dict], int]:

    for lookback_days in LOOKBACK_STEPS:
        LOG.info(
            "Searching for %d starts using %d-day lookback...",
            REQUIRED_STARTS,
            lookback_days,
        )

        df = download_statcast(
            pitcher_id,
            lookback_days,
        )

        if df.empty:
            continue

        starts = find_starting_appearances(
            df,
            pitcher_id,
        )

        LOG.info(
            "Found %d starts in %d-day window.",
            len(starts),
            lookback_days,
        )

        if len(starts) >= REQUIRED_STARTS:
            return (
                starts[:REQUIRED_STARTS],
                lookback_days,
            )

    return [], MAX_LOOKBACK_DAYS


# ============================================================
# OUT EVENTS
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
    df: pd.DataFrame,
) -> int:
    pa = plate_appearance_rows(
        df
    )

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
# BATTING / CONTACT QUALITY
# ============================================================

AB_EXCLUDED_EVENTS = {
    "walk",
    "intent_walk",
    "hit_by_pitch",
    "sac_fly",
    "sac_bunt",
    "catcher_interf",
}


def calculate_batting_stats(
    df: pd.DataFrame,
) -> dict:
    pa = plate_appearance_rows(
        df
    )

    empty = {
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
        "xba": None,
        "xslg": None,
        "hard_hit_pct": None,
        "barrel_pct": None,
    }

    if pa.empty:
        return empty

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
            [
                "walk",
                "intent_walk",
            ]
        ).sum()
    )

    hbp = int(
        (events == "hit_by_pitch").sum()
    )

    strikeouts = int(
        events.isin(
            [
                "strikeout",
                "strikeout_double_play",
            ]
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

    avg = None

    if at_bats > 0:
        avg = hits / at_bats

    slg = None

    if at_bats > 0:
        slg = total_bases / at_bats

    def mean_column(
        column: str,
    ) -> float | None:
        if column not in pa.columns:
            return None

        values = pd.to_numeric(
            pa[column],
            errors="coerce",
        ).dropna()

        if values.empty:
            return None

        return float(
            values.mean()
        )

    woba = mean_column(
        "woba_value"
    )

    xwoba = mean_column(
        "estimated_woba_using_speedangle"
    )

    xba = mean_column(
        "estimated_ba_using_speedangle"
    )

    xslg = mean_column(
        "estimated_slg_using_speedangle"
    )

    # --------------------------------------------------------
    # Hard-Hit %
    # --------------------------------------------------------

    hard_hit_pct = None

    if "launch_speed" in pa.columns:
        launch_speed = pd.to_numeric(
            pa["launch_speed"],
            errors="coerce",
        )

        launch_speed = launch_speed[
            launch_speed.notna()
        ]

        if not launch_speed.empty:
            hard_hit_pct = float(
                (
                    launch_speed >= 95.0
                ).mean()
            )

    # --------------------------------------------------------
    # Barrel %
    #
    # Calculate barrels directly from exit velocity and
    # launch angle instead of relying on pybaseball's
    # "barrel" column, which can be missing/empty.
    #
    # Statcast barrel baseline:
    #   98 mph -> 26° to 30°
    #
    # Each additional mph above 98 expands the acceptable
    # launch-angle range by 1° on each side.
    #
    # Example:
    #   98 mph -> 26° to 30°
    #   99 mph -> 25° to 31°
    #   100 mph -> 24° to 32°
    # --------------------------------------------------------

    barrel_pct = None

    if {
        "launch_speed",
        "launch_angle",
    }.issubset(pa.columns):

        launch_speed = pd.to_numeric(
            pa["launch_speed"],
            errors="coerce",
        )

        launch_angle = pd.to_numeric(
            pa["launch_angle"],
            errors="coerce",
        )

        batted_ball_mask = (
            launch_speed.notna()
            & launch_angle.notna()
        )

        ev = launch_speed[
            batted_ball_mask
        ]

        la = launch_angle[
            batted_ball_mask
        ]

        if not ev.empty:

            # Barrels require at least 98 mph exit velocity.
            eligible = ev >= 98.0

            # How many mph above the 98 mph barrel baseline.
            mph_above_98 = (
                ev - 98.0
            ).clip(
                lower=0
            )

            # Expand the launch-angle window as EV increases.
            lower_angle = (
                26.0
                - mph_above_98
            )

            upper_angle = (
                30.0
                + mph_above_98
            )

            barrel_mask = (
                eligible
                & (la >= lower_angle)
                & (la <= upper_angle)
            )

            barrel_pct = float(
                barrel_mask.mean()
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
        "avg": avg,
        "slg": slg,
        "woba": woba,
        "xwoba": xwoba,
        "xba": xba,
        "xslg": xslg,
        "hard_hit_pct": hard_hit_pct,
        "barrel_pct": barrel_pct,
    }


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
# LHB / RHB SPLITS
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
# DISCORD REPORT
# ============================================================

def split_text(
    report: dict,
) -> str:
    if report["pa"] == 0:
        return "Plate Appearances: 0"

    return (
        f"Plate Appearances: {report['pa']}\n"
        f"Batting Average: {fmt_avg(report['avg'])}\n"
        f"Slugging Percentage: {fmt_avg(report['slg'])}\n"
        f"Strikeout Rate: {fmt_pct(report['k_pct'])}\n"
        f"Walk Rate: {fmt_pct(report['bb_pct'])}\n"
        f"wOBA: {fmt_avg(report['woba'])}\n"
        f"Expected wOBA: {fmt_avg(report['xwoba'])}"
    )


def make_discord_embed(
    starter: dict,
    starts: list[dict],
) -> dict:
    if len(starts) < REQUIRED_STARTS:
        raise ValueError(
            "Cannot build alert without 3 starts."
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

    lhb_df, rhb_df = get_hand_splits(
        recent_df
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
            f"**{start_date.strftime('%b %d')}**\n"
            f"{report['ip_display']} IP · "
            f"{report['h']} H · "
            f"{report['hr']} HR · "
            f"{report['bb']} BB · "
            f"{report['k']} K"
        )

    starts_text = "\n\n".join(
        start_lines
    )

    # --------------------------------------------------------
    # Game date/time
    # --------------------------------------------------------

    game_date_raw = starter.get(
        "game_date"
    )

    game_datetime = None

    if game_date_raw:
        try:
            game_datetime = pd.Timestamp(
                game_date_raw
            )

            if game_datetime.tzinfo is None:
                game_datetime = game_datetime.tz_localize(
                    "UTC"
                )

            game_datetime = game_datetime.tz_convert(
                "America/New_York"
            )

        except Exception:
            game_datetime = None

    if game_datetime is not None:
        game_time_text = game_datetime.strftime(
            "%b %d, %Y · %-I:%M %p ET"
        )
    else:
        game_time_text = "Game time unavailable"

    # --------------------------------------------------------
    # Discord description
    # --------------------------------------------------------

    description = (
        f"**{starter['team']} @ "
        f"{starter['opponent']}**\n"
        f"{starter['venue']}\n"
        f"**{game_time_text}**\n"
        f"Throws: **{pitcher_hand}**\n\n"

        f"### 📋 LAST 3 STARTS\n"
        f"{starts_text}\n\n"

        f"### 📊 LAST 3 COMBINED\n"
        f"**{overall['ip_display']} IP · "
        f"{overall['h']} H · "
        f"{overall['hr']} HR · "
        f"{overall['bb']} BB · "
        f"{overall['k']} K**\n\n"

        f"**Strikeout Rate:** "
        f"{fmt_pct(overall['k_pct'])}\n"
        f"**Walk Rate:** "
        f"{fmt_pct(overall['bb_pct'])}\n"
        f"**Home Runs per 9 Innings:** "
        f"{fmt_one(overall['hr9'])}\n\n"

        f"### 🎯 CONTACT QUALITY\n"
        f"**Batting Average Against:** "
        f"{fmt_avg(overall['avg'])}\n"
        f"**Slugging Percentage:** "
        f"{fmt_avg(overall['slg'])}\n"
        f"**wOBA:** "
        f"{fmt_avg(overall['woba'])}\n"
        f"**Expected wOBA:** "
        f"{fmt_avg(overall['xwoba'])}\n"
        f"**Expected Batting Average:** "
        f"{fmt_avg(overall['xba'])}\n"
        f"**Expected Slugging Percentage:** "
        f"{fmt_avg(overall['xslg'])}\n"
        f"**Hard-Hit Rate:** "
        f"{fmt_pct(overall['hard_hit_pct'])}\n"
        f"**Barrel Rate:** "
        f"{fmt_pct(overall['barrel_pct'])}\n\n"

        f"### 👈 VS LEFT-HANDED BATTERS\n"
        f"{split_text(lhb)}\n\n"

        f"### 👉 VS RIGHT-HANDED BATTERS\n"
        f"{split_text(rhb)}"
    )

    if len(description) > 4000:
        description = (
            description[:3990]
            + "\n..."
        )

    return {
        "title": (
            f"🔥 {starter['pitcher_name']} "
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
    if not webhook_url:
        raise ValueError(
            "Discord webhook URL is empty"
        )

    response = requests.post(
        webhook_url,
        json=payload,
        timeout=REQUEST_TIMEOUT,
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
) -> tuple[
    list[dict],
    int,
]:
    pitcher_name = starter[
        "pitcher_name"
    ]

    pitcher_id = starter[
        "pitcher_id"
    ]

    starts, lookback_days = (
        find_recent_starts(
            pitcher_id
        )
    )

    if len(starts) < REQUIRED_STARTS:
        raise RuntimeError(
            f"Only found {len(starts)} starts "
            f"for {pitcher_name} after searching "
            f"{MAX_LOOKBACK_DAYS} days."
        )

    LOG.info(
        "%s: using last %d starts "
        "(lookback %d days).",
        pitcher_name,
        len(starts),
        lookback_days,
    )

    return (
        starts,
        lookback_days,
    )


# ============================================================
# DRY RUN
# ============================================================

def print_dry_run(
    starter: dict,
    payload: dict,
) -> None:
    print()
    print("=" * 100)

    print(
        f"{starter['pitcher_name']} | "
        f"{starter['team']} @ "
        f"{starter['opponent']}"
    )

    print("=" * 100)

    print(
        json.dumps(
            payload,
            indent=2,
            ensure_ascii=False,
        )
    )

    print("=" * 100)
    print()


# ============================================================
# RUN
# ============================================================

def run(
    dry_run: bool,
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
        state,
        keep_days=STATE_KEEP_DAYS,
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
    skipped = 0

    for index, starter in enumerate(
        starters,
        1,
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
            # Find 3 actual starts
            # ------------------------------------------------

            starts, lookback_days = (
                process_starter(
                    starter
                )
            )

            # ------------------------------------------------
            # Build report
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
                    "(NOT marked as posted)",
                    pitcher_name,
                )

                successful += 1
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
            # Record only after successful POST
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
                "lookback_days": lookback_days,
            }

            save_state(
                state
            )

            successful += 1

            LOG.info(
                "Completed: %s",
                pitcher_name,
            )

            time.sleep(1)

        except Exception as exc:
            skipped += 1

            LOG.warning(
                "SKIP %s: %s",
                pitcher_name,
                exc,
            )

            continue

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    LOG.info(
        "Run complete | successful=%d | skipped=%d",
        successful,
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

    args = parser.parse_args()

    LOG.info(
        "Initial Statcast lookback: %d days",
        INITIAL_LOOKBACK_DAYS,
    )

    LOG.info(
        "Maximum Statcast lookback: %d days",
        MAX_LOOKBACK_DAYS,
    )

    LOG.info(
        "Required recent starts: %d",
        REQUIRED_STARTS,
    )

    LOG.info(
        "Dry run: %s",
        args.dry_run,
    )

    return run(
        dry_run=args.dry_run,
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    sys.exit(main())
