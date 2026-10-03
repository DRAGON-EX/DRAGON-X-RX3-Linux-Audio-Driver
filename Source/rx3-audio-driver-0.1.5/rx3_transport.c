/* SPDX-License-Identifier: GPL-3.0-or-later */
#define _POSIX_C_SOURCE 200809L
#include "rx3_transport.h"
#include <libusb.h>
#include <pthread.h>
#include <stdatomic.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <sched.h>

#define OUT_COUNT 4
#define IN_COUNT 4
#define MAX_FRAMES 48
#define IN_BYTES 384
struct slot {
    struct rx3_transport *owner;
    struct libusb_transfer *transfer;
    unsigned char data[IN_BYTES * 2];
    unsigned consumed;
    int active;
};
struct rx3_transport {
    libusb_context *ctx;
    libusb_device_handle *device;
    struct slot output[OUT_COUNT], input[IN_COUNT];
    pthread_t thread;
    atomic_int running, error;
    int claimed, active;
    unsigned quotas[256], head, tail;
    rx3_fill_fn fill;
    rx3_done_fn done;
    void *user;
    FILE *offline;
};

int rx3_pack(unsigned char *dst, const unsigned char *src, unsigned frames)
{
    for (unsigned i = 0; i < frames * 4; ++i) {
        if (src[i * 4] != 0) return -EINVAL;
    }
    /* Match source+1 shift with an explicitly zero-filled block end. */
    if (frames) {
        memcpy(dst, src + 1, (size_t)frames * 16 - 1);
        dst[(size_t)frames * 16 - 1] = 0;
    }
    return 0;
}

static void fail(struct rx3_transport *t, int error)
{
    int expected = 0;
    atomic_compare_exchange_strong(&t->error, &expected, error);
    atomic_store(&t->running, 0);
    /* Wake the ALSA poller on errors as well as successful completions. */
    t->done(t->user, 0);
}

static int submit(struct slot *s)
{
    int rc = libusb_submit_transfer(s->transfer);
    if (rc < 0) { fail(s->owner, -EIO); return rc; }
    s->active = 1;
    ++s->owner->active;
    return 0;
}

static void pump(struct rx3_transport *t)
{
    for (unsigned i = 0; i < OUT_COUNT && t->head != t->tail && atomic_load(&t->running); ++i) {
        struct slot *s = &t->output[i];
        if (s->active) continue;
        unsigned frames = t->quotas[t->head++ % 256];
        unsigned char pcm[MAX_FRAMES * 16];
        memset(pcm, 0, sizeof(pcm));
        int consumed = t->fill(t->user, pcm, frames);
        if (consumed < 0) { fail(t, consumed); return; }
        if ((unsigned)consumed > frames || rx3_pack(s->data, pcm, frames) < 0) {
            fail(t, -EINVAL); return;
        }
        s->consumed = (unsigned)consumed;
        s->transfer->length = (int)frames * 16;
        s->transfer->iso_packet_desc[0].length = frames * 16;
        if (submit(s) < 0) return;
    }
}

static void LIBUSB_CALL complete(struct libusb_transfer *x)
{
    struct slot *s = x->user_data;
    struct rx3_transport *t = s->owner;
    s->active = 0;
    --t->active;
    if (!atomic_load(&t->running)) return;
    if (x->status != LIBUSB_TRANSFER_COMPLETED) { fail(t, -EIO); return; }
    for (int i = 0; i < x->num_iso_packets; ++i) {
        if (x->iso_packet_desc[i].status != LIBUSB_TRANSFER_COMPLETED) {
            fail(t, -EIO); return;
        }
    }
    if (x->endpoint == 1) {
        t->done(t->user, s->consumed);
    } else {
        for (int i = 0; i < x->num_iso_packets; ++i) {
            unsigned length = x->iso_packet_desc[i].actual_length;
            if (!length) continue;
            /* In capture: 352/360 bytes = 44/45 stereo 32-bit input frames.
             * Using these lengths as implicit feedback remains a hardware hypothesis. */
            if (length % 8 || length / 8 < 40 || length / 8 > MAX_FRAMES) {
                fail(t, -EPROTO); return;
            }
            if (t->tail - t->head >= 256) { fail(t, -EOVERFLOW); return; }
            t->quotas[t->tail++ % 256] = length / 8;
        }
        if (submit(s) < 0) return;
    }
    pump(t);
}

static void *worker(void *arg)
{
    struct rx3_transport *t = arg;
    if (t->offline) {
        unsigned phase = 0, next = 0, count = 0;
        unsigned pending[OUT_COUNT] = {0};
        struct timespec until;
        clock_gettime(CLOCK_MONOTONIC, &until);
        /* Model four queued OUT packets: startup fills four slots, then the
         * oldest completes after 4 ms and subsequent completions every 1 ms.
         * Captured bytes are submissions, just like the hardware USB trace. */
        while (atomic_load(&t->running)) {
            if (count == OUT_COUNT) {
                until.tv_nsec += next == 0 ? 4000000 : 1000000;
                if (until.tv_nsec >= 1000000000) {
                    ++until.tv_sec; until.tv_nsec -= 1000000000;
                }
                int rc;
                do { rc = clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &until, NULL); }
                while (rc == EINTR && atomic_load(&t->running));
                if (!atomic_load(&t->running)) break;
                if (rc && rc != EINTR) { fail(t, -EIO); break; }
                t->done(t->user, pending[next % OUT_COUNT]);
                ++next;
                --count;
            }
            phase += 44100;
            unsigned frames = phase / 1000;
            phase %= 1000;
            unsigned char pcm[MAX_FRAMES * 16] = {0}, usb[MAX_FRAMES * 16];
            int used = t->fill(t->user, pcm, frames);
            if (used < 0) { fail(t, used); break; }
            if ((unsigned)used > frames || rx3_pack(usb, pcm, frames) < 0) {
                fail(t, -EINVAL); break;
            }
            if (fwrite(usb, 16, frames, t->offline) != frames) {
                fail(t, -EIO); break;
            }
            pending[(next + count) % OUT_COUNT] = (unsigned)used;
            ++count;
        }
        return NULL;
    }
    /* The USB producer must not remain a normal-priority worker while its
     * PipeWire consumer runs FIFO. Failure is explicit and non-fatal. */
    struct sched_param priority = {.sched_priority = 20};
    int scheduler_error = pthread_setschedparam(pthread_self(), SCHED_FIFO, &priority);
    if (scheduler_error)
        fprintf(stderr, "rx3: USB real-time priority unavailable: %s\n", strerror(scheduler_error));
    for (unsigned i = 0; i < OUT_COUNT; ++i) t->quotas[t->tail++ % 256] = 44;
    pump(t);
    /* ALSA's existing Pioneer implicit-feedback handling starts playback first. */
    for (unsigned i = 0; i < IN_COUNT && atomic_load(&t->running); ++i) submit(&t->input[i]);
    while (atomic_load(&t->running)) {
        struct timeval wait = {.tv_sec = 0, .tv_usec = 20000};
        int rc = libusb_handle_events_timeout(t->ctx, &wait);
        if (rc < 0 && rc != LIBUSB_ERROR_INTERRUPTED) fail(t, -EIO);
    }
    for (unsigned i = 0; i < IN_COUNT; ++i)
        if (t->input[i].active) libusb_cancel_transfer(t->input[i].transfer);
    for (unsigned i = 0; i < OUT_COUNT; ++i)
        if (t->output[i].active) libusb_cancel_transfer(t->output[i].transfer);
    /* libusb owns transfers until their cancellation callbacks return. */
    while (t->active) {
        struct timeval wait = {.tv_sec = 0, .tv_usec = 20000};
        libusb_handle_events_timeout(t->ctx, &wait);
    }
    return NULL;
}

static void dispose(struct rx3_transport *t)
{
    for (unsigned i = 0; i < IN_COUNT; ++i) libusb_free_transfer(t->input[i].transfer);
    for (unsigned i = 0; i < OUT_COUNT; ++i) libusb_free_transfer(t->output[i].transfer);
    if (t->claimed) {
        libusb_set_interface_alt_setting(t->device, 0, 0);
        libusb_release_interface(t->device, 0);
    }
    if (t->device) libusb_close(t->device);
    if (t->ctx) libusb_exit(t->ctx);
    if (t->offline) fclose(t->offline);
    free(t);
}

static int validate_device(struct rx3_transport *t)
{
    libusb_device *dev = libusb_get_device(t->device);
    if (libusb_get_device_speed(dev) != LIBUSB_SPEED_HIGH) return -EPROTO;
    struct libusb_config_descriptor *cfg = NULL;
    if (libusb_get_active_config_descriptor(dev, &cfg) < 0) return -EIO;
    int match = 0;
    if (cfg->bConfigurationValue == 1) {
        for (unsigned i = 0; i < cfg->bNumInterfaces; ++i) {
            for (int j = 0; j < cfg->interface[i].num_altsetting; ++j) {
                const struct libusb_interface_descriptor *a = &cfg->interface[i].altsetting[j];
                if (a->bInterfaceNumber != 0 || a->bAlternateSetting != 1 ||
                    a->bInterfaceClass != 255 || a->bNumEndpoints != 2) continue;
                int out = 0, in = 0;
                for (unsigned k = 0; k < a->bNumEndpoints; ++k) {
                    const struct libusb_endpoint_descriptor *e = &a->endpoint[k];
                    if (e->bmAttributes != 5 || e->wMaxPacketSize != 1024 || e->bInterval != 4) continue;
                    out |= e->bEndpointAddress == 1;
                    in |= e->bEndpointAddress == 0x82;
                }
                match |= out && in;
            }
        }
    }
    libusb_free_config_descriptor(cfg);
    return match ? 0 : -EPROTO;
}

int rx3_start(struct rx3_transport **out, rx3_fill_fn fill, rx3_done_fn done,
              void *user, const char *offline_path)
{
    *out = NULL;
    struct rx3_transport *t = calloc(1, sizeof(*t));
    if (!t) return -ENOMEM;
    t->fill = fill; t->done = done; t->user = user;
    int rc = -EIO;
    if (offline_path && *offline_path) {
        /* Refuse overwrite: test captures must always have a fresh destination. */
        t->offline = fopen(offline_path, "wbx");
        if (!t->offline) { rc = -errno; goto error; }
    } else {
        if (libusb_init(&t->ctx) < 0) goto error;
        t->device = libusb_open_device_with_vid_pid(t->ctx, 0x2b73, 0x003d);
        if (!t->device) { rc = -ENODEV; goto error; }
        rc = validate_device(t);
        if (rc < 0) goto error;
        /* Do not detach any bound kernel driver automatically. */
        if (libusb_kernel_driver_active(t->device, 0) == 1) { rc = -EBUSY; goto error; }
        if (libusb_claim_interface(t->device, 0) < 0) { rc = -EBUSY; goto error; }
        t->claimed = 1;
        if (libusb_set_interface_alt_setting(t->device, 0, 1) < 0) { rc = -EIO; goto error; }
        unsigned char rate[] = {0x44, 0xac, 0};
        if (libusb_control_transfer(t->device, 0x22, 1, 0x0100, 0x0082,
                                    rate, sizeof(rate), 1000) != 3) { rc = -EIO; goto error; }
        for (unsigned i = 0; i < IN_COUNT + OUT_COUNT; ++i) {
            int input = i >= OUT_COUNT;
            struct slot *s = input ? &t->input[i-OUT_COUNT] : &t->output[i];
            s->owner = t;
            s->transfer = libusb_alloc_transfer(input ? 2 : 1);
            if (!s->transfer) { rc = -ENOMEM; goto error; }
            libusb_fill_iso_transfer(s->transfer, t->device, input ? 0x82 : 1,
                s->data, input ? IN_BYTES * 2 : 704, input ? 2 : 1, complete, s, 1000);
            libusb_set_iso_packet_lengths(s->transfer, input ? IN_BYTES : 704);
        }
    }
    atomic_store(&t->running, 1);
    /* Publish before the worker can call done(), including on startup failure. */
    *out = t;
    rc = pthread_create(&t->thread, NULL, worker, t);
    if (rc) { *out = NULL; rc = -rc; goto error; }
    return 0;
error:
    dispose(t);
    return rc;
}

int rx3_error(struct rx3_transport *t) { return t ? atomic_load(&t->error) : 0; }
void rx3_stop(struct rx3_transport *t)
{
    if (!t) return;
    atomic_store(&t->running, 0);
    pthread_join(t->thread, NULL);
    dispose(t);
}
