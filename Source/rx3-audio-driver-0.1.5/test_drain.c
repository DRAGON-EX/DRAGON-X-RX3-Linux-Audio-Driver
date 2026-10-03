/* SPDX-License-Identifier: GPL-3.0-or-later */
#define _POSIX_C_SOURCE 200809L
#include <alloca.h>
#include <alsa/asoundlib.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>

#define CHECK(call) do { int rc = (call); if (rc < 0) { \
    fprintf(stderr, "%s: %s\n", #call, snd_strerror(rc)); return 1; } } while (0)

int main(void)
{
    snd_pcm_t *pcm;
    CHECK(snd_pcm_open(&pcm, "rx3tail", SND_PCM_STREAM_PLAYBACK, SND_PCM_NONBLOCK));
    snd_pcm_hw_params_t *hw;
    snd_pcm_hw_params_alloca(&hw);
    CHECK(snd_pcm_hw_params_any(pcm, hw));
    CHECK(snd_pcm_hw_params_set_access(pcm, hw, SND_PCM_ACCESS_RW_INTERLEAVED));
    const char *format = getenv("RX3_TEST_FORMAT");
    int packed = format && !strcmp(format, "packed24");
    CHECK(snd_pcm_hw_params_set_format(pcm, hw, packed ? SND_PCM_FORMAT_S24_3LE : SND_PCM_FORMAT_S32_LE));
    CHECK(snd_pcm_hw_params_set_channels(pcm, hw, 4));
    CHECK(snd_pcm_hw_params_set_rate(pcm, hw, 44100, 0));
    CHECK(snd_pcm_hw_params_set_period_size(pcm, hw, 256, 0));
    CHECK(snd_pcm_hw_params_set_buffer_size(pcm, hw, 512));
    CHECK(snd_pcm_hw_params(pcm, hw));
    snd_pcm_sw_params_t *sw;
    snd_pcm_sw_params_alloca(&sw);
    CHECK(snd_pcm_sw_params_current(pcm, sw));
    CHECK(snd_pcm_sw_params_set_start_threshold(pcm, sw, 512));
    CHECK(snd_pcm_sw_params(pcm, sw));
    unsigned char zero[257 * 16] = {0};
    snd_pcm_sframes_t written = snd_pcm_writei(pcm, zero, 257);
    if (written != 257) { fprintf(stderr, "write: %ld\n", (long)written); return 1; }
    CHECK(snd_pcm_start(pcm));
    unsigned pending = 0;
    for (unsigned i = 0; i < 2000; ++i) {
        int rc = snd_pcm_drain(pcm);
        if (rc == 0) {
            snd_pcm_sframes_t delay;
            CHECK(snd_pcm_delay(pcm, &delay));
            if (delay != 0 || !pending) { fprintf(stderr, "finished: delay=%ld pending=%u\n", (long)delay, pending); return 1; }
            CHECK(snd_pcm_close(pcm));
            puts("PASS: nonblocking drain waited for final USB completion despite repeated avail updates");
            return 0;
        }
        if (rc != -EAGAIN) { fprintf(stderr, "drain: %s\n", snd_strerror(rc)); return 1; }
        snd_pcm_sframes_t delay;
        CHECK(snd_pcm_delay(pcm, &delay));
        CHECK(snd_pcm_avail_update(pcm));
        CHECK(snd_pcm_delay(pcm, &delay));
        if (delay > 0 && snd_pcm_state(pcm) == SND_PCM_STATE_SETUP) {
            fputs("Premature drain: USB data cancelled by ALSA avail update\n", stderr);
            return 1;
        }
        pending += delay > 0;
        struct timespec interval = {.tv_nsec = 1000000};
        nanosleep(&interval, NULL);
    }
    fputs("Drain timed out\n", stderr);
    return 1;
}
