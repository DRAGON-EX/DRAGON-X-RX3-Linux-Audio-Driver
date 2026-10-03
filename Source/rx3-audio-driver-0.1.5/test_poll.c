/* SPDX-License-Identifier: GPL-3.0-or-later */
#define _POSIX_C_SOURCE 200809L
#include <alsa/asoundlib.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#define CHECK(call) do { int e=(call); if(e<0){fprintf(stderr,"%s: %s\n",#call,snd_strerror(e));return 1;} } while(0)
int main(int argc, char **argv){
 (void)argv;
 snd_pcm_t *p; snd_pcm_hw_params_t *hw; snd_pcm_sw_params_t *sw;
 CHECK(snd_pcm_open(&p,"rx3diag",SND_PCM_STREAM_PLAYBACK,SND_PCM_NONBLOCK));
 snd_pcm_hw_params_malloc(&hw); snd_pcm_sw_params_malloc(&sw);
 CHECK(snd_pcm_hw_params_any(p,hw));
 CHECK(snd_pcm_hw_params_set_access(p,hw,SND_PCM_ACCESS_RW_INTERLEAVED));
 CHECK(snd_pcm_hw_params_set_format(p,hw,SND_PCM_FORMAT_S32_LE));
 CHECK(snd_pcm_hw_params_set_rate(p,hw,44100,0));
 CHECK(snd_pcm_hw_params_set_channels(p,hw,4));
 CHECK(snd_pcm_hw_params_set_period_size(p,hw,256,0));
 CHECK(snd_pcm_hw_params_set_buffer_size(p,hw,512));
 CHECK(snd_pcm_hw_params(p,hw));
 CHECK(snd_pcm_sw_params_current(p,sw));
 CHECK(snd_pcm_sw_params_set_avail_min(p,sw,256));
 CHECK(snd_pcm_sw_params_set_start_threshold(p,sw,512));
 CHECK(snd_pcm_sw_params(p,sw));
 unsigned char zero[512*16]={0};
 CHECK(snd_pcm_writei(p,zero,512));
 struct pollfd fd; CHECK(snd_pcm_poll_descriptors(p,&fd,1));
 int ready=0; unsigned short events=0; snd_pcm_sframes_t avail=0;
 for(int tries=0;tries<1000;tries++){
 ready=poll(&fd,1,10);events=0;
 CHECK(snd_pcm_poll_descriptors_revents(p,&fd,1,&events));
 avail=snd_pcm_avail_update(p);
 if(events & (POLLOUT|POLLERR))break;
 }
 printf("poll=%d POLLOUT=%d available=%ld requested_avail_min=256\n",ready,!!(events&POLLOUT),(long)avail);
 int early=(events&POLLOUT) && avail>=0 && avail<256;
 printf("PREMATURE_READY=%s\n",early?"YES":"NO");
 int writable_ok=(events&POLLOUT) && !(events&POLLERR);
 if(argc>1){
  snd_pcm_drop(p);snd_pcm_close(p);snd_pcm_hw_params_free(hw);snd_pcm_sw_params_free(sw);
  return early || !writable_ok ? 1 : 0;
 }
 for(int tries=0;tries<1000 && !(events&POLLERR);tries++){
  struct timespec wait={.tv_nsec=1000000};nanosleep(&wait,NULL);
  poll(&fd,1,10);events=0;CHECK(snd_pcm_poll_descriptors_revents(p,&fd,1,&events));
 }
 int error_ok=!!(events&POLLERR);
 printf("UNDERRUN_REPORTED_AS_POLLERR=%s\n",error_ok?"YES":"NO");
 snd_pcm_drop(p);snd_pcm_close(p);snd_pcm_hw_params_free(hw);snd_pcm_sw_params_free(sw);
 return early || !writable_ok || !error_ok ? 1 : 0;
}
