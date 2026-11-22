// /vipxpert_minimal_os/src/core/viz_dsp.c
// Native zero-GC ARM64 48kHz 20-band 4-tap log-spaced Goertzel spectrum engine (<0.5% CPU vs 70% in Python).
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/select.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

static pid_t g_p_main = -1;
static pid_t g_p_aux = -1;

static void clean_exit(int sig) {
    (void)sig;
    if (g_p_main > 0) kill(g_p_main, SIGTERM);
    if (g_p_aux > 0) kill(g_p_aux, SIGTERM);
    _exit(0);
}

static void get_default_monitor(char *buf, size_t cap) {
    snprintf(buf, cap, "alsa_output.platform-soc_ahub0_mach.stereo-fallback.monitor");
    FILE *fp = popen("pactl get-default-sink 2>/dev/null", "r");
    if (!fp) return;
    char line[256];
    if (fgets(line, sizeof(line), fp)) {
        line[strcspn(line, "\r\n")] = 0;
        if (line[0] && !strstr(line, "vip_mic_bus")) {
            snprintf(buf, cap, "%s.monitor", line);
        }
    }
    pclose(fp);
}

static int spawn_parec(const char *target, int is_stream, pid_t *out_pid) {
    char resolved[300];
    if (!is_stream && strcmp(target, "@DEFAULT_MONITOR@") == 0) {
        get_default_monitor(resolved, sizeof(resolved));
        target = resolved;
    }
    int pfd[2];
    if (pipe(pfd) < 0) return -1;
    pid_t pid = fork();
    if (pid < 0) {
        close(pfd[0]);
        close(pfd[1]);
        return -1;
    }
    if (pid == 0) {
        close(pfd[0]);
        dup2(pfd[1], STDOUT_FILENO);
        close(pfd[1]);
        int devnull = open("/dev/null", O_WRONLY);
        if (devnull >= 0) {
            dup2(devnull, STDERR_FILENO);
            close(devnull);
        }
        char arg_target[340];
        snprintf(arg_target, sizeof(arg_target), is_stream ? "--monitor-stream=%s" : "--device=%s", target);
        execlp("parec", "parec", "--format=s16le", "--rate=48000", "--channels=1", "--latency-msec=22", arg_target, (char *)NULL);
        _exit(127);
    }
    close(pfd[1]);
    int fl = fcntl(pfd[0], F_GETFL, 0);
    fcntl(pfd[0], F_SETFL, fl | O_NONBLOCK);
    *out_pid = pid;
    return pfd[0];
}

static double goertzel_quad(const double *w, double c0, double c1, double c2, double c3) {
    double s1a = 0, s2a = 0, s1b = 0, s2b = 0, s1c = 0, s2c = 0, s1d = 0, s2d = 0;
    for (int i = 0; i < 128; i++) {
        double x = w[i];
        double s0a = x + c0 * s1a - s2a; s2a = s1a; s1a = s0a;
        double s0b = x + c1 * s1b - s2b; s2b = s1b; s1b = s0b;
        double s0c = x + c2 * s1c - s2c; s2c = s1c; s1c = s0c;
        double s0d = x + c3 * s1d - s2d; s2d = s1d; s1d = s0d;
    }
    double pa = s1a * s1a + s2a * s2a - c0 * s1a * s2a; if (pa < 0) pa = 0;
    double pb = s1b * s1b + s2b * s2b - c1 * s1b * s2b; if (pb < 0) pb = 0;
    double pc = s1c * s1c + s2c * s2c - c2 * s1c * s2c; if (pc < 0) pc = 0;
    double pd = s1d * s1d + s2d * s2d - c3 * s1d * s2d; if (pd < 0) pd = 0;
    double pmax = pa;
    if (pb > pmax) pmax = pb;
    if (pc > pmax) pmax = pc;
    if (pd > pmax) pmax = pd;
    double pavg = 0.25 * (pa + pb + pc + pd);
    return sqrt(pmax * 0.75 + pavg * 0.25) * 0.015625;
}

static void shift_ring(uint8_t *ring, const uint8_t *chunk, int n) {
    int nc = n & ~1;
    if (nc >= 2048) {
        memcpy(ring, chunk + (nc - 2048), 2048);
    } else if (nc > 0) {
        memmove(ring, ring + nc, 2048 - nc);
        memcpy(ring + (2048 - nc), chunk, nc);
    }
}

int main(int argc, char **argv) {
    if (argc < 5) return 1;
    const char *viz_mode = argv[1];
    const char *curve_mode = argv[2];
    const char *amb_on = argv[3];
    const char *stream_idx = argv[4];

    signal(SIGTERM, clean_exit);
    signal(SIGINT, clean_exit);
    signal(SIGPIPE, clean_exit);

    static const double centers[20] = {
        45, 65, 92, 130, 185, 260, 365, 510, 700, 960,
        1320, 1800, 2450, 3350, 4550, 6200, 8400, 11400, 15200, 19500
    };
    int strides[20];
    double c_tap[20][4], weights[20], hann128[128];
    for (int i = 0; i < 128; i++) {
        hann128[i] = 0.5 * (1.0 - cos(2.0 * M_PI * i / 127.0));
    }
    // Comment (invariant): Keep C99 fallback synchronized 1:1 with rust_workers/src/bin/viz_dsp.rs 4-tier Constant-Q decimation (stride 8/4/2/1) for 0-LSB parity and 20/20 center-frequency lock.
    int is_pink = (strcmp(curve_mode, "pink") == 0);
    for (int idx = 0; idx < 20; idx++) {
        double fc = centers[idx];
        int stride = (idx < 6) ? 8 : ((idx < 11) ? 4 : ((idx < 16) ? 2 : 1));
        double fs_eff = (idx < 6) ? 6000.0 : ((idx < 11) ? 12000.0 : ((idx < 16) ? 24000.0 : 48000.0));
        strides[idx] = stride;
        double f_prev = (idx > 0) ? centers[idx - 1] : (fc / 1.38);
        double f_next = (idx + 1 < 20) ? centers[idx + 1] : 21400.0;
        double f_lo = fc * pow(f_prev / fc, 0.38); if (f_lo < 25.0) f_lo = 25.0;
        double f_hi = fc * pow(f_next / fc, 0.38); if (f_hi > fs_eff * 0.435) f_hi = fs_eff * 0.435;
        if (idx == 19) { f_lo = 17100.0; f_hi = 20400.0; }
        double ratio = (f_hi > f_lo) ? (f_hi / f_lo) : 1.05;
        for (int k = 0; k < 4; k++) {
            double fk = f_lo * pow(ratio, k / 3.0);
            c_tap[idx][k] = 2.0 * cos(2.0 * M_PI * fk / fs_eff);
        }
        double xs = M_PI * fc / fs_eff;
        double sinc_comp = (stride > 1 && xs > 0.01) ? (xs / sin(xs)) : 1.0;
        double hf_boost = (idx >= 18) ? 1.35 : ((idx == 17) ? 1.15 : 1.0);
        weights[idx] = (is_pink ? pow(fc / 60.0, 0.58) : 1.0) * sinc_comp * hf_boost;
    }

    int using_stream_idx = 0;
    int fd_main = -1, fd_aux = -1;
    if (strcmp(viz_mode, "mic_only") == 0) {
        fd_main = spawn_parec("vip_mic_bus.monitor", 0, &g_p_main);
    } else if (strcmp(viz_mode, "music_only") == 0) {
        if (stream_idx[0] != '\0') {
            fd_main = spawn_parec(stream_idx, 1, &g_p_main);
            using_stream_idx = 1;
        } else {
            fd_main = spawn_parec("@DEFAULT_MONITOR@", 0, &g_p_main);
        }
    } else {
        fd_main = spawn_parec("@DEFAULT_MONITOR@", 0, &g_p_main);
        if (strcmp(amb_on, "0") == 0) {
            fd_aux = spawn_parec("vip_mic_bus.monitor", 0, &g_p_aux);
        }
    }
    if (fd_main < 0) clean_exit(0);

    uint8_t ring_main[2048] = {0}, ring_aux[2048] = {0}, chunk[2048], out_bytes[20];
    int is_music_only = (strcmp(viz_mode, "music_only") == 0);
    double agc_peak = is_music_only ? 6.0 : 180.0;
    double floor_pk = is_music_only ? 1.8 : 18.0;
    double gate_v = is_music_only ? 0.12 : 2.2;
    double smooth[20];
    for (int i = 0; i < 20; i++) smooth[i] = 0.05;
    int bytes_since_calc = 0;

    while (1) {
        fd_set rfds;
        FD_ZERO(&rfds);
        FD_SET(fd_main, &rfds);
        int maxfd = fd_main;
        if (fd_aux >= 0) {
            FD_SET(fd_aux, &rfds);
            if (fd_aux > maxfd) maxfd = fd_aux;
        }
        struct timeval tv = {0, 30000};
        int sr = select(maxfd + 1, &rfds, NULL, NULL, &tv);
        if (sr > 0 && FD_ISSET(fd_main, &rfds)) {
            while (bytes_since_calc < 1856) {
                ssize_t r = read(fd_main, chunk, sizeof(chunk));
                if (r == 0) {
                    if (using_stream_idx) {
                        close(fd_main);
                        if (g_p_main > 0) { kill(g_p_main, SIGTERM); waitpid(g_p_main, NULL, WNOHANG); }
                        fd_main = spawn_parec("@DEFAULT_MONITOR@", 0, &g_p_main);
                        using_stream_idx = 0;
                        if (fd_main < 0) clean_exit(0);
                        break;
                    }
                    clean_exit(0);
                } else if (r < 0) {
                    if (errno == EAGAIN || errno == EWOULDBLOCK) break;
                    clean_exit(0);
                }
                bytes_since_calc += (int)r;
                shift_ring(ring_main, chunk, (int)r);
            }
        }
        if (fd_aux >= 0 && sr > 0 && FD_ISSET(fd_aux, &rfds)) {
            while (1) {
                ssize_t r = read(fd_aux, chunk, sizeof(chunk));
                if (r <= 0) break;
                if ((int)r > bytes_since_calc) bytes_since_calc = (int)r;
                shift_ring(ring_aux, chunk, (int)r);
            }
        }
        if (bytes_since_calc < 1856) continue;
        bytes_since_calc = 0;

        const int16_t *am = (const int16_t *)ring_main;
        const int16_t *aa = (const int16_t *)ring_aux;
        double pcm[1024];
        if (fd_aux >= 0) {
            for (int i = 0; i < 1024; i++) pcm[i] = (double)am[i] + (double)aa[i] * 1.15;
        } else {
            for (int i = 0; i < 1024; i++) pcm[i] = (double)am[i];
        }

        double s8[128], w8[128], s4e[128], w4e[128], s4l[128], w4l[128], s2e[128], w2e[128], s2l[128], w2l[128], w1e[128], w1l[128];
        double m8 = 0, m4e = 0, m4l = 0, m2e = 0, m2l = 0, m1e = 0, m1l = 0;
        for (int i = 0; i < 128; i++) {
            double sum8 = 0;
            for (int k = 0; k < 8; k++) sum8 += pcm[i * 8 + k];
            s8[i] = sum8 * 0.125; m8 += s8[i];
            s4e[i] = (pcm[i * 4] + pcm[i * 4 + 1] + pcm[i * 4 + 2] + pcm[i * 4 + 3]) * 0.25; m4e += s4e[i];
            int o4 = 512 + i * 4;
            s4l[i] = (pcm[o4] + pcm[o4 + 1] + pcm[o4 + 2] + pcm[o4 + 3]) * 0.25; m4l += s4l[i];
            s2e[i] = (pcm[256 + i * 2] + pcm[257 + i * 2]) * 0.5; m2e += s2e[i];
            s2l[i] = (pcm[768 + i * 2] + pcm[769 + i * 2]) * 0.5; m2l += s2l[i];
            m1e += pcm[384 + i];
            m1l += pcm[896 + i];
        }
        m8 *= 0.0078125; m4e *= 0.0078125; m4l *= 0.0078125; m2e *= 0.0078125; m2l *= 0.0078125; m1e *= 0.0078125; m1l *= 0.0078125;
        for (int i = 0; i < 128; i++) {
            w8[i]  = (s8[i] - m8) * hann128[i];
            w4e[i] = (s4e[i] - m4e) * hann128[i];
            w4l[i] = (s4l[i] - m4l) * hann128[i];
            w2e[i] = (s2e[i] - m2e) * hann128[i];
            w2l[i] = (s2l[i] - m2l) * hann128[i];
            w1e[i] = (pcm[384 + i] - m1e) * hann128[i];
            w1l[i] = (pcm[896 + i] - m1l) * hann128[i];
        }

        double mags[20], frame_max = 1.0;
        for (int idx = 0; idx < 20; idx++) {
            double c0 = c_tap[idx][0], c1 = c_tap[idx][1], c2 = c_tap[idx][2], c3 = c_tap[idx][3];
            double raw_mag;
            if (strides[idx] == 8) {
                raw_mag = goertzel_quad(w8, c0, c1, c2, c3);
            } else if (strides[idx] == 4) {
                double a = goertzel_quad(w4e, c0, c1, c2, c3);
                double b = goertzel_quad(w4l, c0, c1, c2, c3);
                raw_mag = (a > b) ? a : b;
            } else if (strides[idx] == 2) {
                double a = goertzel_quad(w2e, c0, c1, c2, c3);
                double b = goertzel_quad(w2l, c0, c1, c2, c3);
                raw_mag = (a > b) ? a : b;
            } else {
                double a = goertzel_quad(w1e, c0, c1, c2, c3);
                double b = goertzel_quad(w1l, c0, c1, c2, c3);
                raw_mag = (a > b) ? a : b;
            }
            double val = raw_mag * weights[idx];
            mags[idx] = val;
            if (val > frame_max) frame_max = val;
        }

        if (frame_max > agc_peak) {
            agc_peak = agc_peak * 0.35 + frame_max * 0.65;
        } else {
            agc_peak = agc_peak * 0.96 + frame_max * 0.04;
            if (agc_peak < floor_pk) agc_peak = floor_pk;
        }

        for (int i = 0; i < 20; i++) {
            double norm;
            if (mags[i] < gate_v) {
                norm = 0.05;
            } else {
                double ratio = mags[i] / agc_peak;
                if (ratio < 1e-4) ratio = 1e-4;
                double db_rel = 20.0 * log10(ratio);
                double lin = (db_rel + 22.0) / 22.0;
                if (lin < 0.0) lin = 0.0;
                if (lin > 1.0) lin = 1.0;
                norm = pow(lin, 1.35);
                if (norm < 0.05) norm = 0.05;
                if (norm > 1.0) norm = 1.0;
            }
            double prev_b = smooth[i];
            double cur_b = (norm > prev_b) ? (prev_b * 0.20 + norm * 0.80) : (prev_b * 0.54 + norm * 0.46);
            if (cur_b < 0.05) cur_b = 0.05;
            if (cur_b > 1.0) cur_b = 1.0;
            smooth[i] = cur_b;
            int iv = (int)llround(cur_b * 255.0);
            if (iv < 12) iv = 12;
            if (iv > 255) iv = 255;
            out_bytes[i] = (uint8_t)iv;
        }
        if (write(STDOUT_FILENO, out_bytes, 20) < 0) clean_exit(0);
    }
    return 0;
}
