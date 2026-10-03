/* SPDX-License-Identifier: GPL-3.0-or-later */
#define _POSIX_C_SOURCE 200809L
#include <alsa/asoundlib.h>
#include <alsa/pcm_external.h>
#include "rx3_transport.h"
#include <pthread.h>
#include <sys/eventfd.h>
#include <unistd.h>
#include <errno.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <limits.h>
#include <json-c/json.h>

struct rx3_pcm {
    snd_pcm_ioplug_t io;
    pthread_mutex_t mutex;
    unsigned char *ring;
    /* Lifetime frame counters, protected by mutex:
     * completed <= reserved <= written. reserved frames have been copied into
     * USB-owned storage; completed frames have left the pending USB queue.
     * A producer may reuse copied slots before USB completion. Conflating these
     * counters delayed wakeups until there was too little refill time. */
    uint64_t written, reserved, completed;
    snd_pcm_uframes_t avail_min;
    int draining;
    struct rx3_transport *transport;
    char *offline;
    unsigned native_period;
};

/* The direct ALSA endpoint reads only the native Linux preference at open.
 * The generic "rx3" endpoint used by PipeWire never reads this preference,
 * so changing Linux settings cannot constrain the ASIO/WASAPI graph.
 * JSON parsing uses json-c, not a partial ad-hoc parser or shell command. */
static int native_period(void)
{
    char path[PATH_MAX];
    const char *config = getenv("XDG_CONFIG_HOME");
    const char *home = getenv("HOME");
    int len;
    if (config && *config)
        len = snprintf(path, sizeof(path), "%s/rx3-audio-driver/settings.json", config);
    else if (home && *home)
        len = snprintf(path, sizeof(path), "%s/.config/rx3-audio-driver/settings.json", home);
    else return 512;
    if (len < 0 || (size_t)len >= sizeof(path)) return -ENAMETOOLONG;
    errno = 0;
    struct json_object *doc = json_object_from_file(path), *buffers, *value;
    if (!doc) return errno == ENOENT ? 512 : -EINVAL;
    int period = -EINVAL;
    if (json_object_is_type(doc, json_type_object) &&
        json_object_object_get_ex(doc, "buffers", &buffers) &&
        json_object_is_type(buffers, json_type_object)) {
        if (!json_object_object_get_ex(buffers, "linux", &value)) period = 512;
        else if (json_object_is_type(value, json_type_int)) {
            int64_t size = json_object_get_int64(value);
            if (size == 64 || size == 128 || size == 256 || size == 512 ||
                size == 1024 || size == 2048) period = (int)size;
        }
    }
    json_object_put(doc);
    return period;
}

static struct rx3_pcm *state(snd_pcm_ioplug_t *io) { return io->private_data; }
/* USB slots own a copy after fill(). Reuse the ALSA ring immediately, but
 * retain one frame until the last queued transfer completes. ALSA otherwise
 * auto-drops a nonblocking drain when its hardware pointer reaches appl_ptr,
 * even though USB still owns the final packets. Call with mutex held. */
static uint64_t ring_position(struct rx3_pcm *p)
{
    return p->reserved - (p->reserved > p->completed ? 1 : 0);
}
/* Call with mutex held. USB completion alone does not imply one ALSA period
 * is writable: an RX3 packet contains only 44/45 frames. */
static int writable(struct rx3_pcm *p)
{
    uint64_t queued = p->written - ring_position(p);
    return queued <= p->io.buffer_size &&
        p->io.buffer_size - queued >= p->avail_min;
}

static void notify(struct rx3_pcm *p)
{
    uint64_t one = 1;
    ssize_t ignored = write(p->io.poll_fd, &one, sizeof(one));
    (void)ignored;
}
static int fill(void *user, unsigned char *pcm, unsigned frames)
{
    struct rx3_pcm *p = user;
    pthread_mutex_lock(&p->mutex);
    uint64_t available = p->written - p->reserved;
    if (available < frames && !p->draining) {
        pthread_mutex_unlock(&p->mutex); return -EPIPE;
    }
    unsigned n = available < frames ? (unsigned)available : frames;
    for (unsigned i = 0; i < n; ++i) {
        memcpy(pcm + i*16, p->ring + ((p->reserved+i) % p->io.buffer_size)*16, 16);
    }
    p->reserved += n;
    if (writable(p)) notify(p);
    pthread_mutex_unlock(&p->mutex);
    return (int)n;
}

static void done(void *user, unsigned frames)
{
    struct rx3_pcm *p = user;
    pthread_mutex_lock(&p->mutex);
    p->completed += frames;
    if (!frames || p->draining || writable(p)) notify(p);
    pthread_mutex_unlock(&p->mutex);
}

static int start(snd_pcm_ioplug_t *io)
{
    struct rx3_pcm *p = state(io);
    if (p->transport) return -EBUSY;
    return rx3_start(&p->transport, fill, done, p, p->offline);
}

static int stop(snd_pcm_ioplug_t *io)
{
    struct rx3_pcm *p = state(io);
    rx3_stop(p->transport);
    p->transport = NULL;
    return 0;
}

static snd_pcm_sframes_t pointer(snd_pcm_ioplug_t *io)
{
    struct rx3_pcm *p = state(io);
    int error = rx3_error(p->transport);
    if (error) return error;
    pthread_mutex_lock(&p->mutex);
    snd_pcm_sframes_t pos = (snd_pcm_sframes_t)(ring_position(p) % io->buffer_size);
    pthread_mutex_unlock(&p->mutex);
    return pos;
}

static snd_pcm_sframes_t transfer(snd_pcm_ioplug_t *io,
    const snd_pcm_channel_area_t *areas, snd_pcm_uframes_t offset,
    snd_pcm_uframes_t frames)
{
    struct rx3_pcm *p = state(io);
    for (unsigned ch = 0; ch < 4; ++ch)
        if (areas[ch].first % 8 || areas[ch].step % 8) return -EINVAL;
    pthread_mutex_lock(&p->mutex);
    if (p->written - ring_position(p) + frames > io->buffer_size) {
        pthread_mutex_unlock(&p->mutex); return -EAGAIN;
    }
    for (snd_pcm_uframes_t i = 0; i < frames; ++i) {
        unsigned char *dst = p->ring + ((p->written+i) % io->buffer_size)*16;
        for (unsigned ch = 0; ch < 4; ++ch) {
            const unsigned char *src = (const unsigned char *)areas[ch].addr +
                areas[ch].first/8 + (offset+i)*(areas[ch].step/8);
            dst[ch*4] = 0;
            if (io->format == SND_PCM_FORMAT_S32_LE) ++src;
            memcpy(dst + ch*4 + 1, src, 3);
        }
    }
    p->written += frames;
    if (writable(p)) notify(p);
    pthread_mutex_unlock(&p->mutex);
    return (snd_pcm_sframes_t)frames;
}

static int hw_params(snd_pcm_ioplug_t *io, snd_pcm_hw_params_t *params)
{
    (void)params;
    struct rx3_pcm *p = state(io);
    p->avail_min = io->period_size;
    free(p->ring);
    p->ring = calloc(io->buffer_size, 16);
    return p->ring ? 0 : -ENOMEM;
}

static int prepare(snd_pcm_ioplug_t *io)
{
    struct rx3_pcm *p = state(io);
    stop(io);
    pthread_mutex_lock(&p->mutex);
    p->written = p->reserved = p->completed = 0;
    p->draining = 0;
    pthread_mutex_unlock(&p->mutex);
    uint64_t value;
    while (read(io->poll_fd, &value, sizeof(value)) > 0) {}
    pthread_mutex_lock(&p->mutex);
    if (writable(p)) notify(p);
    pthread_mutex_unlock(&p->mutex);
    return 0;
}

static int sw_params(snd_pcm_ioplug_t *io, snd_pcm_sw_params_t *params)
{
    struct rx3_pcm *p = state(io);
    snd_pcm_uframes_t minimum;
    int rc = snd_pcm_sw_params_get_avail_min(params, &minimum);
    if (rc < 0) return rc;
    pthread_mutex_lock(&p->mutex);
    p->avail_min = minimum ? minimum : 1;
    if (writable(p)) notify(p);
    pthread_mutex_unlock(&p->mutex);
    return 0;
}

static int drain(snd_pcm_ioplug_t *io)
{
    struct rx3_pcm *p = state(io);
    pthread_mutex_lock(&p->mutex);
    p->draining = 1;
    uint64_t target = p->written;
    pthread_mutex_unlock(&p->mutex);
    if (!target) return stop(io);
    if (!p->transport) {
        int rc = start(io);
        if (rc < 0) return rc;
    }
    for (;;) {
        int error = rx3_error(p->transport);
        if (error) { stop(io); return error; }
        pthread_mutex_lock(&p->mutex);
        int finished = p->completed >= target;
        pthread_mutex_unlock(&p->mutex);
        if (finished) return stop(io);
        if (io->nonblock) return -EAGAIN;
        struct pollfd fd = {.fd = io->poll_fd, .events = POLLIN};
        int rc = poll(&fd, 1, 2000);
        if (rc < 0 && errno != EINTR) { stop(io); return -errno; }
        if (rc == 0) { stop(io); return -ETIMEDOUT; }
        uint64_t value;
        if (rc > 0) {
            ssize_t ignored = read(io->poll_fd, &value, sizeof(value));
            (void)ignored;
        }
    }
}

static int poll_revents(snd_pcm_ioplug_t *io, struct pollfd *fds,
                       unsigned nfds, unsigned short *events)
{
    if (nfds != 1) return -EINVAL;
    struct rx3_pcm *p = state(io);
    *events = fds[0].revents & (POLLERR | POLLHUP | POLLNVAL);
    if (fds[0].revents & POLLIN) {
        uint64_t value;
        ssize_t ignored = read(io->poll_fd, &value, sizeof(value));
        (void)ignored;
        if (rx3_error(p->transport)) {
            *events |= POLLERR;
        } else {
            pthread_mutex_lock(&p->mutex);
            if (writable(p)) {
                *events |= POLLOUT;
                /* Preserve level-triggered readiness until the caller writes. */
                notify(p);
            }
            pthread_mutex_unlock(&p->mutex);
        }
    }
    return 0;
}

static int delay(snd_pcm_ioplug_t *io, snd_pcm_sframes_t *frames)
{
    struct rx3_pcm *p = state(io);
    pthread_mutex_lock(&p->mutex);
    /* Unlike ring_position(), delay must include the pending USB frames.
     * This is queued host audio, not a measurement of the RX3 analog latency. */
    *frames = (snd_pcm_sframes_t)(p->written - p->completed);
    pthread_mutex_unlock(&p->mutex);
    return 0;
}

static int close_pcm(snd_pcm_ioplug_t *io)
{
    struct rx3_pcm *p = state(io);
    stop(io);
    close(io->poll_fd);
    pthread_mutex_destroy(&p->mutex);
    free(p->ring); free(p->offline); free(p);
    return 0;
}

static const snd_pcm_ioplug_callback_t callbacks = {
    .start = start, .stop = stop, .pointer = pointer, .transfer = transfer,
    .hw_params = hw_params, .sw_params = sw_params, .prepare = prepare, .drain = drain,
    .poll_revents = poll_revents, .delay = delay, .close = close_pcm
};

SND_PCM_PLUGIN_DEFINE_FUNC(rx3)
{
    (void)root;
    if (stream != SND_PCM_STREAM_PLAYBACK) return -EINVAL;
    struct rx3_pcm *p = calloc(1, sizeof(*p));
    if (!p) return -ENOMEM;
    int use_native_profile = 0;
    snd_config_iterator_t pos, next;
    snd_config_for_each(pos, next, conf) {
        snd_config_t *entry = snd_config_iterator_entry(pos);
        const char *id;
        if (snd_config_get_id(entry, &id) < 0) continue;
        if (!strcmp(id, "type") || !strcmp(id, "comment") || !strcmp(id, "hint")) continue;
        if (!strcmp(id, "native_profile")) {
            use_native_profile = snd_config_get_bool(entry);
            if (use_native_profile < 0) { free(p->offline); free(p); return -EINVAL; }
        } else if (!strcmp(id, "offline_output")) {
            const char *value;
            if (snd_config_get_string(entry, &value) < 0 || !*value) { free(p); return -EINVAL; }
            p->offline = strdup(value);
            if (!p->offline) { free(p); return -ENOMEM; }
        } else {
            free(p->offline); free(p); return -EINVAL;
        }
    }
    if (use_native_profile) {
        int period = native_period();
        if (period < 0) {
            SNDERR("Invalid or unreadable RX3 Linux buffer preference");
            free(p->offline); free(p); return period;
        }
        p->native_period = (unsigned)period;
    }
    int rc = pthread_mutex_init(&p->mutex, NULL);
    if (rc) { free(p->offline); free(p); return -rc; }
    p->io.version = SND_PCM_IOPLUG_VERSION;
    /* ioplug_create does not initialize this field from mode. drain() relies
     * on it to return EAGAIN instead of blocking a nonblocking audio client. */
    p->io.nonblock = !!(mode & SND_PCM_NONBLOCK);
    p->io.name = "Experimental XDJ-RX3 playback";
    p->io.callback = &callbacks;
    p->io.private_data = p;
    p->io.poll_fd = eventfd(0, EFD_NONBLOCK | EFD_CLOEXEC);
    p->io.poll_events = POLLIN;
    p->io.flags = SND_PCM_IOPLUG_FLAG_MONOTONIC;
    if (p->io.poll_fd < 0) { rc = -errno; pthread_mutex_destroy(&p->mutex); free(p->offline); free(p); return rc; }
    rc = snd_pcm_ioplug_create(&p->io, name, stream, mode);
    if (rc < 0) { close_pcm(&p->io); return rc; }
    const unsigned access[] = {SND_PCM_ACCESS_RW_INTERLEAVED};
    const unsigned formats[] = {SND_PCM_FORMAT_S32_LE, SND_PCM_FORMAT_S24_3LE};
    #define LIMIT(call) do { rc = (call); if (rc < 0) { snd_pcm_ioplug_delete(&p->io); return rc; } } while (0)
    LIMIT(snd_pcm_ioplug_set_param_list(&p->io, SND_PCM_IOPLUG_HW_ACCESS, 1, access));
    /* The direct endpoint fixes S32 internally so exact frame sizes have
     * unambiguous byte constraints. Its ALSA plug wrapper converts formats
     * in-process; no PipeWire/PulseAudio daemon participates. */
    LIMIT(snd_pcm_ioplug_set_param_list(&p->io, SND_PCM_IOPLUG_HW_FORMAT, p->native_period ? 1 : 2, formats));
    LIMIT(snd_pcm_ioplug_set_param_minmax(&p->io, SND_PCM_IOPLUG_HW_CHANNELS, 4, 4));
    LIMIT(snd_pcm_ioplug_set_param_minmax(&p->io, SND_PCM_IOPLUG_HW_RATE, 44100, 44100));
    /* Byte constraints cover 64 frames in packed24 through 2048 in S32.
     * The settings panel uses four periods at 64 frames: two would not hold
     * the initial four USB packets (~176 frames). 512 remains the default;
     * accepting a smaller size does not promise stable real-time scheduling. */
    if (p->native_period) {
        unsigned bytes = p->native_period * 16;
        unsigned periods = p->native_period == 64 ? 4 : 2;
        LIMIT(snd_pcm_ioplug_set_param_minmax(&p->io, SND_PCM_IOPLUG_HW_PERIOD_BYTES, bytes, bytes));
        LIMIT(snd_pcm_ioplug_set_param_minmax(&p->io, SND_PCM_IOPLUG_HW_BUFFER_BYTES, bytes * periods, bytes * periods));
        LIMIT(snd_pcm_ioplug_set_param_minmax(&p->io, SND_PCM_IOPLUG_HW_PERIODS, periods, periods));
    } else {
        LIMIT(snd_pcm_ioplug_set_param_minmax(&p->io, SND_PCM_IOPLUG_HW_PERIOD_BYTES, 768, 32768));
        LIMIT(snd_pcm_ioplug_set_param_minmax(&p->io, SND_PCM_IOPLUG_HW_BUFFER_BYTES, 3072, 1048576));
        LIMIT(snd_pcm_ioplug_set_param_minmax(&p->io, SND_PCM_IOPLUG_HW_PERIODS, 2, 64));
    }
    #undef LIMIT
    *pcmp = p->io.pcm;
    return 0;
}
SND_PCM_PLUGIN_SYMBOL(rx3);
