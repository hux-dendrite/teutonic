#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os

import psycopg
from psycopg.rows import dict_row

from teutonic.evaluation import EarlyStoppingPolicy


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required environment variable {name}")
    return value


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description="Update one competition's live early-stopping policy"
    )
    enabled = value.add_mutually_exclusive_group()
    enabled.add_argument("--enabled", dest="enabled", action="store_true")
    enabled.add_argument("--disabled", dest="enabled", action="store_false")
    value.set_defaults(enabled=None)
    value.add_argument("--min-fraction", type=float)
    value.add_argument("--advantage-quantile", type=float)
    value.add_argument("--margin", type=float)
    value.add_argument("--check-interval", type=int)
    return value


def main() -> int:
    args = parser().parse_args()
    database_url = required("TEUTONIC_DATABASE_URL")
    scope = (
        int(required("TEUTONIC_NETUID")),
        required("TEUTONIC_CHAIN_GENERATION"),
        required("TEUTONIC_COMPETITION"),
    )
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        row = connection.execute(
            """
            SELECT p.*, config.eval_n
              FROM control_plane.competitions c
              JOIN control_plane.evaluation_configs config
                ON config.competition_id = c.competition_id AND config.active
              LEFT JOIN control_plane.evaluation_early_stopping_policies p
                ON p.competition_id = c.competition_id
             WHERE c.netuid = %s AND c.chain_generation = %s AND c.name = %s
             FOR UPDATE OF c
            """,
            scope,
        ).fetchone()
        if row is None:
            raise RuntimeError("configured competition does not exist")
        current = EarlyStoppingPolicy(
            enabled=True if row["enabled"] is None else bool(row["enabled"]),
            min_fraction=0.4 if row["min_fraction"] is None else float(row["min_fraction"]),
            advantage_quantile=(
                0.95
                if row["advantage_quantile"] is None
                else float(row["advantage_quantile"])
            ),
            margin=0.0 if row["margin"] is None else float(row["margin"]),
            check_interval=(
                100 if row["check_interval"] is None else int(row["check_interval"])
            ),
        )
        policy = EarlyStoppingPolicy(
            enabled=current.enabled if args.enabled is None else args.enabled,
            min_fraction=(
                current.min_fraction if args.min_fraction is None else args.min_fraction
            ),
            advantage_quantile=(
                current.advantage_quantile
                if args.advantage_quantile is None
                else args.advantage_quantile
            ),
            margin=current.margin if args.margin is None else args.margin,
            check_interval=(
                current.check_interval
                if args.check_interval is None
                else args.check_interval
            ),
        )
        if policy.enabled and policy.check_interval > int(row["eval_n"]):
            raise ValueError(
                "early stopping check_interval cannot exceed the active evaluation sample count"
            )
        connection.execute(
            """
            INSERT INTO control_plane.evaluation_early_stopping_policies (
                competition_id, enabled, min_fraction, advantage_quantile,
                margin, check_interval
            )
            SELECT competition_id, %s, %s, %s, %s, %s
              FROM control_plane.competitions
             WHERE netuid = %s AND chain_generation = %s AND name = %s
            ON CONFLICT (competition_id) DO UPDATE SET
                enabled = EXCLUDED.enabled,
                min_fraction = EXCLUDED.min_fraction,
                advantage_quantile = EXCLUDED.advantage_quantile,
                margin = EXCLUDED.margin,
                check_interval = EXCLUDED.check_interval,
                updated_at = clock_timestamp()
            """,
            (
                policy.enabled,
                policy.min_fraction,
                policy.advantage_quantile,
                policy.margin,
                policy.check_interval,
                *scope,
            ),
        )
    print(
        "early stopping updated: "
        f"enabled={str(policy.enabled).lower()} "
        f"min_fraction={policy.min_fraction} "
        f"advantage_quantile={policy.advantage_quantile} "
        f"margin={policy.margin} "
        f"check_interval={policy.check_interval}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
