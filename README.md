# yql-lite

Weather & Stocks backend for legacy iOS (2-7) devices rewritten in C.

## Features

- Weather and stocks data endpoints for legacy iOS Weather and Stocks apps
- Single-file C server, no framework dependencies
- SQLite-backed place lookup (`places.db`)
- Self-test script (`selftest.sh`)

## Requirements

- macOS or Linux
- libcurl, json-c, libxml2, sqlite3 (via pkg-config)

## Build

```sh
make
```

## Run

```sh
./server
```

## Test

```sh
make selftest
```

## Credits

Fork of [YQL-X-Server](https://github.com/TestOrig/YQL-X-Server-New) by Requis and Electimon.

## License

Released under the GPL-3.0. See [LICENSE](LICENSE).
