#!/usr/bin/env python3
"""Issue Tracker Tasks Dump

Replacement for FME Workbench HD_BaseMap_Tasks_25092025.fmw.
Connects to the PostGIS issue_tracker database, queries issues
for a given project label (and optionally a spatial WKT filter),
splits features by geometry type, and writes to a zipped GeoPackage.

Usage:
    python issue_tracker_dump.py --project-name SWE_Sample_Motorways_ARC1 --output output.zip
    python issue_tracker_dump.py --project-name SWE_Sample_Motorways_ARC1 --output output.zip --wkt-file area.wkt

Environment variables:
    ISSUE_TRACKER_SECRET - Full DB connection details from Azure Key Vault
                           (secret: kv-issue-tracker in kv-adp-hd-contrib).
                           Accepted formats:
                             - JSON:       {"host":"...","port":5432,"user":"...","password":"...","dbname":"..."}
                             - Conn string: postgresql://user:pass@host:port/dbname
                             - DSN:         host=... port=... dbname=... user=... password=...
"""

import argparse
import logging
import os
import sys
import tempfile
import zipfile
from pathlib import Path

import json
from urllib.parse import urlparse, parse_qs, unquote

import geopandas as gpd
import psycopg2

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Database connection — credentials from Azure Key Vault secret
# ---------------------------------------------------------------------------


def _parse_secret(raw: str) -> dict:
    """Parse the ISSUE_TRACKER_SECRET env var.

    Supports three formats stored in Azure Key Vault:
      1. JSON   {"host":"…","port":5432,"user":"…","password":"…","dbname":"…"}
      2. URI    postgresql://user:pass@host:port/dbname?sslmode=prefer
      3. DSN    host=… port=… dbname=… user=… password=…
    Returns a dict with keys: host, port, dbname, user, password, sslmode.
    """
    raw = raw.strip()

    # --- 1. Try JSON ---------------------------------------------------------
    if raw.startswith("{"):
        obj = json.loads(raw)
        # Normalise common key aliases
        return {
            "host":     obj.get("host") or obj.get("hostname") or obj.get("server"),
            "port":     int(obj.get("port", 5432)),
            "dbname":   obj.get("dbname") or obj.get("database") or obj.get("db"),
            "user":     obj.get("user") or obj.get("username"),
            "password": obj.get("password"),
            "sslmode":  obj.get("sslmode", "prefer"),
        }

    # --- 2. Try URI (postgresql://…) -----------------------------------------
    if raw.startswith("postgres"):
        p = urlparse(raw)
        return {
            "host":     p.hostname,
            "port":     int(p.port or 5432),
            "dbname":   p.path.lstrip("/"),
            "user":     unquote(p.username or ""),
            "password": unquote(p.password or ""),
            "sslmode":  parse_qs(p.query).get("sslmode", ["prefer"])[0],
        }

    # --- 3. Try DSN key=value pairs ------------------------------------------
    #     host=x port=y dbname=z user=u password=p
    if "=" in raw:
        pairs = {}
        for token in raw.split():
            if "=" in token:
                k, v = token.split("=", 1)
                pairs[k.strip()] = v.strip()
        return {
            "host":     pairs.get("host"),
            "port":     int(pairs.get("port", 5432)),
            "dbname":   pairs.get("dbname") or pairs.get("database"),
            "user":     pairs.get("user"),
            "password": pairs.get("password"),
            "sslmode":  pairs.get("sslmode", "prefer"),
        }

    raise ValueError(
        "Cannot parse ISSUE_TRACKER_SECRET. "
        "Expected JSON, postgresql:// URI, or DSN key=value string."
    )


def get_connection():
    """Create a psycopg2 connection from the Azure Key Vault secret."""
    secret = os.environ.get("ISSUE_TRACKER_SECRET", "")
    if not secret:
        raise EnvironmentError(
            "ISSUE_TRACKER_SECRET environment variable is not set. "
            "It should be populated from Azure Key Vault (kv-adp-hd-contrib / kv-issue-tracker)."
        )
    cfg = _parse_secret(secret)
    log.info("Connecting to %s:%s/%s as %s", cfg["host"], cfg["port"], cfg["dbname"], cfg["user"])
    return psycopg2.connect(
        host=cfg["host"],
        port=cfg["port"],
        dbname=cfg["dbname"],
        user=cfg["user"],
        password=cfg["password"],
        sslmode=cfg.get("sslmode", "prefer"),
    )


# ---------------------------------------------------------------------------
# SQL queries (ported from FME workspace)
# ---------------------------------------------------------------------------
SQL_BY_LABEL = """
WITH
    tasks_tracker AS (
        SELECT
            i.id        AS issue,
            i.type      AS issue_category,
            i.details   AS details,
            i.updated_at,
            array_agg(l.label ORDER BY l.id) AS labels,
            i.status,
            i.geometry
        FROM issue_tracker.issues  i
        JOIN issue_tracker.labels  l ON i.id = l.issue
        WHERE l.label IN (%(project_name)s)
        GROUP BY 1, 2, i.details, i.updated_at, i.status, i.geometry
    ),
    issues_stages AS (
        SELECT
            tt.issue            AS task_id,
            tt.issue_category,
            tt.details,
            tt.status,
            tt.updated_at,
            p.name              AS issue_tracker_project_name,
            tt.labels[1]        AS project_from_label,
            tt.labels           AS all_labels,
            CASE
                WHEN tt.status = 1
                THEN array_prepend(tt.status,
                        array_remove(array_agg(ps.id ORDER BY ps.id), NULL))
                ELSE array_agg(ps.id ORDER BY ps.id)
            END AS stage_all_ids,
            CASE
                WHEN tt.status = 1
                THEN array_prepend('NEW',
                        array_remove(
                            array_agg(ps.name || '-' || s.name ORDER BY ps.id),
                            NULL))
                ELSE array_agg(ps.name || '-' || s.name ORDER BY ps.id)
            END AS stage_all_names,
            tt.geometry
        FROM tasks_tracker tt
        LEFT JOIN issue_tracker.issue_statuses ist ON ist.issue   = tt.issue
        LEFT JOIN issue_tracker.project_stages  ps ON ist.stage   = ps.id
        LEFT JOIN issue_tracker.projects         p ON p.id        = ps.project
        LEFT JOIN issue_tracker.statuses         s ON ist.status  = s.id
        GROUP BY
            tt.issue, tt.issue_category, tt.details,
            p.name, tt.labels[1], tt.labels,
            tt.status, tt.updated_at, tt.geometry
    )
SELECT
    task_id,
    issue_category,
    TO_CHAR(updated_at, 'YYYY-MM-DD') AS updated_at,
    project_from_label,
    stage_all_names[array_length(stage_all_names, 1)] AS current_stage_name,
    details,
    geometry
FROM issues_stages;
"""

SQL_BY_SPATIAL = """
WITH
    tasks_tracker AS (
        SELECT
            i.id        AS issue,
            i.type      AS issue_category,
            i.updated_at,
            i.details   AS details,
            array_agg(l.label ORDER BY l.id) AS labels,
            i.status,
            i.geometry
        FROM issue_tracker.issues  i
        JOIN issue_tracker.labels  l ON i.id = l.issue
        WHERE ST_Intersects(i.geometry, ST_GeomFromText(%(wkt_text)s, 4326)) = TRUE
        GROUP BY 1, 2, i.updated_at, i.details, i.status, i.geometry
    ),
    issues_stages AS (
        SELECT
            tt.issue            AS task_id,
            tt.issue_category,
            tt.details,
            tt.status,
            tt.updated_at,
            p.name              AS issue_tracker_project_name,
            tt.labels[1]        AS project_from_label,
            tt.labels           AS all_labels,
            CASE
                WHEN tt.status = 1
                THEN array_prepend(tt.status,
                        array_remove(array_agg(ps.id ORDER BY ps.id), NULL))
                ELSE array_agg(ps.id ORDER BY ps.id)
            END AS stage_all_ids,
            CASE
                WHEN tt.status = 1
                THEN array_prepend('NEW',
                        array_remove(
                            array_agg(ps.name || '-' || s.name ORDER BY ps.id),
                            NULL))
                ELSE array_agg(ps.name || '-' || s.name ORDER BY ps.id)
            END AS stage_all_names,
            tt.geometry
        FROM tasks_tracker tt
        LEFT JOIN issue_tracker.issue_statuses ist ON ist.issue   = tt.issue
        LEFT JOIN issue_tracker.project_stages  ps ON ist.stage   = ps.id
        LEFT JOIN issue_tracker.projects         p ON p.id        = ps.project
        LEFT JOIN issue_tracker.statuses         s ON ist.status  = s.id
        GROUP BY
            tt.issue, tt.issue_category, tt.details,
            p.name, tt.labels[1], tt.labels,
            tt.status, tt.updated_at, tt.geometry
    )
SELECT
    task_id,
    issue_category,
    TO_CHAR(updated_at, 'YYYY-MM-DD') AS updated_at,
    project_from_label,
    stage_all_names[array_length(stage_all_names, 1)] AS current_stage_name,
    details,
    geometry
FROM issues_stages;
"""


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------
def fetch_issues(conn, project_name: str, wkt_file: str | None) -> gpd.GeoDataFrame:
    """Fetch issues from PostGIS and return a GeoDataFrame."""
    if wkt_file:
        wkt_text = Path(wkt_file).read_text().strip()
        log.info("Using spatial filter from WKT file: %s", wkt_file)
        sql = SQL_BY_SPATIAL
        params = {"wkt_text": wkt_text}
    else:
        log.info("Using label filter: %s", project_name)
        sql = SQL_BY_LABEL
        params = {"project_name": project_name}

    log.info("Executing query against issue_tracker database...")
    gdf = gpd.read_postgis(
        sql,
        conn,
        geom_col="geometry",
        params=params,
    )
    log.info("Fetched %d features", len(gdf))
    return gdf


def split_by_geometry_type(gdf: gpd.GeoDataFrame) -> dict[str, gpd.GeoDataFrame]:
    """Split GeoDataFrame by geometry type, mimicking FME GeometryFilter.

    Returns a dict mapping layer name suffix to GeoDataFrame.
    E.g. 'polygon', 'line', 'point'.
    """
    if gdf.empty:
        return {}

    gdf = gdf.copy()
    gdf["_geom_type"] = gdf.geometry.geom_type

    type_map = {
        "Polygon": "polygon",
        "MultiPolygon": "polygon",
        "LineString": "line",
        "MultiLineString": "line",
        "Point": "point",
        "MultiPoint": "point",
    }
    gdf["_layer"] = gdf["_geom_type"].map(type_map).fillna("other")

    result = {}
    for layer_name, group in gdf.groupby("_layer"):
        subset = group.drop(columns=["_geom_type", "_layer"]).copy()
        result[layer_name] = subset
        log.info("  Layer '%s': %d features", layer_name, len(subset))

    return result


def write_geopackage(
    layers: dict[str, gpd.GeoDataFrame],
    project_name: str,
    output_path: str,
) -> None:
    """Write layers to a zipped GeoPackage.

    Mimics FME fanout: FNR_Tasks_{ProjectName}.gpkg inside a zip.
    """
    gpkg_name = f"FNR_Tasks_{project_name}.gpkg"

    with tempfile.TemporaryDirectory() as tmpdir:
        gpkg_path = os.path.join(tmpdir, gpkg_name)

        for layer_suffix, gdf in layers.items():
            if gdf.empty:
                continue
            # Build layer name from issue_category + geometry type
            # Group by issue_category to create separate layers
            for cat, cat_gdf in gdf.groupby("issue_category"):
                layer = f"{cat}_{layer_suffix}"
                cat_gdf.to_file(gpkg_path, layer=layer, driver="GPKG")
                log.info(
                    "  Wrote layer '%s': %d features", layer, len(cat_gdf)
                )

        # Zip the GeoPackage
        log.info("Zipping to %s ...", output_path)
        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(gpkg_path, gpkg_name)

    log.info("Output written: %s", output_path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args():
    parser = argparse.ArgumentParser(
        description="Dump issue tracker tasks to a zipped GeoPackage."
    )
    parser.add_argument(
        "--project-name",
        required=True,
        help="Project label to filter on (e.g. SWE_Sample_Motorways_ARC1)",
    )
    parser.add_argument(
        "--output",
        default="output.zip",
        help="Output zip file path (default: output.zip)",
    )
    parser.add_argument(
        "--wkt-file",
        default=None,
        help="Optional WKT file for spatial filtering (replaces label filter)",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    log.info("=" * 60)
    log.info("Issue Tracker Tasks Dump")
    log.info("Project: %s", args.project_name)
    log.info("Output:  %s", args.output)
    if args.wkt_file:
        log.info("WKT:     %s", args.wkt_file)
    log.info("=" * 60)

    try:
        conn = get_connection()
    except (EnvironmentError, ValueError) as exc:
        log.error(str(exc))
        sys.exit(1)

    try:
        gdf = fetch_issues(conn, args.project_name, args.wkt_file)

        if gdf.empty:
            log.warning("No features found. Exiting.")
            sys.exit(0)

        # Set CRS to EPSG:4326 (same as FME source)
        gdf = gdf.set_crs(epsg=4326, allow_override=True)

        log.info("Splitting features by geometry type...")
        layers = split_by_geometry_type(gdf)

        write_geopackage(layers, args.project_name, args.output)

        log.info("Translation was SUCCESSFUL (%d features output)", len(gdf))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
