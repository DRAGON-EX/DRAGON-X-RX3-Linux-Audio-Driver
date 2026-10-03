/* SPDX-License-Identifier: GPL-3.0-or-later */
#define _POSIX_C_SOURCE 200809L
#include <alloca.h>
#include <alsa/asoundlib.h>
#include <stdio.h>
#include <stdlib.h>

static void check(int rc, const char *what)
{
    if (rc < 0) { fprintf(stderr, "%s: %s\n", what, snd_strerror(rc)); exit(1); }
}

int main(int argc, char **argv)
{
    if (argc != 3 && argc != 4) { fprintf(stderr, "Usage: test_pcm PCM reference_s32le.bin [packed24]\n"); return 2; }
    int packed = argc == 4 && !strcmp(argv[3], "packed24");
    if (argc == 4 && !packed) return 2;
    FILE *f = fopen(argv[2], "rb");
    if (!f) { perror("reference"); return 1; }
    snd_pcm_t *pcm;
    check(snd_pcm_open(&pcm, argv[1], SND_PCM_STREAM_PLAYBACK, 0), "open");
    snd_pcm_hw_params_t *hw;
    snd_pcm_hw_params_alloca(&hw);
    check(snd_pcm_hw_params_any(pcm, hw), "params any");
    if (!getenv("RX3_TEST_PLUG") && (snd_pcm_hw_params_test_rate(pcm, hw, 48000, 0) == 0 ||
        snd_pcm_hw_params_test_channels(pcm, hw, 2) == 0)) {
        fprintf(stderr, "Invalid rate/channel combination accepted\n"); return 1;
    }
    check(snd_pcm_hw_params_set_access(pcm, hw, SND_PCM_ACCESS_RW_INTERLEAVED), "access");
    check(snd_pcm_hw_params_set_format(pcm, hw, packed ? SND_PCM_FORMAT_S24_3LE : SND_PCM_FORMAT_S32_LE), "format");
    check(snd_pcm_hw_params_set_channels(pcm, hw, 4), "channels");
    check(snd_pcm_hw_params_set_rate(pcm, hw, 44100, 0), "rate");
    snd_pcm_uframes_t period = getenv("RX3_TEST_PERIOD") ? strtoul(getenv("RX3_TEST_PERIOD"), NULL, 10) : 256;
    snd_pcm_uframes_t requested_period = period;
    snd_pcm_uframes_t buffer = (period == 64 ? 4 : 2) * period;
    snd_pcm_uframes_t requested_buffer = buffer;
    check(snd_pcm_hw_params_set_period_size_near(pcm, hw, &period, NULL), "period");
    check(snd_pcm_hw_params_set_buffer_size_near(pcm, hw, &buffer), "buffer");
    check(snd_pcm_hw_params(pcm, hw), "hw params");
    if (period != requested_period || buffer != requested_buffer) {
        fprintf(stderr, "Requested period/buffer was silently rounded\n"); return 1;
    }
    snd_pcm_sw_params_t *sw;
    snd_pcm_sw_params_alloca(&sw);
    check(snd_pcm_sw_params_current(pcm, sw), "sw params current");
    check(snd_pcm_sw_params_set_start_threshold(pcm, sw, buffer), "start threshold");
    check(snd_pcm_sw_params_set_avail_min(pcm, sw, period), "avail min");
    check(snd_pcm_sw_params(pcm, sw), "sw params");
    unsigned char samples[256*16], packed_samples[256*12];
    unsigned long long total = 0;
    size_t frames;
    while ((frames = fread(samples, 16, 256, f))) {
        if (packed) {
            for (size_t i = 0; i < frames*4; ++i) memcpy(packed_samples+i*3, samples+i*4+1, 3);
        }
        const unsigned char *data = packed ? packed_samples : samples;
        unsigned stride = packed ? 12 : 16;
        size_t offset = 0;
        while (offset < frames) {
            snd_pcm_sframes_t n = snd_pcm_writei(pcm, data + offset*stride, frames-offset);
            if (n == -EAGAIN) { check(snd_pcm_wait(pcm, 2000), "wait"); continue; }
            check((int)n, "write");
            if (!n) { fprintf(stderr, "No write progress\n"); return 1; }
            offset += (size_t)n;
            total += (unsigned long long)n;
        }
    }
    if (ferror(f)) { perror("read"); return 1; }
    check(snd_pcm_drain(pcm), "drain");
    check(snd_pcm_close(pcm), "close");
    fclose(f);
    printf("ALSA playback and drain completed: %llu frames (period=%lu buffer=%lu)\n", total, (unsigned long)period, (unsigned long)buffer);
    return 0;
}
