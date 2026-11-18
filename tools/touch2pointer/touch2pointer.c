// touch2pointer: lleva la pantalla táctil del GamePad a un compositor Wayland como puntero absoluto.
//
// Hace falta cuando el escritorio del mando es un compositor anidado (p. ej. un segundo Hyprland): un
// compositor anidado solo recibe ratón y teclado del que lo contiene, no toques. Este programa lee el
// dispositivo evdev de la pantalla táctil (lo toma en exclusiva, para que la sesión del PC no reaccione) e
// inyecta el movimiento y el botón izquierdo en el compositor de $WAYLAND_DISPLAY con el protocolo
// wlr-virtual-pointer.
//
// También en el escritorio del PC (extended/mirror con Hyprland): ahí los toques no mueven el cursor y en el
// escritorio vacío o en apps X11 no hacen nada; como ratón funcionan en cualquier sitio. Con T2P_OUTPUT=<monitor>
// el puntero queda atado a ese monitor (wlr-virtual-pointer v2); sin él, a todo el escritorio.
//
// Uso: [T2P_OUTPUT=<monitor>] WAYLAND_DISPLAY=<socket> touch2pointer [nombre del dispositivo]
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/input.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <time.h>
#include <unistd.h>
#include <wayland-client.h>
#include "wlr-virtual-pointer-unstable-v1-client-protocol.h"

static struct zwlr_virtual_pointer_manager_v1 *manager;
static uint32_t manager_ver;

// Monitores (wl_output v4 trae el nombre, p. ej. «DRC-1»)
#define MAX_OUTPUTS 16
static struct { struct wl_output *o; char name[64]; } outputs[MAX_OUTPUTS];
static int n_outputs;

static void out_geometry(void *d, struct wl_output *o, int32_t x, int32_t y, int32_t pw, int32_t ph, int32_t sp,
                         const char *make, const char *model, int32_t tr) {
    (void)d; (void)o; (void)x; (void)y; (void)pw; (void)ph; (void)sp; (void)make; (void)model; (void)tr;
}
static void out_mode(void *d, struct wl_output *o, uint32_t f, int32_t w, int32_t h, int32_t r) {
    (void)d; (void)o; (void)f; (void)w; (void)h; (void)r;
}
static void out_done(void *d, struct wl_output *o) { (void)d; (void)o; }
static void out_scale(void *d, struct wl_output *o, int32_t s) { (void)d; (void)o; (void)s; }
static void out_name(void *d, struct wl_output *o, const char *name) {
    (void)d;
    for (int i = 0; i < n_outputs; i++)
        if (outputs[i].o == o) snprintf(outputs[i].name, sizeof(outputs[i].name), "%s", name);
}
static void out_description(void *d, struct wl_output *o, const char *desc) { (void)d; (void)o; (void)desc; }
static const struct wl_output_listener out_listener = {
    out_geometry, out_mode, out_done, out_scale, out_name, out_description };

static void reg_global(void *data, struct wl_registry *reg, uint32_t name, const char *iface, uint32_t ver) {
    (void)data;
    if (strcmp(iface, zwlr_virtual_pointer_manager_v1_interface.name) == 0) {
        manager_ver = ver < 2 ? ver : 2;
        manager = wl_registry_bind(reg, name, &zwlr_virtual_pointer_manager_v1_interface, manager_ver);
    } else if (strcmp(iface, wl_output_interface.name) == 0 && ver >= 4 && n_outputs < MAX_OUTPUTS) {
        outputs[n_outputs].o = wl_registry_bind(reg, name, &wl_output_interface, 4);
        outputs[n_outputs].name[0] = '\0';
        wl_output_add_listener(outputs[n_outputs].o, &out_listener, NULL);
        n_outputs++;
    }
}
static void reg_remove(void *data, struct wl_registry *reg, uint32_t name) { (void)data; (void)reg; (void)name; }
static const struct wl_registry_listener reg_listener = { reg_global, reg_remove };

// Abre el dispositivo evdev con ese nombre (-1 si aún no existe).
static int open_by_name(const char *want) {
    DIR *d = opendir("/dev/input");
    if (!d) return -1;
    struct dirent *de;
    int found = -1;
    while (found < 0 && (de = readdir(d))) {
        if (strncmp(de->d_name, "event", 5) != 0) continue;
        char path[300], name[128] = "";
        snprintf(path, sizeof(path), "/dev/input/%s", de->d_name);
        int fd = open(path, O_RDONLY | O_NONBLOCK | O_CLOEXEC);
        if (fd < 0) continue;
        if (ioctl(fd, EVIOCGNAME(sizeof(name)), name) >= 0 && strcmp(name, want) == 0) found = fd;
        else close(fd);
    }
    closedir(d);
    return found;
}

static uint32_t now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint32_t)(ts.tv_sec * 1000 + ts.tv_nsec / 1000000);
}

int main(int argc, char **argv) {
    const char *want = argc > 1 ? argv[1] : "Wii U GamePad Touchscreen";
    struct wl_display *dpy = wl_display_connect(NULL);
    if (!dpy) { fprintf(stderr, "touch2pointer: no puedo conectar con WAYLAND_DISPLAY=%s\n", getenv("WAYLAND_DISPLAY")); return 1; }
    struct wl_registry *reg = wl_display_get_registry(dpy);
    wl_registry_add_listener(reg, &reg_listener, NULL);
    wl_display_roundtrip(dpy);
    if (!manager) { fprintf(stderr, "touch2pointer: el compositor no ofrece wlr-virtual-pointer\n"); return 1; }
    wl_display_roundtrip(dpy);                            // nombres de los monitores
    const char *want_out = getenv("T2P_OUTPUT");
    struct wl_output *out = NULL;
    for (int i = 0; want_out && *want_out && i < n_outputs; i++)
        if (strcmp(outputs[i].name, want_out) == 0) out = outputs[i].o;
    struct zwlr_virtual_pointer_v1 *ptr;
    if (out && manager_ver >= 2) {
        ptr = zwlr_virtual_pointer_manager_v1_create_virtual_pointer_with_output(manager, NULL, out);
    } else {
        if (want_out && *want_out)
            fprintf(stderr, "touch2pointer: no puedo atar el puntero a «%s» (%s): se usa todo el escritorio\n", want_out,
                    out ? "el compositor solo ofrece wlr-virtual-pointer v1" : "no existe ese monitor");
        ptr = zwlr_virtual_pointer_manager_v1_create_virtual_pointer(manager, NULL);
    }
    wl_display_roundtrip(dpy);

    for (;;) {
        int fd = -1;
        while ((fd = open_by_name(want)) < 0) {          // drc_stream lo crea al conectar el mando
            if (wl_display_roundtrip(dpy) < 0) return 0;  // el compositor se cerró
            usleep(500000);
        }
        struct input_absinfo ax = {0}, ay = {0};
        ioctl(fd, EVIOCGABS(ABS_X), &ax);
        ioctl(fd, EVIOCGABS(ABS_Y), &ay);
        uint32_t ex = (uint32_t)(ax.maximum - ax.minimum + 1), ey = (uint32_t)(ay.maximum - ay.minimum + 1);
        if (ex < 2 || ey < 2) { ex = 854; ey = 480; }
        if (ioctl(fd, EVIOCGRAB, 1) < 0) fprintf(stderr, "touch2pointer: no pude tomar el dispositivo en exclusiva: %s\n", strerror(errno));
        fprintf(stderr, "touch2pointer: '%s' (%ux%u) -> puntero en %s%s%s\n", want, ex, ey, getenv("WAYLAND_DISPLAY"),
                out ? ", monitor " : "", out ? want_out : "");

        int x = 0, y = 0, down = 0, want_down = 0, moved = 0;
        struct pollfd pfd[2] = { { fd, POLLIN, 0 }, { wl_display_get_fd(dpy), POLLIN, 0 } };
        for (;;) {
            wl_display_flush(dpy);
            if (poll(pfd, 2, -1) < 0 && errno != EINTR) break;
            if (pfd[1].revents & (POLLHUP | POLLERR)) return 0;
            if (pfd[1].revents & POLLIN) { if (wl_display_dispatch(dpy) < 0) return 0; }
            if (pfd[0].revents & (POLLHUP | POLLERR)) break;
            if (!(pfd[0].revents & POLLIN)) continue;
            struct input_event ev;
            ssize_t n;
            while ((n = read(fd, &ev, sizeof(ev))) == (ssize_t)sizeof(ev)) {
                if (ev.type == EV_ABS && ev.code == ABS_X) { x = ev.value - ax.minimum; moved = 1; }
                else if (ev.type == EV_ABS && ev.code == ABS_Y) { y = ev.value - ay.minimum; moved = 1; }
                else if (ev.type == EV_KEY && ev.code == BTN_TOUCH) want_down = ev.value != 0;
                else if (ev.type == EV_SYN && ev.code == SYN_REPORT) {
                    uint32_t t = now_ms();
                    // Primero la posición y después el botón: así el clic cae donde se tocó
                    if (moved || want_down != down) {
                        zwlr_virtual_pointer_v1_motion_absolute(ptr, t, (uint32_t)(x < 0 ? 0 : x), (uint32_t)(y < 0 ? 0 : y), ex, ey);
                        zwlr_virtual_pointer_v1_frame(ptr);
                    }
                    if (want_down != down) {
                        if (want_down) {
                            // Al tocar, el compositor debe haber llevado ya el foco del puntero al elemento
                            // de debajo (sobre todo barras y paneles) antes de recibir el botón: si llegan
                            // juntos, algunos clientes descartan el clic.
                            wl_display_roundtrip(dpy);
                            usleep(12000);
                        }
                        down = want_down;
                        zwlr_virtual_pointer_v1_button(ptr, t, BTN_LEFT, down ? WL_POINTER_BUTTON_STATE_PRESSED : WL_POINTER_BUTTON_STATE_RELEASED);
                        zwlr_virtual_pointer_v1_frame(ptr);
                    }
                    moved = 0;
                }
            }
            if (n < 0 && errno != EAGAIN && errno != EINTR) break;   // dispositivo retirado
        }
        if (down) {   // no dejar el botón pulsado si el mando se desconecta a medio toque
            zwlr_virtual_pointer_v1_button(ptr, now_ms(), BTN_LEFT, WL_POINTER_BUTTON_STATE_RELEASED);
            zwlr_virtual_pointer_v1_frame(ptr);
            wl_display_flush(dpy);
        }
        close(fd);
    }
}
