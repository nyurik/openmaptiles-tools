import logging
from functools import partial
from typing import Union, List, Any, Dict

from asyncpg import Connection, ConnectionDoesNotExistError, PostgresLogMessage, \
    create_pool
from asyncpg.pool import Pool
# noinspection PyUnresolvedReferences
from tornado.ioloop import IOLoop
# noinspection PyUnresolvedReferences
from tornado.log import access_log
# noinspection PyUnresolvedReferences
from tornado.web import Application, RequestHandler

from openmaptiles.pgutils import show_settings, get_postgis_version, PgWarnings, \
    create_metadata, print_connecting
from openmaptiles.sqltomvt import MvtGenerator
from openmaptiles.tileset import Tileset


class RequestHandledWithCors(RequestHandler):
    def set_default_headers(self):
        self.set_header("Access-Control-Allow-Origin", "*")
        self.set_header("Access-Control-Allow-Headers", "x-requested-with")
        self.set_header('Access-Control-Allow-Methods', 'GET, OPTIONS')

    def options(self):
        self.set_status(204)
        self.finish()

    def head(self):
        # TODO: Technically here we should do a full tile/metadata retrieval,
        # but without sending the actual content back.
        # We must implement it to support QGIS
        self.finish()


class GetTile(RequestHandledWithCors):
    pool: Pool
    query: str
    key_column: str
    test_geometry: bool
    gzip: bool
    verbose: bool
    connection: Union[Connection, None]
    cancelled: bool

    def initialize(self, pool, query, key_column, gzip, verbose, test_geometry):
        self.pool = pool
        self.query = query
        self.key_column = key_column
        self.gzip = gzip
        self.test_geometry = test_geometry
        self.verbose = verbose
        self.connection = None
        self.cancelled = False

    async def get(self, zoom, x, y):
        messages: List[PostgresLogMessage] = []

        def logger(_, log_msg: PostgresLogMessage):
            messages.append(log_msg)

        self.set_header("Content-Type", "application/x-protobuf")
        self.set_header("Content-Disposition", "attachment")
        self.set_header("Access-Control-Allow-Origin", "*")
        try:
            async with self.pool.acquire() as connection:
                connection.add_log_listener(logger)
                self.connection = connection
                query = self.query
                zoom, x, y = int(zoom), int(x), int(y)
                if self.verbose:
                    # Make it easier to track queries in pg_stat_activity table
                    query = f"/* {zoom}/{x}/{y} */ " + query
                if self.key_column or self.test_geometry:
                    row = await connection.fetchrow(query, zoom, x, y)
                    tile = row['mvt']
                    key = row['key'] if self.key_column else None
                    bad_geos = row['bad_geos'] if self.test_geometry else 0
                else:
                    tile = await connection.fetchval(query, zoom, x, y)
                    key = None
                    bad_geos = 0
                if tile:
                    if self.gzip:
                        self.set_header("content-encoding", "gzip")
                    if key:
                        # Report strong validation, see
                        # https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/ETag
                        self.set_header("ETag", f'"{key}"')
                    self.write(tile)

                    if self.verbose or bad_geos > 0 or messages:
                        print(f"Tile {zoom}/{x}/{y}"
                              f"{f' key={key}' if self.key_column else ''} "
                              f"is {len(tile):,} bytes"
                              f"{bad_geos and f' has {bad_geos} bad geometries' or ''}"
                              )
                else:
                    self.set_status(204)
                    if self.verbose or messages:
                        print(f"Tile {zoom}/{x}/{y} is empty.")
                for msg in messages:
                    PgWarnings.print_message(msg)
                connection.remove_log_listener(logger)

        except ConnectionDoesNotExistError as err:
            if not self.cancelled:
                raise err
            elif self.verbose:
                print(f"Tile request {zoom}/{x}/{y} was cancelled.")
        finally:
            self.connection = None

    def on_connection_close(self):
        if self.connection:
            self.cancelled = True
            self.connection.terminate()


class GetMetadata(RequestHandledWithCors):
    metadata: str

    def initialize(self, metadata):
        self.metadata = metadata

    def get(self):
        self.write(self.metadata)
        print('Returning metadata')


class Postserve:
    pool: Pool
    metadata: Dict[str, Any]
    generated_query: str

    def __init__(self, url, port, pghost, pgport, dbname, user, password,
                 layers, tileset_path, sql_file, key_column, disable_feature_ids,
                 gzip, verbose, exclude_layers, test_geometry):
        self.url = url
        self.port = port
        self.pghost = pghost
        self.pgport = pgport
        self.dbname = dbname
        self.user = user
        self.password = password
        self.tileset_path = tileset_path
        self.sql_file = sql_file
        self.layer_ids = layers
        self.exclude_layers = exclude_layers
        self.key_column = key_column
        self.gzip = gzip
        self.disable_feature_ids = disable_feature_ids
        self.test_geometry = test_geometry
        self.verbose = verbose

    async def init_connection(self):
        tileset = Tileset.parse(self.tileset_path)
        async with self.pool.acquire() as conn:
            await show_settings(conn)
            mvt = MvtGenerator(
                tileset,
                postgis_ver=await get_postgis_version(conn),
                zoom='$1', x='$2', y='$3',
                layer_ids=self.layer_ids,
                key_column=self.key_column,
                gzip=self.gzip,
                use_feature_id=False if self.disable_feature_ids else None,
                test_geometry=self.test_geometry,
                exclude_layers=self.exclude_layers,
            )
            self.metadata = await create_metadata(conn, mvt, self.url + '/tiles/')
            self.generated_query = mvt.generate_sql()

    def serve(self):
        access_log.setLevel(logging.INFO if self.verbose else logging.ERROR)
        print_connecting(self.pghost, self.pgport, self.dbname, self.user)
        io_loop = IOLoop.current()
        self.pool = io_loop.run_sync(partial(
            create_pool,
            dsn=f"postgresql://{self.user}:{self.password}@"
                f"{self.pghost}:{self.pgport}/{self.dbname}"))
        io_loop.run_sync(partial(self.init_connection))

        if self.sql_file:
            with open(self.sql_file) as stream:
                query = stream.read()
            print(f'Loaded {self.sql_file}')
        else:
            query = self.generated_query

        if self.verbose:
            print(f'Using SQL query:\n\n-------\n\n{query}\n\n-------\n\n')

        application = Application([
            (
                r"/",
                GetMetadata,
                dict(metadata=self.metadata)
            ),
            (
                r"/tiles/([0-9]+)/([0-9]+)/([0-9]+).pbf",
                GetTile,
                dict(pool=self.pool, query=query, key_column=self.key_column,
                     gzip=self.gzip, test_geometry=self.test_geometry,
                     verbose=self.verbose)
            ),
        ])

        application.listen(self.port)
        print(f"Postserve started, listening on 0.0.0.0:{self.port}")
        print(f"Use {self.url} as the data source")
        IOLoop.instance().start()
