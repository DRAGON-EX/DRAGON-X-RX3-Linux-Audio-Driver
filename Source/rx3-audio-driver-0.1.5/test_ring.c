/* SPDX-License-Identifier: GPL-3.0-or-later */
#define _POSIX_C_SOURCE 200809L
/* Exercise the real PCM callbacks with completion deliberately withheld. */
#include "pcm_rx3.c"
#include <assert.h>
#include <stdio.h>

struct rx3_transport { int error; };
int rx3_start(struct rx3_transport **t, rx3_fill_fn f, rx3_done_fn d,
              void *u, const char *path)
{
    (void)t; (void)f; (void)d; (void)u; (void)path; return -ENOSYS;
}
int rx3_error(struct rx3_transport *t) { return t ? t->error : 0; }
void rx3_stop(struct rx3_transport *t) { (void)t; }

int main(void)
{
    struct rx3_pcm p = {0};
    struct rx3_transport t = {0};
    assert(pthread_mutex_init(&p.mutex, NULL) == 0);
    p.io.private_data = &p;
    p.io.buffer_size = 512;
    p.io.period_size = p.avail_min = 256;
    p.io.format = SND_PCM_FORMAT_S32_LE;
    p.io.poll_fd = eventfd(0, EFD_NONBLOCK | EFD_CLOEXEC);
    assert(p.io.poll_fd >= 0);
    p.ring = calloc(512, 16);
    assert(p.ring);
    p.transport = &t;
    uint32_t source[768 * 4];
    for (unsigned i = 0; i < 768 * 4; ++i) source[i] = (i + 1) << 8;
    snd_pcm_channel_area_t areas[4];
    for (unsigned ch = 0; ch < 4; ++ch)
        areas[ch] = (snd_pcm_channel_area_t){source, ch * 32, 128};
    assert(transfer(&p.io, areas, 0, 512) == 512);
    assert(!writable(&p));

    unsigned char copied[768 * 16];
    for (unsigned i = 0; i < 6; ++i) {
        assert(fill(&p, copied + i * 44 * 16, 44) == 44);
        if (i < 5) assert(!writable(&p));
    }
    /* 264 copied, no USB completions: a complete period is writable. */
    assert(p.completed == 0 && p.reserved == 264);
    assert(pointer(&p.io) == 263 && writable(&p));
    snd_pcm_sframes_t frames;
    assert(delay(&p.io, &frames) == 0 && frames == 512);
    struct pollfd fd = {p.io.poll_fd, POLLIN, 0};
    assert(poll(&fd, 1, 0) == 1);
    unsigned short events;
    assert(poll_revents(&p.io, &fd, 1, &events) == 0);
    assert(events & POLLOUT);

    /* Wrap and refill while those copies still belong to USB. */
    assert(transfer(&p.io, areas, 512, 256) == 256);
    assert(transfer(&p.io, areas, 0, 8) == -EAGAIN);
    assert(!writable(&p));
    done(&p, 176);
    assert(pointer(&p.io) == 263);
    assert(delay(&p.io, &frames) == 0 && frames == 592);
    for (unsigned i = 264; i < 768; i += 24)
        assert(fill(&p, copied + i * 16, 24) == 24);
    assert(!memcmp(copied, source, sizeof(copied)));
    /* Ring empty, but USB still pending: retain the final frame for drain. */
    assert(pointer(&p.io) == 255);
    assert(delay(&p.io, &frames) == 0 && frames == 592);
    assert(fill(&p, copied, 44) == -EPIPE);
    assert(p.reserved == 768);
    p.draining = 1;
    assert(fill(&p, copied, 44) == 0);
    done(&p, 592);
    assert(pointer(&p.io) == 256);
    assert(delay(&p.io, &frames) == 0 && frames == 0);
    t.error = -EIO;
    done(&p, 0);
    assert(poll(&fd, 1, 0) == 1);
    assert(poll_revents(&p.io, &fd, 1, &events) == 0 && (events & POLLERR));
    assert(pointer(&p.io) == -EIO);
    close(p.io.poll_fd);
    free(p.ring);
    pthread_mutex_destroy(&p.mutex);
    puts("PASS: refill before completion, unchanged queued copies, exact delay, drain fence, POLLERR");
    return 0;
}
