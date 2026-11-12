CC ?= cc
PKG_CONFIG ?= pkg-config
CFLAGS ?= -O2
CFLAGS += -std=c11 -Wall -Wextra -Wpedantic -D_POSIX_C_SOURCE=200809L $(shell $(PKG_CONFIG) --cflags libcurl json-c libxml-2.0 sqlite3)
LDLIBS += $(shell $(PKG_CONFIG) --libs libcurl json-c libxml-2.0 sqlite3) -lm -pthread

.PHONY: all run selftest clean

all: server

server: server.c
	$(CC) $(CFLAGS) -o $@ $< $(LDLIBS)

run: server
	./server

selftest: server
	./selftest.sh

clean:
	rm -f server
