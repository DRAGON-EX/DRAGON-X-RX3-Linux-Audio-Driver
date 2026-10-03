/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef RX3_TRANSPORT_H
#define RX3_TRANSPORT_H
#include <stddef.h>
#include <stdint.h>
/* S32_LE, left-aligned signed 24-bit values; low eight bits must be zero. */
typedef int (*rx3_fill_fn)(void *, unsigned char *, unsigned);
typedef void (*rx3_done_fn)(void *, unsigned);
struct rx3_transport;
int rx3_pack(unsigned char *dst, const unsigned char *src, unsigned frames);
int rx3_start(struct rx3_transport **out, rx3_fill_fn fill,
              rx3_done_fn done, void *user, const char *offline_path);
int rx3_error(struct rx3_transport *t);
void rx3_stop(struct rx3_transport *t);
#endif
