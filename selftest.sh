#!/bin/sh
set -eu

selftest_port="${YQL_LITE_SELFTEST_PORT:-18992}"
tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/yql-lite-selftest.XXXXXX")"
server_pid=""

cleanup() {
    if [ -n "$server_pid" ]; then
        kill "$server_pid" 2>/dev/null || true
        wait "$server_pid" 2>/dev/null || true
    fi
    rm -rf "$tmp_dir"
}
trap cleanup EXIT HUP INT TERM

PORT="$selftest_port" ./server >"$tmp_dir/server.out" 2>&1 &
server_pid=$!

ready=0
attempt=0
while [ "$attempt" -lt 40 ]; do
    if curl -sS -o /dev/null "http://127.0.0.1:$selftest_port/" 2>/dev/null; then
        ready=1
        break
    fi
    attempt=$((attempt + 1))
    sleep 0.1
done
if [ "$ready" -ne 1 ]; then
    cat "$tmp_dir/server.out"
    printf '%s\n' 'selftest: server did not start'
    exit 1
fi

check() {
    expected="$1"
    method="$2"
    path="$3"
    body="$4"
    if [ "$method" = POST ]; then
        status="$(curl -sS -o "$tmp_dir/body" -w '%{http_code}' -X POST -H 'Content-Type: application/xml' --data "$body" "http://127.0.0.1:$selftest_port$path")"
    else
        status="$(curl -sS -o "$tmp_dir/body" -w '%{http_code}' "http://127.0.0.1:$selftest_port$path")"
    fi
    if [ "$status" != "$expected" ]; then
        printf 'selftest: %s %s expected %s, got %s\n' "$method" "$path" "$expected" "$status"
        cat "$tmp_dir/body"
        exit 1
    fi
    printf 'ok %s %s -> %s\n' "$method" "$path" "$status"
}

check 200 GET / ''
grep -q '"name":"yql-lite"' "$tmp_dir/body"
check 200 GET /dgw ''
grep -q '^ok$' "$tmp_dir/body"
check 400 GET '/yql/weather?q=' ''
check 404 GET /missing ''
check 400 POST /dgw '<request'
check 400 POST /dgw '<request api="weather"><query type="unsupported"/></request>'
check 400 POST /dgw '<request api="finance"><query type="getquotes"/></request>'

curl -sS -o /dev/null -H 'CF-Connecting-IP: 203.0.113.8' "http://127.0.0.1:$selftest_port/"
if ! grep -q '203.0.113.8' "$tmp_dir/server.out"; then
    printf '%s\n' 'selftest: loopback Cloudflare client IP was not logged'
    exit 1
fi
printf '%s\n' 'ok loopback CF-Connecting-IP logging'
