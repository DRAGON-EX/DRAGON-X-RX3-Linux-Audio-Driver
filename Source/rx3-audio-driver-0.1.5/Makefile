CC ?= cc
CFLAGS ?= -O2 -g
CFLAGS += -std=c11 -Wall -Wextra -Werror -fPIC -DPIC
CPPFLAGS += $(shell pkg-config --cflags alsa libusb-1.0 json-c)
LDLIBS += $(shell pkg-config --libs alsa libusb-1.0 json-c) -pthread

all: libasound_module_pcm_rx3.so test_pcm test_poll test_ring test_drain

libasound_module_pcm_rx3.so: pcm_rx3.o rx3_transport.o
	$(CC) $(LDFLAGS) -shared -o $@ $^ $(LDLIBS)

test_pcm: test_pcm.o
	$(CC) $(LDFLAGS) -o $@ $^ $(LDLIBS)

clean:
	rm -f *.o libasound_module_pcm_rx3.so test_pcm test_poll test_ring test_drain

.PHONY: all clean

test_poll: test_poll.o
	$(CC) $(LDFLAGS) -o $@ $^ $(LDLIBS)

test_ring: test_ring.o
	$(CC) $(LDFLAGS) -o $@ $^ $(LDLIBS)

test_ring.o: test_ring.c pcm_rx3.c rx3_transport.h

test_drain: test_drain.o
	$(CC) $(LDFLAGS) -o $@ $^ $(LDLIBS)
