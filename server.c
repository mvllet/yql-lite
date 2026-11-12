#define _GNU_SOURCE
#include <arpa/inet.h>
#include <ctype.h>
#include <curl/curl.h>
#include <errno.h>
#include <fcntl.h>
#if __has_include("/opt/homebrew/opt/json-c/include/json-c/json.h")
#include "/opt/homebrew/opt/json-c/include/json-c/json.h"
#else
#include <json-c/json.h>
#endif
#include <libxml/parser.h>
#include <libxml/tree.h>
#include <math.h>
#include <netinet/in.h>
#include <pthread.h>
#include <signal.h>
#include <sqlite3.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>

#define REQUEST_LIMIT 65536
#define BODY_LIMIT 49152
#define THREAD_COUNT 12
#define QUEUE_SIZE 256
#define CACHE_SIZE 512
#define CACHE_TTL 900
#define RATE_LIMIT 100
#define RATE_WINDOW 10
#define MAX_SYMBOLS 32
#define MAX_WOEIDS 10
#define MAX_RESPONSE (2 * 1024 * 1024)

struct buffer {
    char *data;
    size_t len, cap;
};
struct request {
    char method[8], target[4096], path[2048], query[8192], version[16];
    char body[BODY_LIMIT + 1];
    size_t body_len;
    char peer[INET6_ADDRSTRLEN], client[INET6_ADDRSTRLEN];
};
struct job {
    int fd;
    char peer[INET6_ADDRSTRLEN];
};
struct cache_entry {
    char key[256];
    time_t saved;
    char *json;
};
struct rate_entry {
    char ip[INET6_ADDRSTRLEN];
    time_t start;
    unsigned count;
};
static pthread_mutex_t queue_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t queue_ready = PTHREAD_COND_INITIALIZER;
static struct job queue[QUEUE_SIZE];
static size_t queue_head, queue_tail, queue_count;
static pthread_mutex_t cache_lock = PTHREAD_MUTEX_INITIALIZER;
static struct cache_entry cache[CACHE_SIZE];
static struct rate_entry rate_table[1024];
static pthread_mutex_t rate_lock = PTHREAD_MUTEX_INITIALIZER;
static sqlite3 *db;
static int listen_fd = -1;
static volatile sig_atomic_t stopping;
static unsigned port = 8000;

static void buf_free(struct buffer *b) {
    free(b->data);
    memset(b, 0, sizeof *b);
}

static int buf_reserve(struct buffer *b, size_t extra) {
    if (b->len >= MAX_RESPONSE || extra > MAX_RESPONSE - b->len - 1)
        return -1;
    size_t need = b->len + extra + 1;

    if (need <= b->cap)
        return 0;
    size_t cap = b->cap ? b->cap : 1024;

    while (cap < need)
        cap = cap > MAX_RESPONSE / 2 ? MAX_RESPONSE + 1 : cap * 2;
    if (cap > MAX_RESPONSE + 1)
        return -1;
    char *p = realloc(b->data, cap);

    if (!p)
        return -1;
    b->data = p;
    b->cap = cap;
    return 0;
}

static int buf_addn(struct buffer *b, const char *s, size_t n) {
    if (buf_reserve(b, n) < 0)
        return -1;
    memcpy(b->data + b->len, s, n);
    b->len += n;
    b->data[b->len] = 0;
    return 0;
}

static int buf_add(struct buffer *b, const char *s) {
    return buf_addn(b, s, strlen(s));
}

static int buf_printf(struct buffer *b, const char *fmt, ...) {
    va_list ap, cp;

    va_start(ap, fmt);
    va_copy(cp, ap);
    int n = vsnprintf(NULL, 0, fmt, cp);

    va_end(cp);
    if (n < 0 || buf_reserve(b, (size_t)n) < 0) {
        va_end(ap);
        return -1;
    }
    vsnprintf(b->data + b->len, b->cap - b->len, fmt, ap);
    va_end(ap);
    b->len += (size_t)n;
    return 0;
}

static int xml_escape(struct buffer *b, const char *s) {
    if (!s)
        return 0;
    for (; *s; s++) {
        const char *r = NULL;

        switch (*s) {
        case '&':
            r = "&amp;";
            break;
        case '<':
            r = "&lt;";
            break;
        case '>':
            r = "&gt;";
            break;
        case '"':
            r = "&quot;";
            break;
        case '\'':
            r = "&apos;";
            break;
        }
        if (r) {
            if (buf_add(b, r) < 0)
                return -1;
        } else if ((unsigned char)*s >= 32 || *s == '\t' || *s == '\n' || *s == '\r') {
            if (buf_addn(b, s, 1) < 0)
                return -1;
        }
    }
    return 0;
}

static const char *jstr(struct json_object *o, const char *key, const char *fallback) {
    struct json_object *v;

    return o && json_object_object_get_ex(o, key, &v) && json_object_is_type(v, json_type_string)
               ? json_object_get_string(v)
               : fallback;
}

static double jnum(struct json_object *o, const char *key, double fallback) {
    struct json_object *v;

    if (!o || !json_object_object_get_ex(o, key, &v))
        return fallback;
    return json_object_is_type(v, json_type_int) || json_object_is_type(v, json_type_double)
               ? json_object_get_double(v)
               : fallback;
}

static struct json_object *jget(struct json_object *o, const char *key) {
    struct json_object *v = NULL;

    return o && json_object_object_get_ex(o, key, &v) ? v : NULL;
}

static double jvalue_num(struct json_object *o, double fallback) {
    if (!o || json_object_is_type(o, json_type_null))
        return fallback;
    return json_object_is_type(o, json_type_int) || json_object_is_type(o, json_type_double)
               ? json_object_get_double(o)
               : fallback;
}

static size_t jlength(struct json_object *o) {
    return o && json_object_is_type(o, json_type_array) ? json_object_array_length(o) : 0;
}

static struct json_object *jidx(struct json_object *o, size_t i) {
    return o && json_object_is_type(o, json_type_array) && i < json_object_array_length(o)
               ? json_object_array_get_idx(o, i)
               : NULL;
}

static size_t curl_write(char *ptr, size_t size, size_t nmemb, void *userdata) {
    if (size && nmemb > SIZE_MAX / size)
        return 0;
    struct buffer *b = userdata;
    size_t n = size * nmemb;

    return buf_addn(b, ptr, n) == 0 ? n : 0;
}

static struct json_object *fetch_json(const char *url) {
    CURL *c = curl_easy_init();
    struct buffer b = {0};
    long status = 0;

    if (!c)
        return NULL;
    curl_easy_setopt(c, CURLOPT_URL, url);
    curl_easy_setopt(c, CURLOPT_WRITEFUNCTION, curl_write);
    curl_easy_setopt(c, CURLOPT_WRITEDATA, &b);
    curl_easy_setopt(c, CURLOPT_USERAGENT, "yql-lite/3.0");
    curl_easy_setopt(c, CURLOPT_FOLLOWLOCATION, 1L);
    curl_easy_setopt(c, CURLOPT_MAXREDIRS, 4L);
    curl_easy_setopt(c, CURLOPT_ACCEPT_ENCODING, "");
    curl_easy_setopt(c, CURLOPT_CONNECTTIMEOUT_MS, 2500L);
    curl_easy_setopt(c, CURLOPT_TIMEOUT_MS, 8000L);
    curl_easy_setopt(c, CURLOPT_NOSIGNAL, 1L);
    CURLcode rc = curl_easy_perform(c);

    curl_easy_getinfo(c, CURLINFO_RESPONSE_CODE, &status);
    curl_easy_cleanup(c);
    struct json_object *j = NULL;

    if (rc == CURLE_OK && status >= 200 && status < 300 && b.data)
        j = json_tokener_parse(b.data);
    buf_free(&b);
    return j;
}

static struct json_object *cache_get(const char *key) {
    time_t now = time(NULL);
    struct json_object *result = NULL;

    pthread_mutex_lock(&cache_lock);
    for (size_t i = 0; i < CACHE_SIZE; i++)
        if (cache[i].json && strcmp(cache[i].key, key) == 0) {
            if (now - cache[i].saved < CACHE_TTL)
                result = json_tokener_parse(cache[i].json);
            else {
                free(cache[i].json);
                memset(&cache[i], 0, sizeof cache[i]);
            }
            break;
        }
    pthread_mutex_unlock(&cache_lock);
    return result;
}

static void cache_set(const char *key, struct json_object *value) {
    if (!value)
        return;
    time_t now = time(NULL);
    size_t slot = (size_t)((unsigned long)now % CACHE_SIZE);
    const char *raw = json_object_to_json_string_ext(value, JSON_C_TO_STRING_PLAIN);
    char *copy = strdup(raw);

    if (!copy)
        return;
    pthread_mutex_lock(&cache_lock);
    for (size_t i = 0; i < CACHE_SIZE; i++)
        if (cache[i].json && strcmp(cache[i].key, key) == 0) {
            slot = i;
            break;
        }
    free(cache[slot].json);
    snprintf(cache[slot].key, sizeof cache[slot].key, "%s", key);
    cache[slot].saved = now;
    cache[slot].json = copy;
    pthread_mutex_unlock(&cache_lock);
}

// Reuse recent upstream data to avoid repeating the same network request.
static struct json_object *fetch_cached(const char *key, const char *url) {
    struct json_object *j = cache_get(key);

    if (j)
        return j;
    j = fetch_json(url);
    if (j)
        cache_set(key, j);
    return j;
}

static int parse_port(const char *s, unsigned *out) {
    char *end = NULL;

    errno = 0;
    unsigned long v = strtoul(s, &end, 10);

    if (errno || !s[0] || *end || v == 0 || v > 65535)
        return -1;
    *out = (unsigned)v;
    return 0;
}

static void load_env(void) {
    FILE *f = fopen(".env", "r");

    if (f) {
        char line[512];

        while (fgets(line, sizeof line, f)) {
            char *p = line;

            while (isspace((unsigned char)*p))
                p++;
            if (!strncmp(p, "PORT=", 5)) {
                char *v = p + 5;

                v[strcspn(v, "\r\n #")] = 0;
                unsigned candidate;

                if (parse_port(v, &candidate) == 0)
                    port = candidate;
            }
        }
        fclose(f);
    }
    const char *env = getenv("PORT");
    unsigned candidate;

    if (env && parse_port(env, &candidate) == 0)
        port = candidate;
}

static int send_response(
    int fd, int status, const char *reason, const char *type, const char *body, size_t len) {
    char header[512];
    int n = snprintf(header,
                     sizeof header,
                     "HTTP/1.1 %d %s\r\nContent-Type: %s\r\nContent-Length: %zu\r\nConnection: "
                     "close\r\nX-Content-Type-Options: nosniff\r\n\r\n",
                     status,
                     reason,
                     type,
                     len);

    if (n <= 0 || (size_t)n >= sizeof header)
        return -1;
    const char *parts[2] = {header, body};
    size_t sizes[2] = {(size_t)n, len};

    for (size_t p = 0; p < 2; p++) {
        const char *s = parts[p];
        size_t left = sizes[p];

        while (left) {
            ssize_t sent = send(fd, s, left, MSG_NOSIGNAL);

            if (sent < 0 && errno == EINTR)
                continue;
            if (sent <= 0)
                return -1;
            s += sent;
            left -= (size_t)sent;
        }
    }
    return status;
}

static int reply_text(int fd, int status, const char *reason, const char *text) {
    int rc = send_response(fd, status, reason, "text/plain; charset=utf-8", text, strlen(text));

    return rc < 0 ? rc : status;
}

static int read_request(int fd, const char *peer, struct request *r) {
    char raw[REQUEST_LIMIT + 1];
    size_t have = 0, header_end = 0, content_length = 0;
    int has_content_length = 0;
    struct timeval tv = {.tv_sec = 8};

    // Read one bounded request and reject ambiguous or unsupported body framing.
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof tv);
    while (have < REQUEST_LIMIT) {
        ssize_t n = recv(fd, raw + have, REQUEST_LIMIT - have, 0);

        if (n < 0 && errno == EINTR)
            continue;
        if (n <= 0)
            return -1;
        have += (size_t)n;
        raw[have] = 0;
        char *end = strstr(raw, "\r\n\r\n");

        if (end) {
            header_end = (size_t)(end - raw) + 4;
            break;
        }
    }
    if (!header_end || sscanf(raw, "%7s %4095s %15s", r->method, r->target, r->version) != 3 ||
        strncmp(r->version, "HTTP/1.", 7))
        return -1;
    char *line = strstr(raw, "\r\n");

    while (line && line < raw + header_end - 2) {
        line += 2;
        if (!strncasecmp(line, "Content-Length:", 15)) {
            char *v = line + 15;

            while (*v == ' ' || *v == '\t')
                v++;
            char *e;

            errno = 0;
            unsigned long x = strtoul(v, &e, 10);

            while (*e == ' ' || *e == '\t')
                e++;
            if (errno || e == v || (*e != '\r' && *e != '\n') || x > BODY_LIMIT ||
                has_content_length)
                return -2;
            has_content_length = 1;
            content_length = (size_t)x;
        }
        if (!strncasecmp(line, "Transfer-Encoding:", 18))
            return -2;
        line = strstr(line, "\r\n");
    }
    if (header_end + content_length > REQUEST_LIMIT || content_length > BODY_LIMIT)
        return -2;
    while (have < header_end + content_length) {
        ssize_t n = recv(fd, raw + have, header_end + content_length - have, 0);

        if (n < 0 && errno == EINTR)
            continue;
        if (n <= 0)
            return -1;
        have += (size_t)n;
    }
    r->body_len = content_length;
    memcpy(r->body, raw + header_end, content_length);
    r->body[content_length] = 0;
    char *q = strchr(r->target, '?');

    if (q) {
        *q++ = 0;
        if (strlen(q) >= sizeof r->query)
            return -1;
        snprintf(r->query, sizeof r->query, "%s", q);
    }
    if (strlen(r->target) >= sizeof r->path)
        return -1;
    snprintf(r->path, sizeof r->path, "%s", r->target);
    snprintf(r->peer, sizeof r->peer, "%s", peer);
    snprintf(r->client, sizeof r->client, "%s", peer);
    // Trust CF-Connecting-IP only from loopback, where the tunnel connects.
    if (!strcmp(peer, "127.0.0.1") || !strcmp(peer, "::1")) {
        line = strstr(raw, "\r\n");
        while (line && line < raw + header_end - 2) {
            line += 2;
            if (!strncasecmp(line, "CF-Connecting-IP:", 17)) {
                char *v = line + 17;

                while (*v == ' ' || *v == '\t')
                    v++;
                size_t n = strcspn(v, "\r\n");
                char ip[INET6_ADDRSTRLEN];

                if (n < sizeof ip) {
                    memcpy(ip, v, n);
                    ip[n] = 0;
                    struct in_addr a4;
                    struct in6_addr a6;

                    if (inet_pton(AF_INET, ip, &a4) == 1 || inet_pton(AF_INET6, ip, &a6) == 1)
                        snprintf(r->client, sizeof r->client, "%s", ip);
                }
                break;
            }
            line = strstr(line, "\r\n");
        }
    }
    return 0;
}

static void log_request(const struct request *r, int status) {
    fprintf(stdout, "%s %s ", r->client, r->method);
    for (const char *p = r->path; *p; p++)
        if (!isprint((unsigned char)*p))
            fputc('?', stdout);
        else
            fputc(*p, stdout);
    fprintf(stdout, " %d\n", status);
    fflush(stdout);
}

// Return 1 while this IP stays within the request limit, otherwise return 0.
static int rate_allow(const char *ip) {
    time_t now = time(NULL);
    unsigned long hash = 5381;

    for (const unsigned char *p = (const unsigned char *)ip; *p; p++)
        hash = hash * 33u + *p;
    // Hashing bounds lookup work while sharing rate state across workers.
    size_t slot = hash % (sizeof rate_table / sizeof rate_table[0]);

    pthread_mutex_lock(&rate_lock);
    struct rate_entry *entry = &rate_table[slot];

    if (strcmp(entry->ip, ip) != 0 || now - entry->start >= RATE_WINDOW) {
        snprintf(entry->ip, sizeof entry->ip, "%s", ip);
        entry->start = now;
        entry->count = 1;
        pthread_mutex_unlock(&rate_lock);
        return 1;
    }
    int allowed = entry->count < RATE_LIMIT;

    if (allowed)
        entry->count++;
    pthread_mutex_unlock(&rate_lock);
    return allowed;
}

static int hexval(unsigned char c) {
    if (c >= '0' && c <= '9')
        return c - '0';
    c = (unsigned char)tolower(c);
    return c >= 'a' && c <= 'f' ? c - 'a' + 10 : -1;
}

static int url_decode(const char *s, char *out, size_t cap) {
    size_t n = 0;

    // Decode percent escapes and reject malformed or embedded-NUL values.
    while (*s && n + 1 < cap) {
        if (*s == '+')
            out[n++] = ' ';
        else if (*s == '%') {
            if (!s[1] || !s[2])
                return -1;
            int a = hexval((unsigned char)s[1]), b = hexval((unsigned char)s[2]);

            if (a < 0 || b < 0 || (a == 0 && b == 0))
                return -1;
            out[n++] = (char)(a * 16 + b);
            s += 2;
        } else
            out[n++] = *s;
        s++;
    }
    if (*s)
        return -1;
    out[n] = 0;
    return 0;
}

static char *query_value(const char *query, const char *name) {
    size_t name_len = strlen(name);
    const char *p = query;

    while (*p) {
        const char *end = strchr(p, '&');
        size_t len = end ? (size_t)(end - p) : strlen(p);
        const char *eq = memchr(p, '=', len);

        if (eq && (size_t)(eq - p) == name_len && !strncmp(p, name, name_len)) {
            size_t cap = len + 1;
            char *out = malloc(cap);

            if (!out)
                return NULL;
            char raw[8192];
            size_t vn = len - name_len - 1;

            if (vn >= sizeof raw) {
                free(out);
                return NULL;
            }
            memcpy(raw, eq + 1, vn);
            raw[vn] = 0;
            if (url_decode(raw, out, cap) < 0) {
                free(out);
                return NULL;
            }
            return out;
        }
        if (!end)
            break;
        p = end + 1;
    }
    return NULL;
}

static int weather_icon(int code, int day) {
    static const int map[][3] = {
        {0, 32, 31},  {1, 34, 33},  {2, 30, 29},  {3, 26, 26},  {45, 20, 20}, {48, 20, 20},
        {51, 9, 9},   {53, 9, 9},   {55, 9, 9},   {56, 8, 8},   {57, 8, 8},   {61, 11, 11},
        {63, 11, 11}, {65, 40, 40}, {66, 10, 10}, {67, 10, 10}, {71, 13, 13}, {73, 16, 16},
        {75, 41, 41}, {77, 13, 13}, {80, 39, 11}, {81, 11, 11}, {82, 40, 40}, {85, 46, 46},
        {86, 42, 42}, {95, 4, 4},   {96, 35, 35}, {99, 17, 17}};

    for (size_t i = 0; i < sizeof map / sizeof map[0]; i++)
        if (map[i][0] == code)
            return map[i][day ? 1 : 2];
    return 26;
}

static int format_local(time_t stamp, int offset, const char *fmt, char *out, size_t cap) {
    struct tm tmv;
    time_t adjusted = stamp + offset;

    if (!gmtime_r(&adjusted, &tmv))
        return -1;
    return (int)strftime(out, cap, fmt, &tmv);
}

static void utc_created(char out[32]) {
    time_t now = time(NULL);
    struct tm tmv;

    if (!gmtime_r(&now, &tmv) || !strftime(out, 32, "%Y-%m-%dT%H:%M:%SZ", &tmv))
        snprintf(out, 32, "1970-01-01T00:00:00Z");
}

static int contains_bytes(const char *data, size_t size, const char *needle, size_t needle_size) {
    if (needle_size > size)
        return 0;
    for (size_t i = 0; i <= size - needle_size; i++)
        if (memcmp(data + i, needle, needle_size) == 0)
            return 1;
    return 0;
}

static int coordinates_woeid(double lat, double lon) {
    long la = lround((lat + 90.0) * 100.0), lo = lround((lon + 180.0) * 100.0);

    if (la < 0)
        la = 0;
    if (la > 18000)
        la = 18000;
    if (lo < 0)
        lo = 0;
    if (lo > 36000)
        lo = 36000;
    return (int)(la * 36001L + lo);
}

struct place {
    int woeid;
    double lat, lon;
    char name[128], country[3], state[128];
};

static void place_save(const struct place *p) {
    sqlite3_stmt *st = NULL;
    const char *sql = "INSERT OR REPLACE INTO places VALUES (?1,?2,?3,?4,?5,?6)";

    if (sqlite3_prepare_v2(db, sql, -1, &st, NULL) != SQLITE_OK)
        return;
    sqlite3_bind_int(st, 1, p->woeid);
    sqlite3_bind_text(st, 2, p->name, -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(st, 3, p->country, -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(st, 4, p->state, -1, SQLITE_TRANSIENT);
    sqlite3_bind_double(st, 5, p->lat);
    sqlite3_bind_double(st, 6, p->lon);
    sqlite3_step(st);
    sqlite3_finalize(st);
}

static int place_load(int woeid, struct place *p) {
    sqlite3_stmt *st = NULL;

    if (sqlite3_prepare_v2(
            db,
            "SELECT woeid,name,country_code,state,latitude,longitude FROM places WHERE woeid=?1",
            -1,
            &st,
            NULL) != SQLITE_OK)
        return 0;
    sqlite3_bind_int(st, 1, woeid);
    int ok = sqlite3_step(st) == SQLITE_ROW;

    if (ok) {
        p->woeid = sqlite3_column_int(st, 0);
        snprintf(p->name, sizeof p->name, "%s", sqlite3_column_text(st, 1));
        snprintf(p->country, sizeof p->country, "%s", sqlite3_column_text(st, 2));
        snprintf(p->state, sizeof p->state, "%s", sqlite3_column_text(st, 3));
        p->lat = sqlite3_column_double(st, 4);
        p->lon = sqlite3_column_double(st, 5);
    }
    sqlite3_finalize(st);
    return ok;
}

static int country_name(const char *code, char *out, size_t cap) {
    static const struct {
        const char *code, *name;
    } countries[] = {{"US", "United States"}, {"GB", "United Kingdom"}, {"DE", "Germany"},
                     {"FR", "France"},        {"CA", "Canada"},         {"AU", "Australia"},
                     {"JP", "Japan"},         {"IN", "India"},          {"IT", "Italy"},
                     {"ES", "Spain"},         {"NL", "Netherlands"},    {"BR", "Brazil"},
                     {"CN", "China"},         {"RU", "Russia"},         {"MX", "Mexico"},
                     {"CH", "Switzerland"},   {"AT", "Austria"},        {"SE", "Sweden"},
                     {"NO", "Norway"},        {"DK", "Denmark"}};

    for (size_t i = 0; i < sizeof countries / sizeof countries[0]; i++)
        if (!strcmp(code, countries[i].code)) {
            snprintf(out, cap, "%s", countries[i].name);
            return 0;
        }
    snprintf(out, cap, "%s", *code ? code : "Unknown");
    return 0;
}

static int geocode_search(const char *term, const char *lang, struct place *out, size_t *count) {
    CURL *curl = curl_easy_init();

    if (!curl)
        return -1;
    char *enc = curl_easy_escape(curl, term, 0);
    char url[4096];

    snprintf(
        url,
        sizeof url,
        "https://geocoding-api.open-meteo.com/v1/search?name=%s&count=10&language=%.2s&format=json",
        enc ? enc : "",
        lang && *lang ? lang : "en");
    curl_free(enc);
    curl_easy_cleanup(curl);
    char key[256];

    snprintf(key, sizeof key, "geo:%s:%.2s", term, lang && *lang ? lang : "en");
    struct json_object *j = fetch_cached(key, url);

    if (!j)
        return -1;
    struct json_object *arr = jget(j, "results");

    *count = 0;
    for (size_t i = 0; i < jlength(arr) && *count < 10; i++) {
        struct json_object *it = jidx(arr, i);
        struct place *p = &out[(*count)++];

        memset(p, 0, sizeof *p);
        p->lat = jnum(it, "latitude", 0);
        p->lon = jnum(it, "longitude", 0);
        p->woeid = coordinates_woeid(p->lat, p->lon);
        snprintf(p->name, sizeof p->name, "%.120s", jstr(it, "name", "Unknown"));
        snprintf(p->country, sizeof p->country, "%.2s", jstr(it, "country_code", ""));
        snprintf(p->state, sizeof p->state, "%.120s", jstr(it, "admin1", ""));
        place_save(p);
    }
    json_object_put(j);
    return 0;
}

static int place_resolve(int woeid, const char *lang, struct place *p) {
    if (place_load(woeid, p))
        return 0;
    long v = woeid;

    if (v < 0 || v > 648054000L)
        return -1;
    int lat_unit = (int)(v / 36001L), lon_unit = (int)(v % 36001L);

    p->lat = lat_unit / 100.0 - 90;
    p->lon = lon_unit / 100.0 - 180;
    p->woeid = woeid;
    char key[128], url[1024];

    snprintf(key, sizeof key, "rev:%d:%.2s", woeid, lang ? lang : "en");
    snprintf(url,
             sizeof url,
             "https://api.bigdatacloud.net/data/"
             "reverse-geocode-client?latitude=%.2f&longitude=%.2f&localityLanguage=%.2s",
             p->lat,
             p->lon,
             lang && *lang ? lang : "en");
    struct json_object *j = fetch_cached(key, url);

    snprintf(p->name, sizeof p->name, "Current Location");
    p->country[0] = 0;
    p->state[0] = 0;
    if (j) {
        snprintf(p->name,
                 sizeof p->name,
                 "%.120s",
                 jstr(j,
                      "city",
                      jstr(j, "locality", jstr(j, "principalSubdivision", "Current Location"))));
        snprintf(p->country, sizeof p->country, "%.2s", jstr(j, "countryCode", ""));
        snprintf(p->state, sizeof p->state, "%.120s", jstr(j, "principalSubdivision", ""));
        json_object_put(j);
    }
    place_save(p);
    return 0;
}

struct wx {
    int woeid, code, humidity, moon, phase, winddeg, offset;
    double lat, lon, temp, dew, feels, pressure, wind, visibility;
    char city[128], country[3], state[128], now12[32], now24[16], rise12[32], rise24[16], set12[32],
        set24[16], tz[24];
    struct {
        int code, pop;
        double high, low;
        int highr, lowr, ordinal;
    } days[7];
    size_t ndays;
    struct {
        int code, pop;
        double temp;
        char time[16];
    } hours[12];
    size_t nhours;
};

static int fetch_weather(double lat, double lon, struct json_object **out) {
    char key[128], url[2048];

    snprintf(key, sizeof key, "weather:%.2f:%.2f", lat, lon);
    snprintf(url,
             sizeof url,
             "https://api.open-meteo.com/v1/"
             "forecast?latitude=%.4f&longitude=%.4f&current=temperature_2m,apparent_temperature,"
             "relative_humidity_2m,dew_point_2m,pressure_msl,wind_speed_10m,wind_direction_10m,"
             "weather_code,is_day&hourly=temperature_2m,precipitation_probability,weather_code,is_"
             "day,visibility&daily=temperature_2m_max,temperature_2m_min,weather_code,sunrise,"
             "sunset,precipitation_probability_max&timezone=auto&timeformat=unixtime&forecast_days="
             "7&wind_speed_unit=kmh",
             lat,
             lon);
    *out = fetch_cached(key, url);
    return *out ? 0 : -1;
}

static double moon_illumination(time_t now, int *phase) {
    const double month = 29.530588853;
    double days = difftime(now, (time_t)946728840) / 86400.0;
    double lun = fmod(days, month) / month;

    if (lun < 0)
        lun += 1;
    double illum = round((1 - cos(2 * 3.141592653589793 * lun)) * 50);

    *phase = illum <= 1 ? 0 : (lun < 0.5 ? 1 : 5);
    return illum;
}

static int weather_build(const struct place *p, struct wx *w) {
    memset(w, 0, sizeof *w);
    struct json_object *j = NULL;

    if (fetch_weather(p->lat, p->lon, &j) < 0)
        return -1;
    struct json_object *cur = jget(j, "current"), *hour = jget(j, "hourly"),
                       *day = jget(j, "daily");

    w->woeid = p->woeid;
    w->lat = p->lat;
    w->lon = p->lon;
    snprintf(w->city, sizeof w->city, "%s", p->name);
    snprintf(w->country, sizeof w->country, "%s", p->country);
    snprintf(w->state, sizeof w->state, "%s", p->state);
    time_t now = (time_t)jnum(cur, "time", time(NULL));

    w->offset = (int)jnum(j, "utc_offset_seconds", 0);
    int isday = (int)jnum(cur, "is_day", 1);

    w->temp = jnum(cur, "temperature_2m", 0);
    w->feels = jnum(cur, "apparent_temperature", w->temp);
    w->dew = jnum(cur, "dew_point_2m", 0);
    w->humidity = (int)jnum(cur, "relative_humidity_2m", 0);
    w->pressure = (int)jnum(cur, "pressure_msl", 0);
    w->wind = jnum(cur, "wind_speed_10m", 0);
    w->winddeg = (int)jnum(cur, "wind_direction_10m", 0);
    w->code = weather_icon((int)jnum(cur, "weather_code", 3), isday);
    format_local(now, w->offset, "%I:%M %p", w->now12, sizeof w->now12);
    if (w->now12[0] == '0')
        memmove(w->now12, w->now12 + 1, strlen(w->now12));
    format_local(now, w->offset, "%H:%M", w->now24, sizeof w->now24);
    struct json_object *sunrise = jidx(jget(day, "sunrise"), 0),
                       *sunset = jidx(jget(day, "sunset"), 0);

    format_local(
        (time_t)jvalue_num(sunrise, 0), w->offset, "%I:%M %p", w->rise12, sizeof w->rise12);
    format_local((time_t)jvalue_num(sunrise, 0), w->offset, "%H:%M", w->rise24, sizeof w->rise24);
    format_local((time_t)jvalue_num(sunset, 0), w->offset, "%I:%M %p", w->set12, sizeof w->set12);
    format_local((time_t)jvalue_num(sunset, 0), w->offset, "%H:%M", w->set24, sizeof w->set24);
    if (w->rise12[0] == '0')
        memmove(w->rise12, w->rise12 + 1, strlen(w->rise12));
    if (w->set12[0] == '0')
        memmove(w->set12, w->set12 + 1, strlen(w->set12));
    snprintf(w->tz, sizeof w->tz, "GMT%+d", w->offset / 3600);
    int phase;

    w->moon = (int)moon_illumination(time(NULL), &phase);
    w->phase = phase;
    int utc_hours = w->offset / 3600;

    if (w->offset < 0 && w->offset % 3600)
        utc_hours--;
    struct json_object *times = jget(hour, "time"), *codes = jget(hour, "weather_code"),
                       *isdays = jget(hour, "is_day"),
                       *pops = jget(hour, "precipitation_probability"),
                       *temps = jget(hour, "temperature_2m"), *vis = jget(hour, "visibility");
    size_t hn = jlength(times), start = 0;

    while (start < hn && (int64_t)jvalue_num(jidx(times, start), 0) <= now)
        start++;
    for (size_t i = start; i < hn && w->nhours < 12; i++) {
        size_t k = w->nhours++;
        struct json_object *tv = jidx(times, i);

        w->hours[k].code = weather_icon((int)(int)jvalue_num(jidx(codes, i), 3),
                                        (int)jvalue_num(jidx(isdays, i), 1));
        w->hours[k].pop = (int)jvalue_num(jidx(pops, i), 0);
        w->hours[k].temp = jvalue_num(jidx(temps, i), 0);
        format_local((int64_t)jvalue_num(tv, 0),
                     w->offset,
                     "%H:%M",
                     w->hours[k].time,
                     sizeof w->hours[k].time);
    }
    w->visibility = hn ? jvalue_num(jidx(vis, start ? start - 1 : 0), 10000) / 1000.0 : 10;
    snprintf(w->tz, sizeof w->tz, "GMT%+d", utc_hours);
    struct json_object *dt = jget(day, "time"), *high = jget(day, "temperature_2m_max"),
                       *low = jget(day, "temperature_2m_min"), *dc = jget(day, "weather_code"),
                       *dp = jget(day, "precipitation_probability_max");
    size_t dn = jlength(dt);

    for (size_t i = 1; i < dn && w->ndays < 6; i++) {
        struct tm tmv;
        time_t d = (int64_t)jvalue_num(jidx(dt, i), 0) + w->offset;

        gmtime_r(&d, &tmv);
        size_t k = w->ndays++;

        w->days[k].ordinal = tmv.tm_wday ? tmv.tm_wday : 7;
        w->days[k].code = weather_icon((int)jvalue_num(jidx(dc, i), 3), 1);
        w->days[k].pop = (int)jvalue_num(jidx(dp, i), 0);
        w->days[k].high = jvalue_num(jidx(high, i), 0);
        w->days[k].low = jvalue_num(jidx(low, i), 0);
        w->days[k].highr = (int)round(w->days[k].high);
        w->days[k].lowr = (int)round(w->days[k].low);
    }
    json_object_put(j);
    return 0;
}

static const char *country_alpha3(const char *cc) {
    static const struct {
        const char *a, *b;
    } m[] = {{"US", "USA"},
             {"GB", "GBR"},
             {"DE", "DEU"},
             {"FR", "FRA"},
             {"CA", "CAN"},
             {"AU", "AUS"},
             {"JP", "JPN"},
             {"IN", "IND"},
             {"IT", "ITA"},
             {"ES", "ESP"},
             {"NL", "NLD"},
             {"CH", "CHE"},
             {"AT", "AUT"},
             {"BR", "BRA"}};

    for (size_t i = 0; i < sizeof m / sizeof m[0]; i++)
        if (!strcmp(cc, m[i].a))
            return m[i].b;
    return "UNK";
}

static int xml_header(struct buffer *b) {
    return buf_add(b, "<?xml version=\"1.0\" encoding=\"UTF-8\"?>");
}
static void xml_text(struct buffer *b, const char *s) {
    xml_escape(b, s ? s : "");
}
static void xml_attr(struct buffer *b, const char *name, const char *value) {
    buf_printf(b, " %s=\"", name);
    xml_escape(b, value);
    buf_add(b, "\"");
}

static void render_search(struct buffer *b, struct place *p, size_t n) {
    char country[128], created[32];

    utc_created(created);
    xml_header(b);
    buf_printf(b,
               "<query xmlns:yahoo=\"http://www.yahooapis.com/v1/base.rng\" yahoo:count=\"%zu\" "
               "yahoo:created=\"%s\" yahoo:lang=\"en-US\"><results>",
               n,
               created);
    for (size_t i = 0; i < n; i++) {
        country_name(p[i].country, country, sizeof country);
        buf_add(b, "<location");
        xml_attr(b, "city", p[i].name);
        xml_attr(b, "country", country);
        xml_attr(b, "countryAbbr", country_alpha3(p[i].country));
        xml_attr(b, "locationID", "ASXX0075");
        char id[32];

        snprintf(id, sizeof id, "%d", p[i].woeid);
        xml_attr(b, "woeid", id);
        buf_add(b, "/>");
    }
    buf_add(b, "</results></query>");
}

static void render_weather(struct buffer *b, struct wx *w, size_t n, const char *advert) {
    char created[32];

    utc_created(created);
    xml_header(b);
    buf_printf(b,
               "<query xmlns:yahoo=\"http://www.yahooapis.com/v1/base.rng\" yahoo:count=\"%zu\" "
               "yahoo:created=\"%s\" yahoo:lang=\"en-US\"><meta><meta><weather><yahoo_mobile_url>",
               n * 2,
               created);
    xml_text(b, advert);
    buf_add(b, "</yahoo_mobile_url><twc_mobile_url>");
    xml_text(b, advert);
    buf_add(b,
            "</twc_mobile_url><units distanceUnits=\"km\" pressureUnits=\"mb\" speedUnits=\"km/h\" "
            "tempUnits=\"C\"/></weather></meta><meta><weather><yahoo_mobile_url>");
    xml_text(b, advert);
    buf_add(b, "</yahoo_mobile_url><twc_mobile_url>");
    xml_text(b, advert);
    buf_add(b, "</twc_mobile_url><units tempUnits=\"C\"/></weather></meta></meta><results>");
    for (size_t i = 0; i < n; i++) {
        struct wx *x = &w[i];
        char city[256], state[256], country[4];

        snprintf(city, sizeof city, "%s", x->city);
        snprintf(state, sizeof state, "%s", x->state);
        snprintf(country, sizeof country, "%s", country_alpha3(x->country));
        buf_add(b, "<results><location");
        xml_attr(b, "city", city);
        xml_attr(b, "country", country);
        buf_printf(b, " latitude=\"%.4f\"", x->lat);
        xml_attr(b, "locationID", "ASXX0075");
        buf_printf(b, " longitude=\"%.4f\"", x->lon);
        xml_attr(b, "state", state);
        char id[32];

        snprintf(id, sizeof id, "%d", x->woeid);
        xml_attr(b, "woeid", id);
        buf_printf(b,
                   "><currently barometer=\"%.0f\" barometricTrend=\"\" dewpoint=\"%.1f\" "
                   "feelsLike=\"%.1f\" heatIndex=\"%.1f\" moonfacevisible=\"%d%%\" "
                   "moonphase=\"%d\" percentHumidity=\"%d\" sunrise=\"%s\" sunrise24=\"%s\" "
                   "sunset=\"%s\" sunset24=\"%s\" temp=\"%.1f\" tempBgcolor=\"\" time=\"%s\" "
                   "time24=\"%s\" timezone=\"%s\" visibility=\"%.1f\" windChill=\"%.1f\" "
                   "windDirection=\"%d\" windDirectionDegree=\"%d\" windSpeed=\"%.1f\"><condition "
                   "code=\"%d\"/></currently><forecast>",
                   x->pressure,
                   x->dew,
                   x->feels,
                   x->feels,
                   x->moon,
                   x->phase,
                   x->humidity,
                   x->rise12,
                   x->rise24,
                   x->set12,
                   x->set24,
                   x->temp,
                   x->now12,
                   x->now24,
                   x->tz,
                   x->visibility,
                   x->feels,
                   x->winddeg,
                   x->winddeg,
                   x->wind,
                   x->code);
        for (size_t d = 0; d < x->ndays; d++)
            buf_printf(b,
                       "<day dayOfWeek=\"%d\" poP=\"%d\"><temp high=\"%.1f\" "
                       "low=\"%.1f\"/><condition code=\"%d\"/></day>",
                       x->days[d].ordinal,
                       x->days[d].pop,
                       x->days[d].high,
                       x->days[d].low,
                       x->days[d].code);
        buf_add(b, "<extended_forecast_url>");
        xml_text(b, advert);
        buf_add(b, "</extended_forecast_url></forecast></location></results><results><location");
        xml_attr(b, "woeid", id);
        buf_add(b, "><hourlyforecast>");
        for (size_t h = 0; h < x->nhours; h++)
            buf_printf(
                b,
                "<hour time24=\"%s\"><condition code=\"%d\" poP=\"%d\" temp=\"%.1f\"/></hour>",
                x->hours[h].time,
                x->hours[h].code,
                x->hours[h].pop,
                x->hours[h].temp);
        buf_add(b, "</hourlyforecast></location></results>");
    }
    buf_add(b, "</results></query>");
}

static void render_dgw_weather_search(struct buffer *b, struct place *p, size_t n) {
    char country[128], id[32];

    xml_header(b);
    buf_add(b, "<response><result><list>");
    for (size_t i = 0; i < n; i++) {
        country_name(p[i].country, country, sizeof country);
        snprintf(id, sizeof id, "%d", p[i].woeid);
        buf_add(b, "<item><id>");
        xml_text(b, id);
        buf_add(b, "</id><city>");
        xml_text(b, p[i].name);
        buf_add(b, "</city><countryname>");
        xml_text(b, country);
        buf_add(b, "</countryname></item>");
    }
    buf_add(b, "</list></result></response>");
}

static void render_dgw_weather(struct buffer *b, struct wx *w, size_t n, const char *advert) {
    xml_header(b);
    buf_add(b, "<response><result><list>");
    for (size_t i = 0; i < n; i++) {
        struct wx *x = &w[i];
        char id[32];

        snprintf(id, sizeof id, "%d", x->woeid);
        buf_add(b, "<item><id>");
        xml_text(b, id);
        buf_add(b, "</id><location");
        xml_attr(b, "id", id);
        xml_attr(b, "city", x->city);
        buf_printf(b, "/><units temperature=\"C\"/><astronomy");
        xml_attr(b, "sunrise", x->rise24);
        xml_attr(b, "sunset", x->set24);
        buf_printf(b,
                   " moonphase=\"%d\" moonfacevisible=\"%d%%\"/><condition text=\"\" code=\"%d\" "
                   "temp=\"%d\"",
                   x->phase,
                   x->moon,
                   x->code,
                   (int)round(x->temp));
        xml_attr(b, "time", x->now24);
        buf_add(b, "/><link>");
        xml_text(b, advert);
        buf_add(b, "</link>");
        for (size_t d = 0; d < x->ndays; d++)
            buf_printf(b,
                       "<forecast dayofweek=\"%d\" low=\"%d\" high=\"%d\" text=\"\" code=\"%d\"/>",
                       x->days[d].ordinal,
                       x->days[d].lowr,
                       x->days[d].highr,
                       x->days[d].code);
        buf_add(b, "</item>");
    }
    buf_add(b, "</list></result></response>");
}

static char *html_unescape(const char *src) {
    struct buffer b = {0};

    for (size_t i = 0; src[i];) {
        const char *rep = NULL;
        size_t n = 0;

        if (!strncmp(src + i, "&quot;", 6)) {
            rep = "\"";
            n = 6;
        } else if (!strncmp(src + i, "&#39;", 5)) {
            rep = "'";
            n = 5;
        } else if (!strncmp(src + i, "&amp;", 5)) {
            rep = "&";
            n = 5;
        } else if (!strncmp(src + i, "&lt;", 4)) {
            rep = "<";
            n = 4;
        } else if (!strncmp(src + i, "&gt;", 4)) {
            rep = ">";
            n = 4;
        }
        if (rep) {
            buf_add(&b, rep);
            i += n;
        } else {
            buf_addn(&b, src + i, 1);
            i++;
        }
    }
    return b.data ? b.data : strdup("");
}

static int extract_query(const char *raw,
                         char *kind,
                         char *term,
                         char *lang,
                         int *woeids,
                         size_t *n,
                         double *lat,
                         double *lon) {
    char *text = html_unescape(raw);

    if (!text)
        return -1;
    snprintf(lang, 32, "en-US");
    char *lp = strstr(text, "lang=");

    if (lp) {
        lp += 5;
        if (*lp == '\'' || *lp == '\"') {
            char q = *lp++;
            char *e = strchr(lp, q);

            if (e) {
                size_t z = (size_t)(e - lp);

                if (z > 31)
                    z = 31;
                memcpy(lang, lp, z);
                lang[z] = 0;
            }
        }
    }
    char *loc = strstr(text, "weather.locations");

    if (loc && !strstr(text, "yql.query.multi")) {
        char *q = strstr(text, "query=");

        if (q) {
            q += 6;
            if (*q == '\'' || *q == '\"') {
                char quote = *q++;
                char *e = strchr(q, quote);

                if (e) {
                    size_t z = (size_t)(e - q);

                    if (z > 127)
                        z = 127;
                    memcpy(term, q, z);
                    term[z] = 0;
                    strcpy(kind, "search");
                    free(text);
                    return 0;
                }
            }
        }
    }
    char *la = strstr(text, "lat="), *lo = strstr(text, "lon=");

    if (la && lo) {
        *lat = strtod(la + 4, NULL);
        *lon = strtod(lo + 4, NULL);
        strcpy(kind, "latlon");
        free(text);
        return 0;
    }
    char *p = text;

    while ((p = strstr(p, "woeid"))) {
        char *eq = strpbrk(p, "=(");

        if (!eq)
            break;
        while (*eq && !isdigit((unsigned char)*eq))
            eq++;
        if (!*eq)
            break;
        long v = strtol(eq, NULL, 10);
        int seen = 0;

        for (size_t i = 0; i < *n; i++)
            if (woeids[i] == v)
                seen = 1;
        if (!seen && *n < MAX_WOEIDS)
            woeids[(*n)++] = (int)v;
        p = eq + 1;
    }
    if (*n)
        strcpy(kind, "woeid");
    else
        strcpy(kind, "");
    free(text);
    return *kind ? 0 : -1;
}

static int handle_weather_get(int fd, const struct request *r) {
    char *q = query_value(r->query, "q");

    if (!q)
        return reply_text(fd, 400, "Bad Request", "Invalid Request");
    char kind[16], term[128] = {0}, lang[32];
    int ids[MAX_WOEIDS];
    size_t nids = 0;
    double lat = 0, lon = 0;

    if (extract_query(q, kind, term, lang, ids, &nids, &lat, &lon) < 0) {
        free(q);
        return reply_text(fd, 400, "Bad Request", "Invalid Request");
    }
    free(q);
    struct place places[MAX_WOEIDS];
    size_t n = 0;

    if (!strcmp(kind, "search")) {
        if (geocode_search(term, lang, places, &n) < 0)
            n = 0;
        struct buffer b = {0};

        render_search(&b, places, n);
        int rc = send_response(fd, 200, "OK", "application/xml; charset=utf-8", b.data, b.len);

        buf_free(&b);
        return rc;
    }
    if (!strcmp(kind, "latlon")) {
        places[0] = (struct place){.woeid = coordinates_woeid(lat, lon), .lat = lat, .lon = lon};
        snprintf(places[0].name, sizeof places[0].name, "Current Location");
        if (place_resolve(places[0].woeid, lang, &places[0]) == 0)
            n = 1;
    } else
        for (size_t i = 0; i < nids; i++)
            if (place_resolve(ids[i], lang, &places[n++]) < 0)
                n--;
    struct wx weather[MAX_WOEIDS];
    size_t nw = 0;

    for (size_t i = 0; i < n; i++)
        if (weather_build(&places[i], &weather[nw]) == 0)
            nw++;
    if (!nw)
        return reply_text(fd, 404, "Not Found", "No results");
    struct buffer b = {0};

    render_weather(&b, weather, nw, "https://open-meteo.com");
    int rc = send_response(fd, 200, "OK", "application/xml; charset=utf-8", b.data, b.len);

    buf_free(&b);
    return rc;
}

static int xml_parse_body(const char *body, size_t n, xmlDocPtr *doc) {
    // Disable network access and DTD declarations when parsing client XML.
    if (n > BODY_LIMIT || contains_bytes(body, n, "<!DOCTYPE", 9) ||
        contains_bytes(body, n, "<!ENTITY", 8))
        return -1;
    *doc = xmlReadMemory(body,
                         (int)n,
                         "request.xml",
                         NULL,
                         XML_PARSE_NONET | XML_PARSE_NOERROR | XML_PARSE_NOWARNING |
                             XML_PARSE_COMPACT);
    return *doc ? 0 : -1;
}

static const char *xml_prop(xmlNodePtr node, const char *name) {
    xmlChar *v = xmlGetProp(node, (const xmlChar *)name);
    static __thread char values[8][256];
    static __thread int slot;

    slot = (slot + 1) % 8;
    snprintf(values[slot], sizeof values[slot], "%s", v ? (char *)v : "");
    if (v)
        xmlFree(v);
    return values[slot];
}

static void
xml_collect_text(xmlNodePtr node, const char *tag, char out[][64], size_t *count, size_t cap) {
    for (xmlNodePtr n = node; n && *count < cap; n = n->next) {
        if (n->type == XML_ELEMENT_NODE && !xmlStrcmp(n->name, (const xmlChar *)tag)) {
            xmlChar *v = xmlNodeGetContent(n);

            if (v) {
                snprintf(out[*count], 64, "%.63s", (char *)v);
                (*count)++;
                xmlFree(v);
            }
        }
        xml_collect_text(n->children, tag, out, count, cap);
    }
}

static void collect_ids(xmlNodePtr node, int *ids, size_t *count) {
    for (xmlNodePtr n = node; n; n = n->next) {
        if (n->type == XML_ELEMENT_NODE && !xmlStrcmp(n->name, (const xmlChar *)"id")) {
            xmlChar *v = xmlNodeGetContent(n);

            if (v) {
                char *s = (char *)v, *last = strrchr(s, '|');
                long x = strtol(last ? last + 1 : s, NULL, 10);

                if (x > 0 && *count < MAX_WOEIDS)
                    ids[(*count)++] = (int)x;
                xmlFree(v);
            }
        }
        collect_ids(n->children, ids, count);
    }
}

struct quote {
    char symbol[64], short_name[64], year_range[64];
    double price, open, previous, high, low, change;
    long long volume, average_volume, market_cap;
};

static struct json_object *
yahoo_chart(const char *symbol, const char *range, const char *interval) {
    CURL *curl = curl_easy_init();

    if (!curl)
        return NULL;
    char *escaped = curl_easy_escape(curl, symbol, 0);
    char url[2048], key[256];

    snprintf(url,
             sizeof url,
             "https://query1.finance.yahoo.com/v8/finance/chart/"
             "%s?range=%s&interval=%s&events=div%%2Csplits",
             escaped ? escaped : "",
             range,
             interval);
    snprintf(key, sizeof key, "yf:%s:%s:%s", symbol, range, interval);
    curl_free(escaped);
    curl_easy_cleanup(curl);
    return fetch_cached(key, url);
}

static struct json_object *yahoo_result(struct json_object *response) {
    struct json_object *chart = jget(response, "chart");
    struct json_object *results = jget(chart, "result");

    return jidx(results, 0);
}

static int load_quote(const char *symbol, struct quote *q, struct json_object **data) {
    *data = yahoo_chart(symbol, "1d", "1m");
    struct json_object *result = yahoo_result(*data);

    if (!result)
        return -1;
    struct json_object *meta = jget(result, "meta");

    snprintf(q->symbol, sizeof q->symbol, "%.63s", jstr(meta, "symbol", symbol));
    const char *name = jstr(meta, "longName", jstr(meta, "shortName", symbol));

    snprintf(q->short_name, sizeof q->short_name, "%.63s", name);
    if (strlen(q->short_name) > 12) {
        q->short_name[9] = '.';
        q->short_name[10] = '.';
        q->short_name[11] = '.';
        q->short_name[12] = 0;
    }
    q->price = jnum(meta, "regularMarketPrice", 0);
    q->previous = jnum(meta, "chartPreviousClose", jnum(meta, "previousClose", 0));
    q->open = jnum(meta, "regularMarketOpen", q->previous);
    q->high = jnum(meta, "regularMarketDayHigh", q->price);
    q->low = jnum(meta, "regularMarketDayLow", q->price);
    q->volume = (long long)jnum(meta, "regularMarketVolume", 0);
    struct json_object *indicators = jget(result, "indicators"),
                       *series = jidx(jget(indicators, "quote"), 0);
    struct json_object *opens = jget(series, "open"), *highs = jget(series, "high"),
                       *lows = jget(series, "low"), *volumes = jget(series, "volume");
    size_t points = jlength(opens);
    double high = 0, low = 0;
    int has_high = 0, has_low = 0;

    for (size_t i = 0; i < points; i++) {
        double value = jvalue_num(jidx(opens, i), 0);

        if (i == 0 && value)
            q->open = value;
        value = jvalue_num(jidx(highs, i), 0);
        if (value && (!has_high || value > high)) {
            high = value;
            has_high = 1;
        }
        value = jvalue_num(jidx(lows, i), 0);
        if (value && (!has_low || value < low)) {
            low = value;
            has_low = 1;
        }
        struct json_object *volume = jidx(volumes, i);

        if (volume && !json_object_is_type(volume, json_type_null))
            q->volume = json_object_get_int64(volume);
    }
    if (has_high && q->high == 0)
        q->high = high;
    if (has_low && q->low == 0)
        q->low = low;
    q->market_cap = (long long)jnum(meta, "marketCap", 0);
    q->average_volume = (long long)jnum(meta, "averageDailyVolume3Month", 0);
    q->change = q->previous != 0 ? (q->price - q->previous) / q->previous * 100.0 : 0;
    snprintf(q->year_range,
             sizeof q->year_range,
             "%.2f - %.2f",
             jnum(meta, "fiftyTwoWeekLow", 0),
             jnum(meta, "fiftyTwoWeekHigh", 0));
    return q->price || q->previous ? 0 : -1;
}

static void render_quotes(struct buffer *b, struct quote *quotes, size_t n) {
    xml_header(b);
    buf_printf(b,
               "<response><result type=\"getquotes\" timestamp=\"%lld\"><list count=\"%zu\" "
               "total=\"%zu\">",
               (long long)time(NULL),
               n,
               n);
    for (size_t i = 0; i < n; i++) {
        struct quote *q = &quotes[i];

        buf_add(b, "<quote><symbol>");
        xml_text(b, q->symbol);
        buf_add(b, "</symbol><sname>");
        xml_text(b, q->short_name);
        buf_printf(
            b,
            "</sname><open>%.4f</open><price>%.4f</price><change>%.2f%%</"
            "change><realtimechange>%.2f%%</realtimechange><changepercent>%.2f%%</"
            "changepercent><marketcap>%lld</marketcap><high>%.4f</high><low>%.4f</"
            "low><volume>%lld</volume><averagedailyvolume>%lld</averagedailyvolume><peratio>0</"
            "peratio><yearrange>%s</yearrange><dividendyield>0</dividendyield><link>https://"
            "open-meteo.com</link><status>0</status></quote>",
            q->open,
            q->price,
            q->change,
            q->change,
            q->change,
            q->market_cap,
            q->high,
            q->low,
            q->volume,
            q->average_volume,
            q->year_range);
    }
    buf_add(b, "</list></result></response>");
}

static int yahoo_news(const char *symbol, struct buffer *b) {
    CURL *curl = curl_easy_init();

    if (!curl)
        return -1;
    char *escaped = curl_easy_escape(curl, symbol, 0);
    char url[1024], key[128];

    snprintf(url,
             sizeof url,
             "https://query1.finance.yahoo.com/v1/finance/search?q=%s&newsCount=20&quotesCount=0",
             escaped ? escaped : "");
    snprintf(key, sizeof key, "yfnews:%s", symbol);
    curl_free(escaped);
    curl_easy_cleanup(curl);
    struct json_object *j = fetch_cached(key, url);

    if (!j)
        return -1;
    struct json_object *news = jget(j, "news");
    size_t count = jlength(news);

    if (count > 20)
        count = 20;
    xml_header(b);
    buf_printf(b, "<feed><list count=\"%zu\" total=\"%zu\">", count, count);
    for (size_t i = 0; i < count; i++) {
        struct json_object *item = jidx(news, i);
        const char *title = jstr(item, "title", ""), *link = jstr(item, "link", "");
        long long stamp = (long long)jnum(item, "providerPublishTime", 0);

        buf_printf(b, "<item id=\"%lld\"><title>", stamp);
        xml_text(b, title);
        buf_printf(b, "</title><timestamp>%lld</timestamp><link>", stamp);
        xml_text(b, link);
        buf_add(b, "</link></item>");
    }
    buf_add(b, "</list></feed>");
    json_object_put(j);
    return 0;
}

static int render_chart(const char *symbol, const char *range_key, struct buffer *b) {
    const char *range = "1d", *interval = "1m";

    if (!strcmp(range_key, "5d")) {
        range = "5d";
        interval = "5m";
    } else if (!strcmp(range_key, "1m")) {
        range = "1mo";
        interval = "15m";
    } else if (!strcmp(range_key, "3m")) {
        range = "3mo";
        interval = "1h";
    } else if (!strcmp(range_key, "6m")) {
        range = "6mo";
        interval = "1h";
    } else if (!strcmp(range_key, "1y")) {
        range = "1y";
        interval = "1d";
    } else if (!strcmp(range_key, "2y")) {
        range = "2y";
        interval = "1d";
    } else if (!strcmp(range_key, "5y")) {
        range = "5y";
        interval = "1wk";
    } else if (!strcmp(range_key, "10y")) {
        range = "10y";
        interval = "1wk";
    } else if (strcmp(range_key, "1d"))
        return -1;
    struct json_object *j = yahoo_chart(symbol, range, interval);
    struct json_object *result = yahoo_result(j);

    if (!result) {
        if (j)
            json_object_put(j);
        return -1;
    }
    struct json_object *meta = jget(result, "meta"), *timestamps = jget(result, "timestamp");
    struct json_object *indicators = jget(result, "indicators"),
                       *quotes = jget(indicators, "quote");
    struct json_object *q = jidx(quotes, 0), *closes = jget(q, "close"),
                       *volumes = jget(q, "volume");
    size_t total = jlength(timestamps), count = 0;

    for (size_t i = 0; i < total; i++)
        if (jidx(closes, i) && !json_object_is_type(jidx(closes, i), json_type_null))
            count++;
    xml_header(b);
    buf_printf(b,
               "<response><result type=\"getchart\" timestamp=\"%lld\"><list count=\"%zu\" "
               "total=\"%zu\"><meta><symbol>",
               (long long)time(NULL),
               count,
               count);
    xml_text(b, jstr(meta, "symbol", symbol));
    buf_printf(b,
               "</symbol><marketopen>%.4f</marketopen><marketclose>%.4f</"
               "marketclose><gmtoffset>%d</gmtoffset></meta>",
               jnum(meta, "regularMarketOpen", 0),
               jnum(meta, "chartPreviousClose", 0),
               (int)jnum(meta, "gmtoffset", 0));
    for (size_t i = 0; i < total; i++) {
        struct json_object *close = jidx(closes, i);

        if (!close || json_object_is_type(close, json_type_null))
            continue;
        long long stamp = (int64_t)jvalue_num(jidx(timestamps, i), 0);
        long long volume = 0;
        struct json_object *v = jidx(volumes, i);

        if (v && !json_object_is_type(v, json_type_null))
            volume = (int64_t)jvalue_num(v, 0);
        buf_printf(b,
                   "<point timestamp=\"%lld\" close=\"%.4f\" volume=\"%lld\"/>",
                   stamp,
                   jvalue_num(close, 0),
                   volume);
    }
    buf_add(b, "</list></result></response>");
    json_object_put(j);
    return count ? 0 : -1;
}

static int handle_finance(int fd, const struct request *r, xmlNodePtr root, xmlNodePtr query) {
    char symbols[MAX_SYMBOLS][64] = {{0}}, ranges[1][64] = {{0}};
    size_t ns = 0, nr = 0;

    xml_collect_text(root, "symbol", symbols, &ns, MAX_SYMBOLS);
    xml_collect_text(root, "phrase", symbols, &ns, MAX_SYMBOLS);
    xml_collect_text(root, "range", ranges, &nr, 1);
    if (!ns)
        return reply_text(fd, 400, "Bad Request", "No symbols");
    const char *type = xml_prop(query, "type");
    struct buffer b = {0};
    int status = 200;

    if (!strcmp(type, "getchart")) {
        if (render_chart(symbols[0], nr ? ranges[0] : "1d", &b) < 0)
            status = 404;
    } else if (!strcmp(type, "getnews")) {
        if (yahoo_news(symbols[0], &b) < 0)
            status = 404;
    } else if (!strcmp(type, "getquotes") || !strcmp(type, "getsymbol")) {
        struct quote quotes[MAX_SYMBOLS];
        size_t count = 0;

        for (size_t i = 0; i < ns; i++) {
            struct json_object *data = NULL;
            struct quote q = {0};

            if (load_quote(symbols[i], &q, &data) == 0)
                quotes[count++] = q;
            if (data)
                json_object_put(data);
        }
        if (!count)
            status = 404;
        else
            render_quotes(&b, quotes, count);
    } else
        return reply_text(fd, 400, "Bad Request", "Unsupported request type");
    if (status != 200) {
        buf_free(&b);
        return reply_text(fd, status, "Not Found", "Unknown ticker or range");
    }
    int rc = send_response(fd, 200, "OK", "application/xml; charset=utf-8", b.data, b.len);

    buf_free(&b);
    (void)r;
    return rc;
}

static int handle_dgw(int fd, const struct request *r) {
    xmlDocPtr doc = NULL;

    if (xml_parse_body(r->body, r->body_len, &doc) < 0)
        return reply_text(fd, 400, "Bad Request", "Malformed XML");
    xmlNodePtr root = xmlDocGetRootElement(doc);

    if (!root || !root->children) {
        xmlFreeDoc(doc);
        return reply_text(fd, 400, "Bad Request", "Empty request");
    }
    const char *api = xml_prop(root, "api");
    xmlNodePtr query = root->children;

    while (query && query->type != XML_ELEMENT_NODE)
        query = query->next;
    if (!query) {
        xmlFreeDoc(doc);
        return reply_text(fd, 400, "Bad Request", "Empty request");
    }
    const char *type = xml_prop(query, "type");
    char lang[32] = "en-US", langs[1][64];
    size_t nlang = 0;

    xml_collect_text(query, "lang", langs, &nlang, 1);
    if (nlang)
        snprintf(lang, sizeof lang, "%.31s", langs[0]);
    if (!strcmp(api, "weather")) {
        if (!strcmp(type, "getlocationid")) {
            char terms[1][64];
            size_t nt = 0;

            xml_collect_text(query ? query->children : NULL, "query", terms, &nt, 1);
            struct place places[10];
            size_t n = 0;

            if (nt) {
                char term[64];

                snprintf(term, sizeof term, "%s", terms[0]);
                geocode_search(term, lang, places, &n);
            }
            struct buffer b = {0};

            render_dgw_weather_search(&b, places, n);
            int rc = send_response(fd, 200, "OK", "application/xml; charset=utf-8", b.data, b.len);

            buf_free(&b);
            xmlFreeDoc(doc);
            return rc;
        }
        if (strcmp(type, "getforecastbylocationid")) {
            xmlFreeDoc(doc);
            return reply_text(fd, 400, "Bad Request", "Unsupported request type");
        }
        int ids[MAX_WOEIDS];
        size_t ni = 0;

        collect_ids(query, ids, &ni);
        struct wx weather[MAX_WOEIDS];
        size_t nw = 0;

        for (size_t i = 0; i < ni; i++) {
            struct place p;

            if (place_resolve(ids[i], lang, &p) == 0 && weather_build(&p, &weather[nw]) == 0)
                nw++;
        }
        if (!nw) {
            xmlFreeDoc(doc);
            return reply_text(fd, 404, "Not Found", "No results");
        }
        struct buffer b = {0};

        render_dgw_weather(&b, weather, nw, "https://open-meteo.com");
        int rc = send_response(fd, 200, "OK", "application/xml; charset=utf-8", b.data, b.len);

        buf_free(&b);
        xmlFreeDoc(doc);
        return rc;
    }
    if (!strcmp(api, "finance")) {
        int rc = handle_finance(fd, r, root, query);

        xmlFreeDoc(doc);
        return rc;
    }
    xmlFreeDoc(doc);
    return reply_text(fd, 400, "Bad Request", "Unsupported api");
}

static void handle_client(int fd, const char *peer) {
    struct request r = {0};
    int status = 500;
    int parsed = read_request(fd, peer, &r);

    if (parsed < 0) {
        status = parsed == -2 ? 413 : 400;
        reply_text(fd,
                   status,
                   status == 413 ? "Payload Too Large" : "Bad Request",
                   status == 413 ? "Request too large" : "Bad Request");
    } else if (!rate_allow(r.client)) {
        status = reply_text(fd, 429, "Too Many Requests", "Too Many Requests");
    } else if (!strcmp(r.path, "/dgw") && !strcmp(r.method, "GET")) {
        status = send_response(fd, 200, "OK", "text/plain; charset=utf-8", "ok", 2);
    } else if (!strcmp(r.path, "/") && !strcmp(r.method, "GET")) {
        const char *body = "{\"name\":\"yql-lite\",\"weather_yql\":\"GET "
                           "/yql/weather?q=<yql>\",\"weather_dgw\":\"POST /dgw "
                           "(api=weather)\",\"stocks\":\"POST /dgw (api=finance)\"}";

        status = send_response(fd, 200, "OK", "application/json", body, strlen(body));
    } else if ((!strcmp(r.path, "/yql/weather") || !strcmp(r.path, "/v1/yql")) &&
               !strcmp(r.method, "GET"))
        status = handle_weather_get(fd, &r);
    else if ((!strcmp(r.path, "/dgw") || !strcmp(r.path, "/yql/weather/dgw")) &&
             !strcmp(r.method, "POST"))
        status = handle_dgw(fd, &r);
    else if (!strcmp(r.method, "GET") || !strcmp(r.method, "POST") || !strcmp(r.method, "HEAD")) {
        status = reply_text(fd, 404, "Not Found", "Not Found") < 0 ? 500 : 404;
    } else {
        status = reply_text(fd, 405, "Method Not Allowed", "Method Not Allowed") < 0 ? 500 : 405;
    }
    log_request(&r, status);
}

static void *worker(void *unused) {
    (void)unused;
    for (;;) {
        pthread_mutex_lock(&queue_lock);
        while (!queue_count && !stopping)
            pthread_cond_wait(&queue_ready, &queue_lock);
        if (!queue_count && stopping) {
            pthread_mutex_unlock(&queue_lock);
            return NULL;
        }
        struct job j = queue[queue_head];

        queue_head = (queue_head + 1) % QUEUE_SIZE;
        queue_count--;
        pthread_mutex_unlock(&queue_lock);
        handle_client(j.fd, j.peer);
        close(j.fd);
    }
    return NULL;
}

static void on_signal(int sig) {
    (void)sig;
    stopping = 1;
    if (listen_fd >= 0)
        close(listen_fd);
}

int main(void) {
    load_env();
    if (curl_global_init(CURL_GLOBAL_DEFAULT) != CURLE_OK) {
        fputs("curl init failed\n", stderr);
        return 1;
    }
    xmlInitParser();
    if (sqlite3_open("places.db", &db) != SQLITE_OK) {
        fprintf(stderr, "sqlite: %s\n", sqlite3_errmsg(db));
        return 1;
    }
    sqlite3_busy_timeout(db, 5000);
    sqlite3_exec(db,
                 "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; CREATE TABLE IF NOT EXISTS "
                 "places (woeid INTEGER PRIMARY KEY,name TEXT NOT NULL,country_code TEXT NOT "
                 "NULL,state TEXT NOT NULL,latitude REAL NOT NULL,longitude REAL NOT NULL)",
                 NULL,
                 NULL,
                 NULL);
    signal(SIGPIPE, SIG_IGN);
    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);
    int fd = socket(AF_INET6, SOCK_STREAM, 0);

    if (fd < 0) {
        perror("socket");
        return 1;
    }
    listen_fd = fd;
    int yes = 1, no = 0;

    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof yes);
    setsockopt(fd, IPPROTO_IPV6, IPV6_V6ONLY, &no, sizeof no);
    struct sockaddr_in6 addr = {0};

    addr.sin6_family = AF_INET6;
    addr.sin6_addr = in6addr_any;
    addr.sin6_port = htons((uint16_t)port);
    if (bind(fd, (struct sockaddr *)&addr, sizeof addr) < 0 || listen(fd, 128) < 0) {
        perror("listen");
        return 1;
    }
    // A fixed worker pool bounds concurrent request memory and socket work.
    pthread_t threads[THREAD_COUNT];

    for (size_t i = 0; i < THREAD_COUNT; i++)
        if (pthread_create(&threads[i], NULL, worker, NULL) != 0) {
            fputs("worker creation failed\n", stderr);
            return 1;
        }
    printf("yql-lite listening on :%u\n", port);
    fflush(stdout);
    while (!stopping) {
        struct sockaddr_in6 client;
        socklen_t len = sizeof client;
        int conn = accept(fd, (struct sockaddr *)&client, &len);

        if (conn < 0) {
            if (errno == EINTR)
                continue;
            if (stopping)
                break;
            perror("accept");
            continue;
        }
        // Format mapped IPv4 peers as dotted decimal for concise request logs.
        char ip[INET6_ADDRSTRLEN] = "?";

        if (IN6_IS_ADDR_V4MAPPED(&client.sin6_addr))
            inet_ntop(AF_INET, &client.sin6_addr.s6_addr[12], ip, sizeof ip);
        else
            inet_ntop(AF_INET6, &client.sin6_addr, ip, sizeof ip);
        pthread_mutex_lock(&queue_lock);
        if (queue_count == QUEUE_SIZE) {
            pthread_mutex_unlock(&queue_lock);
            reply_text(conn, 503, "Service Unavailable", "Busy");
            close(conn);
            continue;
        }
        queue[queue_tail].fd = conn;
        snprintf(queue[queue_tail].peer, sizeof queue[queue_tail].peer, "%s", ip);
        queue_tail = (queue_tail + 1) % QUEUE_SIZE;
        queue_count++;
        pthread_cond_signal(&queue_ready);
        pthread_mutex_unlock(&queue_lock);
    }
    stopping = 1;
    pthread_cond_broadcast(&queue_ready);
    for (size_t i = 0; i < THREAD_COUNT; i++)
        pthread_join(threads[i], NULL);
    for (size_t i = 0; i < CACHE_SIZE; i++)
        free(cache[i].json);
    sqlite3_close(db);
    xmlCleanupParser();
    curl_global_cleanup();
    return 0;
}
