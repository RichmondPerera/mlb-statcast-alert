#!/usr/bin/env python3

"""
MLB Statcast Probable Starter Alert

Data rules:
- MLB Stats API: schedule + probable pitcher identification only
- Baseball Savant via pybaseball: all pitching statistics
- No FanGraphs pitching stats
- No Baseball Reference pitching stats
- No MLB pitching-stat endpoints

Features:
- Today's probable MLB starters
- Last 3 starting appearances
- HR allowed
- LHB vs RHB splits
- K, BB, H, HR
- AVG, SLG, wOBA, xwOBA
- K%
- BB%
- HR/9
- MLB player headshot
- Discord webhook
- Persistent duplicate protection
- --dry-run testing mode
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
from pybaseball import statcast


# ============================================================
# CONFIGURATION
# ============================================================

ROOT = Path(__file__).resolve().parent

DATA_DIR = ROOT / "data"
STATE_FILE = DATA_DIR / "discord_state.json"

MLB_API = "https://statsapi.mlb.com/api/v1"

USER_AGENT = "MLB-Statcast-Alert/2.0"

DEFAULT_LOOKBACK_DAYS = 45

DEFAULT_TIMEOUT = 45

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

    return f"{value:.1f}"


def format_ip(
    outs: int,
) -> str:

    """
    Baseball innings notation.

    0 outs = 0.0
    1 out  = 0.1
    2 outs = 0.2
    3 outs = 1.0
    """

    outs = max(0, int(outs))

    innings = outs // 3
    remainder = outs % 3

    return f"{innings}.{remainder}"


# ============================================================
# STATE / DUPLICATE PROTECTION
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

    for key, record in posted.items():

        try:

            posted_date = datetime.fromisoformat(
                record["posted_at"]
            ).date()

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


# ============================================================
# HTTP HELPERS
# ============================================================

def get_json(
    url: str,
    params: dict | None = None,
    retries: int = 4,
) -> dict:

    last_error = None

    for attempt in range(
        1,
        retries + 1,
    ):

        try:

            response = requests.get(
                url,
                params=params,
                timeout=DEFAULT_TIMEOUT,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json",
                },
            )

            # Rate limit
            if response.status_code == 429:

                retry_after = response.headers.get(
                    "Retry-After"
                )

                if retry_after:

                    delay = max(
                        1,
                        safe_int(
                            retry_after,
                            3,
                        ),
                    )

                else:

                    delay = min(
                        2 ** attempt,
                        20,
                    )

                LOG.warning(
                    "HTTP 429 from %s. "
                    "Sleeping %s seconds.",
                    url,
                    delay,
                )

                time.sleep(delay)

                continue

            response.raise_for_status()

            return response.json()

        except requests.RequestException as exc:

            last_error = exc

            if attempt >= retries:
                break

            delay = min(
                2 ** attempt,
                10,
            )

            LOG.warning(
                "Request failed "
                "(attempt %d/%d): %s",
                attempt,
                retries,
                exc,
            )

            time.sleep(delay)

    raise RuntimeError(
        f"Unable to retrieve {url}: {last_error}"
    )


# ============================================================
# MLB SCHEDULE
# ============================================================

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
                []
            )
        )

    return games


# ============================================================
# PROBABLE STARTERS
# ============================================================

def get_pitcher_hand(
    pitcher: dict,
) -> str:

    """
    Extract probable pitcher's throwing hand
    from the hydrated MLB schedule object.
    """

    pitch_hand = pitcher.get(
        "pitchHand"
    )

    if isinstance(
        pitch_hand,
        dict,
    ):

        code = pitch_hand.get(
            "code"
        )

        if code:

            code = str(
                code
            ).upper()

            if code in {
                "L",
                "R",
            }:

                return code

        description = pitch_hand.get(
            "description"
        )

        if description:

            description = str(
                description
            ).lower()

            if "left" in description:
                return "L"

            if "right" in description:
                return "R"

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
            .get(
                "venue",
                {},
            )
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
            .get(
                "team",
                {},
            )
            .get(
                "name",
                "Away",
            )
        )

        home_team = (
            home
            .get(
                "team",
                {},
            )
            .get(
                "name",
                "Home",
            )
        )

        # ----------------------------------------------------
        # Away starter
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
                    "away_team": away_team,
                    "home_team": home_team,
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
                    "throws": get_pitcher_hand(
                        away_pitcher
                    ),
                }
            )

        # ----------------------------------------------------
        # Home starter
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
                    "away_team": away_team,
                    "home_team": home_team,
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
                    "throws": get_pitcher_hand(
                        home_pitcher
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

    """
    Download Statcast for the historical period.

    We intentionally stop at yesterday so today's
    game cannot accidentally contaminate the pregame
    historical report.
    """

    end_date = (
        date.today()
        - timedelta(days=1)
    )

    start_date = (
        end_date
        - timedelta(
            days=lookback_days
        )
    )

    LOG.info(
        "Downloading Statcast for pitcher %s: "
        "%s -> %s",
        pitcher_id,
        start_date,
        end_date,
    )

    try:

        df = statcast(
            start_dt=start_date.isoformat(),
            end_dt=end_date.isoformat(),
            verbose=False,
            parallel=True,
        )

    except TypeError:

        # Compatibility with older pybaseball versions.
        df = statcast(
            start_dt=start_date.isoformat(),
            end_dt=end_date.isoformat(),
        )

    if df is None or df.empty:

        return pd.DataFrame()

    df = df.copy()

    # --------------------------------------------------------
    # Required columns
    # --------------------------------------------------------

    required = {
        "pitcher",
        "game_date",
        "game_pk",
        "at_bat_number",
        "pitch_number",
        "inning",
        "events",
    }

    missing = [
        column
        for column in required
        if column not in df.columns
    ]

    if missing:

        raise RuntimeError(
            "Statcast response is missing "
            f"required columns: {missing}"
        )

    # --------------------------------------------------------
    # Filter pitcher
    # --------------------------------------------------------

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
    # Normalize columns
    # --------------------------------------------------------

    df["game_date"] = pd.to_datetime(
        df["game_date"],
        errors="coerce",
    )

    df["game_pk"] = pd.to_numeric(
        df["game_pk"],
        errors="coerce",
    )

    df["at_bat_number"] = pd.to_numeric(
        df["at_bat_number"],
        errors="coerce",
    )

    df["pitch_number"] = pd.to_numeric(
        df["pitch_number"],
        errors="coerce",
    )

    df["inning"] = pd.to_numeric(
        df["inning"],
        errors="coerce",
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

    value = str(
        event
    ).strip().lower()

    return value in {
        str(name).lower()
        for name in names
    }


def event_count(
    df: pd.DataFrame,
    *events: str,
) -> int:

    if df.empty:

        return 0

    if "events" not in df.columns:

        return 0

    return int(
        df["events"]
        .apply(
            lambda value:
            event_is(
                value,
                *events,
            )
        )
        .sum()
    )


# ============================================================
# PLATE APPEARANCES
# ============================================================

def plate_appearance_rows(
    df: pd.DataFrame,
) -> pd.DataFrame:

    """
    Convert pitch-level Statcast into one row per
    completed plate appearance.

    The final pitch of each PA contains the completed
    event such as single, walk, strikeout, etc.
    """

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

    required_sort_columns = {
        "game_pk",
        "at_bat_number",
        "pitch_number",
    }

    if required_sort_columns.issubset(
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
# BATTING EVENT STATISTICS
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
        .astype("string")
        .str.lower()
        .fillna("")
    )

    # --------------------------------------------------------
    # Hits
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # Walks
    # --------------------------------------------------------

    walks = int(
        events.isin(
            [
                "walk",
                "intent_walk",
            ]
        ).sum()
    )

    # --------------------------------------------------------
    # HBP
    # --------------------------------------------------------

    hbp = int(
        (
            events
            == "hit_by_pitch"
        ).sum()
    )

    # --------------------------------------------------------
    # Strikeouts
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # At bats
    #
    # For this report we remove BB and HBP.
    # Sacrifice situations are also excluded from AB.
    # --------------------------------------------------------

    sacrifice_events = {
        "sac_fly",
        "sac_bunt",
        "catcher_interf",
    }

    sacrifices = int(
        events.isin(
            sacrifice_events
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

    # --------------------------------------------------------
    # Total bases
    # --------------------------------------------------------

    total_bases = (
        singles
        + (2 * doubles)
        + (3 * triples)
        + (4 * home_runs)
    )

    # --------------------------------------------------------
    # AVG
    # --------------------------------------------------------

    avg = None

    if at_bats > 0:

        avg = (
            hits
            / at_bats
        )

    # --------------------------------------------------------
    # SLG
    # --------------------------------------------------------

    slg = None

    if at_bats > 0:

        slg = (
            total_bases
            / at_bats
        )

    # --------------------------------------------------------
    # wOBA
    #
    # Baseball Savant provides woba_value at the
    # plate-appearance level.
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
        .fillna("")
    )

    outs = 0

    for event in events:

        outs += OUT_EVENTS.get(
            event,
            0,
        )

    return outs


# ============================================================
# FULL PITCHING REPORT
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

    # --------------------------------------------------------
    # K%
    # --------------------------------------------------------

    k_pct = None

    if stats["pa"] > 0:

        k_pct = (
            stats["k"]
            / stats["pa"]
        )

    # --------------------------------------------------------
    # BB%
    # --------------------------------------------------------

    bb_pct = None

    if stats["pa"] > 0:

        bb_pct = (
            stats["bb"]
            / stats["pa"]
        )

    # --------------------------------------------------------
    # HR/9
    # --------------------------------------------------------

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
# IDENTIFY STARTING APPEARANCES
# ============================================================

def find_starting_appearances(
    df: pd.DataFrame,
    pitcher_id: int,
) -> list[dict]:

    """
    Identify starting appearances from Statcast.

    A candidate start is identified when the pitcher:
    - pitched in the game
    - threw his first pitch in the 1st inning

    Results are sorted newest first.
    """

    if df.empty:

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

        first_inning = safe_float(
            game_df[
                "inning"
            ].iloc[0],
            99,
        )

        if first_inning != 1:

            continue

        game_date = game_df[
            "game_date"
        ].iloc[0]

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
        key=lambda item: (
            pd.Timestamp(
                item["game_date"]
            )
        ),
        reverse=True,
    )

    return appearances


# ============================================================
# HANDEDNESS SPLITS
# ============================================================

def get_batter_hand_column(
    df: pd.DataFrame,
) -> str | None:

    """
    pybaseball's Statcast dataframe uses 'stand'
    for batter handedness.

    Some older/custom Statcast data can use
    'batter_stands', so we support both.
    """

    if "stand" in df.columns:

        return "stand"

    if "batter_stands" in df.columns:

        return "batter_stands"

    return None


def get_handedness_splits(
    df: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:

    """
    Return:
        LHB dataframe
        RHB dataframe
    """

    if df.empty:

        return (
            pd.DataFrame(),
            pd.DataFrame(),
        )

    column = get_batter_hand_column(
        df
    )

    if column is None:

        LOG.warning(
            "No batter handedness column found. "
            "Available columns: %s",
            list(df.columns),
        )

        return (
            pd.DataFrame(),
            pd.DataFrame(),
        )

    handedness = (
        df[column]
        .astype("string")
        .str.strip()
        .str.upper()
    )

    lhb_df = df[
        handedness == "L"
    ].copy()

    rhb_df = df[
        handedness == "R"
    ].copy()

    return (
        lhb_df,
        rhb_df,
    )


# ============================================================
# DISCORD FORMATTERS
# ============================================================

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


def make_discord_embed(
    starter: dict,
    starts: list[dict],
) -> dict:

    if not starts:

        raise ValueError(
            "No starting appearances found"
        )

    # --------------------------------------------------------
    # Combine last 3 starts
    # --------------------------------------------------------

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

    lhb_df, rhb_df = get_handedness_splits(
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
    # Matchup
    # --------------------------------------------------------

    matchup = (
        f"**{starter['away_team']}** @ "
        f"**{starter['home_team']}**"
    )

    throws = starter.get(
        "throws",
        "?",
    )

    throws_display = (
        throws
        if throws in {"L", "R"}
        else "?"
    )

    # --------------------------------------------------------
    # Description
    # --------------------------------------------------------

    description = (
        f"{matchup}\n"
        f"{starter['venue']}\n"
        f"Throws **{throws_display}**\n\n"

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
                "Baseball Savant Statcast "
                "via pybaseball"
            )
        },
    }


def make_discord_payload(
    starter: dict,
    starts: list[dict],
) -> dict:

    embed = make_discord_embed(
        starter,
        starts,
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
        starter.get(
            "game_date",
            ""
        )
    )[:10]

    return (
        f"{game_date}:"
        f"{starter['game_pk']}:"
        f"{starter['pitcher_id']}"
    )


# ============================================================
# DEBUG STATCAST COLUMNS
# ============================================================

def log_statcast_columns(
    df: pd.DataFrame,
) -> None:

    """
    Useful when Baseball Savant changes or pybaseball
    returns a different schema.
    """

    important = [
        "pitcher",
        "events",
        "stand",
        "batter_stands",
        "woba_value",
        "estimated_woba_using_speedangle",
        "game_pk",
        "at_bat_number",
        "pitch_number",
        "inning",
    ]

    available = [
        column
        for column in important
        if column in df.columns
    ]

    LOG.info(
        "Important Statcast columns available: %s",
        available,
    )


# ============================================================
# MAIN PROCESS
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

            continue

        try:

            # ------------------------------------------------
            # Download Statcast
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

            log_statcast_columns(
                df
            )

            # ------------------------------------------------
            # Find starts
            # ------------------------------------------------

            starts = find_starting_appearances(
                df,
                pitcher_id,
            )

            if not starts:

                LOG.warning(
                    "No starts identified: %s",
                    pitcher_name,
                )

                continue

            # Last 3
            starts = starts[:3]

            LOG.info(
                "%s: found %d recent starts",
                pitcher_name,
                len(starts),
            )

            # ------------------------------------------------
            # Build Discord payload
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
                    "=" * 100
                )

                print(
                    pitcher_name
                )

                print(
                    "=" * 100
                )

                print(
                    json.dumps(
                        payload,
                        indent=2,
                    )
                )

                print(
                    "=" * 100
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

                # Avoid hammering Discord.
                time.sleep(1)

            # ------------------------------------------------
            # Save duplicate state
            #
            # Important:
            # We save this after a successful dry run or
            # successful Discord post.
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
                "pitcher_id": pitcher_id,
                "pitcher_name": pitcher_name,
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
