from os import getenv
from typing import Dict, Union

import asyncpg
from asyncpg import UndefinedFunctionError, UndefinedObjectError, Connection

from openmaptiles.perfutils import COLOR
from openmaptiles.sqltomvt import MvtGenerator
from openmaptiles.utils import coalesce, print_err


async def get_postgis_version(conn: Connection) -> Union[str, None]:
    try:
        return await conn.fetchval("SELECT postgis_version()")
    except (UndefinedFunctionError, UndefinedObjectError) as ex:
        return None


async def show_settings(conn: Connection, verbose=True) -> Dict[str, str]:
    settings = {
        'version()': None,
        'postgis_full_version()': None,
        'jit': lambda
            v: 'disable JIT in PG 11-12 for complex queries' if v != 'off' else '',
        'shared_buffers': None,
        'work_mem': None,
        'maintenance_work_mem': None,
        'max_connections': None,
        'max_worker_processes': None,
        'max_parallel_workers': None,
        'max_parallel_workers_per_gather': None,
    }
    key_len = max((len(v) for v in settings))
    results = {}
    for setting, validator in settings.items():
        q = f"{'SELECT' if '(' in setting else 'SHOW'} {setting};"
        prefix = ''
        suffix = ''
        try:
            res = await conn.fetchval(q)
            if validator:
                msg = validator(res)
                if msg:
                    prefix, suffix = COLOR.RED, f" {msg}{COLOR.RESET}"
            results[setting] = res
        except (UndefinedFunctionError, UndefinedObjectError) as ex:
            res = ex.message
            prefix, suffix = COLOR.RED, COLOR.RESET
            results[setting] = None
        if verbose:
            print(f"* {setting:{key_len}} = {prefix}{res}{suffix}")

    return results


def parse_pg_args(args):
    pghost = coalesce(
        args.get("--pghost"), getenv('POSTGRES_HOST'), getenv('PGHOST'),
        'localhost')
    pgport = coalesce(
        args.get("--pgport"), getenv('POSTGRES_PORT'), getenv('PGPORT'),
        '5432')
    dbname = coalesce(
        args.get("--dbname"), getenv('POSTGRES_DB'), getenv('PGDATABASE'),
        'openmaptiles')
    user = coalesce(
        args.get("--user"), getenv('POSTGRES_USER'), getenv('PGUSER'),
        'openmaptiles')
    password = coalesce(
        args.get("--password"), getenv('POSTGRES_PASSWORD'),
        getenv('PGPASSWORD'), 'openmaptiles')
    return pghost, pgport, dbname, user, password


class PgWarnings:
    def __init__(self, conn: Connection, delay_printing=False) -> None:
        self.messages = []
        self.delay_printing = delay_printing
        conn.add_log_listener(lambda _, msg: self.on_warning(msg))

    def on_warning(self, msg: asyncpg.PostgresLogMessage):
        if self.delay_printing:
            self.messages.append(msg)
        else:
            self.print_message(msg)

    @staticmethod
    def print_message(msg: asyncpg.PostgresLogMessage):
        try:
            # noinspection PyUnresolvedReferences
            print_err(f"  {msg.severity}: {msg.message} @ {msg.context}")
        except AttributeError:
            print_err(f"  {msg}")

    def print(self):
        for msg in self.messages:
            self.print_message(msg)
        self.messages = []


async def create_metadata(pg_conn: Connection,
                          mvt_generator: MvtGenerator, url_prefix: str):
    """Get mbtiles metadata based on the tileset and PG connection"""
    vector_layers = []
    unknown_fields = {}
    tileset = mvt_generator.tileset
    pg_types = await get_sql_types(pg_conn)
    for layer_id, layer in mvt_generator.get_layers():
        fields = await mvt_generator.validate_layer_fields(pg_conn, layer_id, layer)
        unknown = {
            name: oid
            for name, oid in fields.items() if oid not in pg_types
        }
        if unknown:
            unknown_fields[layer_id] = unknown
        vector_layers.append(dict(
            id=layer.id,
            fields={name: pg_types[type_oid]
                    for name, type_oid in fields.items()
                    if type_oid in pg_types},
            maxzoom=tileset.maxzoom,
            minzoom=tileset.minzoom,
            description=layer.description,
        ))
    if unknown_fields:
        msg = "Some of the layer field(s) have unknown SQL types (OIDs):"
        for lid, fields in unknown_fields.items():
            msg += f"Layer {lid}: "
            msg += ', '.join([f'{n} ({o})' for n, o in fields.items()])
        raise ValueError(msg)

    return dict(
        format="pbf",
        name=tileset.name,
        id=tileset.id,
        bounds=tileset.bounds,
        center=tileset.center,
        maxzoom=tileset.maxzoom,
        minzoom=tileset.minzoom,
        version=tileset.version,
        attribution=tileset.attribution,
        description=tileset.description,
        pixel_scale=tileset.pixel_scale,
        tilejson="2.0.0",
        tiles=[url_prefix + "/{z}/{x}/{y}.pbf"],
        vector_layers=vector_layers,
    )


async def get_sql_types(pg_conn: Connection):
    """
    Get Postgres types that we can handle,
    and return the mapping of OSM type id (oid) => MVT style type
    """
    sql_to_mvt_types = dict(
        bool="Boolean",
        text="String",
        int4="Number",
        int8="Number",
    )
    types = await pg_conn.fetch(
        "select oid, typname from pg_type where typname = ANY($1::text[])",
        list(sql_to_mvt_types.keys())
    )
    return {row['oid']: sql_to_mvt_types[row['typname']] for row in types}


def print_connecting(pghost: str, pgport: str, dbname: str, user: str) -> None:
    print(f'Connecting to PostgreSQL at {pghost}:{pgport}, db={dbname}, user={user}...')
