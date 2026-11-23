#!/usr/bin/env python3
"""Aplicación gráfica del GamePad (GTK4 + libadwaita): emparejar, transmitir y ver el estado.

Toda la lógica está en drc_ctl.py (la misma que usa el menú de terminal); aquí solo hay presentación.
Las acciones con privilegios se lanzan con pkexec (ventana de contraseña de polkit).
"""
import os
import subprocess
import sys
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import drc_ap  # noqa: E402
import drc_ctl as ctl  # noqa: E402

CSS = b"""
.req-ok { color: @success_color; }
.req-warn { color: @warning_color; }
.req-fail { color: @error_color; }
.symbol { font-size: 22px; }
.state { font-size: 18px; font-weight: bold; }
"""
ICON_NAME = {ctl.OK: "emblem-ok-symbolic", ctl.WARN: "dialog-warning-symbolic", ctl.FAIL: "dialog-error-symbolic"}
CSS_CLASS = {ctl.OK: "req-ok", ctl.WARN: "req-warn", ctl.FAIL: "req-fail"}


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Wii U GamePad", default_width=760, default_height=720)
        self.host = ctl.Host()
        self.proc = None                    # acción en curso (emparejar / transmitir)
        self.ifaces = []
        self.pair_checks, self.start_checks = [], []

        self.toast = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.state_label = Gtk.Label(label="…", css_classes=["dim-label"])
        header.pack_start(self.state_label)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Volver a comprobar todo")
        refresh.connect("clicked", lambda *_: self.refresh_all())
        header.pack_end(refresh)
        view.add_top_bar(header)

        self.stack = Adw.ViewStack()
        switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)
        self.stack.add_titled_with_icon(self._build_iface_page(), "iface", "Interfaz", "network-wireless-symbolic")
        self.stack.add_titled_with_icon(self._build_pair_page(), "pair", "Emparejar", "insert-link-symbolic")
        self.stack.add_titled_with_icon(self._build_start_page(), "start", "Transmitir", "media-playback-start-symbolic")
        self.stack.add_titled_with_icon(self._build_config_page(), "config", "Configuración", "emblem-system-symbolic")

        # Registro compartido, plegable
        self.log = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=6, bottom_margin=6,
                                left_margin=8, right_margin=8)
        scroll = Gtk.ScrolledWindow(min_content_height=120, child=self.log)
        expander = Gtk.Expander(label="Registro", child=scroll, expanded=True, margin_start=12, margin_end=12, margin_bottom=12)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.stack.set_vexpand(True)
        box.append(self.stack)
        box.append(expander)
        view.set_content(box)
        self.toast.set_child(view)
        self.set_content(self.toast)

        self.connect("close-request", self._on_close)
        self.refresh_all()
        GLib.timeout_add_seconds(3, self._poll_status)

    # ---------------------------------------------------------------- páginas
    def _page(self, *children):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_top=18, margin_bottom=18, margin_start=18, margin_end=18)
        for ch in children:
            box.append(ch)
        return Gtk.ScrolledWindow(child=Adw.Clamp(maximum_size=640, child=box), vexpand=True)

    def _build_iface_page(self):
        self.iface_group = Adw.PreferencesGroup(title="Interfaz de red del GamePad",
                                                description="El GamePad habla en 5 GHz. Elige la tarjeta que creará su red.")
        self.iface_rows = []
        self.state_big = Gtk.Label(label="…", css_classes=["state"], xalign=0)
        self.state_detail = Gtk.Label(label="", xalign=0, css_classes=["dim-label"], wrap=True)
        status_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        status_box.append(self.state_big)
        status_box.append(self.state_detail)
        return self._page(status_box, self.iface_group)

    def _build_pair_page(self):
        sym_group = Adw.PreferencesGroup(title="Símbolos", description="Los mismos que introducirás en la pantalla del GamePad.")
        row = Adw.ActionRow(title="Símbolos de emparejamiento")
        self.sym_drops = []
        cur = ctl.current_symbols()
        for i in range(4):
            dd = Gtk.DropDown.new_from_strings(list(ctl.SYMBOLS))
            dd.set_selected(cur[i])
            dd.add_css_class("symbol")
            dd.set_valign(Gtk.Align.CENTER)
            dd.connect("notify::selected", self._on_symbols)
            self.sym_drops.append(dd)
            row.add_suffix(dd)
        sym_group.add(row)
        self.pair_group = Adw.PreferencesGroup(title="Qué hace falta para emparejar")
        self.pair_rows = []
        self.pair_btn = Gtk.Button(label="Emparejar", css_classes=["suggested-action", "pill"], halign=Gtk.Align.CENTER)
        self.pair_btn.connect("clicked", self._on_pair)
        self.pair_msg = Gtk.Label(wrap=True, css_classes=["dim-label"])
        return self._page(sym_group, self.pair_group, self.pair_msg, self.pair_btn)

    def _build_start_page(self):
        self.start_group = Adw.PreferencesGroup(title="Qué hace falta para transmitir")
        self.start_rows = []
        mode_group = Adw.PreferencesGroup(title="Pantalla")
        self.mode_row = Adw.ComboRow(title="Modo", model=Gtk.StringList.new(["separate", "extended", "mirror"]),
                                     subtitle="separate = sesión propia del GamePad; extended/mirror = monitor de tu escritorio")
        cfg_mode = ctl.effective_mode()
        self.mode_row.set_selected(["separate", "extended", "mirror"].index(cfg_mode) if cfg_mode in ("separate", "extended", "mirror") else 0)
        self.mode_row.connect("notify::selected", self._on_mode)
        mode_group.add(self.mode_row)
        self.start_btn = Gtk.Button(label="Empezar transmisión", css_classes=["suggested-action", "pill"])
        self.start_btn.connect("clicked", self._on_start)
        self.stop_btn = Gtk.Button(label="Detener", css_classes=["destructive-action", "pill"])
        self.stop_btn.connect("clicked", self._on_stop)
        btns = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        btns.append(self.start_btn)
        btns.append(self.stop_btn)
        self.start_msg = Gtk.Label(wrap=True, css_classes=["dim-label"])
        return self._page(self.start_group, mode_group, self.start_msg, btns)

    def _build_config_page(self):
        """Un ajuste de drc.conf por fila (menos el modo, que está en «Transmitir»); se guarda al cambiarlo."""
        cfg = drc_ap.load_drc_conf()
        groups = {}
        for f in ctl.CONFIG_FIELDS:
            if f.key == "DRC_MODE":
                continue
            grp = groups.get(f.group)
            if grp is None:
                grp = groups[f.group] = Adw.PreferencesGroup(title=f.group)
            val = ctl.field_value(cfg, f)
            sub = f.help
            if f.kind == "choice":
                row = Adw.ComboRow(title=f.label, subtitle=sub, model=Gtk.StringList.new(list(f.choices)))
                row.set_selected(f.choices.index(val) if val in f.choices else 0)
                row.connect("notify::selected", lambda r, _p, f=f: self._save(f, f.choices[r.get_selected()]))
            elif f.kind == "int":
                adj = Gtk.Adjustment(value=int(val) if str(val).lstrip("-").isdigit() else int(f.default or f.lo),
                                     lower=f.lo, upper=f.hi, step_increment=1, page_increment=10)
                row = Adw.SpinRow(title=f.label, subtitle=sub, adjustment=adj)
                row.connect("notify::value", lambda r, _p, f=f: self._save(f, str(int(r.get_value()))))
            elif f.kind == "bool":
                row = Adw.SwitchRow(title=f.label, subtitle=sub, active=(val == "1"))
                row.connect("notify::active", lambda r, _p, f=f: self._save(f, "1" if r.get_active() else "0"))
            else:
                row = Adw.EntryRow(title=f.label, text=val, show_apply_button=True)
                if sub:
                    row.set_tooltip_text(sub)
                row.connect("apply", lambda r, f=f: self._save(f, r.get_text()))
            grp.add(row)
        page = [Gtk.Label(label=f"Se guardan en {ctl.conf_path() or 'drc.conf'} y se aplican en la próxima transmisión.",
                          wrap=True, xalign=0, css_classes=["dim-label"])]
        return self._page(*page, *groups.values())

    def _save(self, f, value):
        try:
            value = ctl.validate_field(f, value)
            ctl.set_conf_value(f.key, value)
            self.toast.add_toast(Adw.Toast(title=f"Guardado: {f.label}", timeout=2))
            self.refresh_checks()
        except (ValueError, OSError) as e:
            self.toast.add_toast(Adw.Toast(title=f"No se guardó {f.label}: {e}", timeout=5))

    def _on_mode(self, *_):
        mode = self.mode_row.get_selected_item().get_string()
        if mode != ctl.effective_mode():
            self._save(ctl.FIELDS_BY_KEY["DRC_MODE"], mode)

    # ---------------------------------------------------------------- selección
    def selected_iface(self):
        return getattr(self, "_iface", None) or ctl.current_iface()

    def selected_symbols(self):
        return tuple(dd.get_selected() for dd in self.sym_drops)

    def _on_symbols(self, *_):
        ctl.save_state({"symbols": ",".join(map(str, self.selected_symbols()))})
        self.refresh_checks()

    def _on_iface_toggle(self, check, name):
        if check.get_active():
            self._iface = name
            try:
                ctl.set_iface(name)          # WIFI_IFACE de drc.conf: la misma para todas las herramientas
            except OSError as e:
                print(f"[drc_gui] no se guardó la interfaz: {e}", file=sys.stderr)
            self.refresh_checks()

    # ---------------------------------------------------------------- refresco
    def refresh_all(self):
        def work():
            ifaces = ctl.list_wifi_ifaces(self.host)
            GLib.idle_add(self._set_ifaces, ifaces)
        threading.Thread(target=work, daemon=True).start()

    def _set_ifaces(self, ifaces):
        self.ifaces = ifaces
        for r in self.iface_rows:
            self.iface_group.remove(r)
        self.iface_rows = []
        cur = self.selected_iface()
        best = ctl.recommend_iface(ifaces)
        first = None
        for i in ifaces:
            row = Adw.ActionRow(title=i.name, subtitle=i.summary())
            tags = []
            if best is not None and i.name == best.name:
                tags.append("recomendada")
            if not i.suitable:
                tags.append("no sirve" if not (i.ap and i.band5) else "es tu Internet")
            if tags:
                row.add_suffix(Gtk.Label(label=" · ".join(tags), css_classes=["dim-label"]))
            btn = Gtk.CheckButton(group=first, active=(i.name == cur))
            first = first or btn
            btn.connect("toggled", self._on_iface_toggle, i.name)
            row.add_prefix(btn)
            row.set_activatable_widget(btn)
            self.iface_group.add(row)
            self.iface_rows.append(row)
        if not ifaces:
            self.iface_rows.append(Adw.ActionRow(title="No se encontró ninguna interfaz Wi-Fi"))
            self.iface_group.add(self.iface_rows[-1])
        self.refresh_checks()
        return False

    def refresh_checks(self):
        iface, sym, ifaces = self.selected_iface(), self.selected_symbols(), list(self.ifaces)
        mode = self.mode_row.get_selected_item().get_string()

        def work():
            pair = ctl.check_pair(self.host, iface, sym, ifaces=ifaces)
            start = ctl.check_start(self.host, iface, ifaces=ifaces, mode=mode)
            GLib.idle_add(self._set_checks, pair, start)
        threading.Thread(target=work, daemon=True).start()

    def _fill(self, group, rows, checks):
        for r in rows:
            group.remove(r)
        rows.clear()
        for c in checks:
            row = Adw.ActionRow(title=GLib.markup_escape_text(c.name), subtitle=GLib.markup_escape_text(
                c.detail + (f"\n→ {c.fix}" if c.status != ctl.OK and c.fix else "")))
            row.set_subtitle_lines(0)
            icon = Gtk.Image(icon_name=ICON_NAME[c.status], css_classes=[CSS_CLASS[c.status]])
            row.add_prefix(icon)
            group.add(row)
            rows.append(row)

    def _set_checks(self, pair, start):
        first = not (self.pair_checks or self.start_checks)
        self.pair_checks, self.start_checks = pair, start
        if first:   # empezar en lo que toca: emparejar si aún no hay GamePad, transmitir si ya lo hay
            self.stack.set_visible_child_name(os.environ.get("DRC_GUI_PAGE") or ("start" if ctl.paired_gamepad() else "pair"))
        self._fill(self.pair_group, self.pair_rows, pair)
        self._fill(self.start_group, self.start_rows, start)
        self._update_buttons()
        return False

    def _update_buttons(self):
        busy = self.proc is not None and self.proc.poll() is None
        bp, bs = ctl.blocking(self.pair_checks), ctl.blocking(self.start_checks)
        self.pair_btn.set_sensitive(not busy and not bp and bool(self.pair_checks))
        self.start_btn.set_sensitive(not busy and not bs and bool(self.start_checks))
        self.stop_btn.set_sensitive(True)
        self.pair_msg.set_text(f"Falta resolver {len(bp)} cosa(s) para poder emparejar." if bp else "")
        self.start_msg.set_text(f"Falta resolver {len(bs)} cosa(s) para poder transmitir." if bs else "")

    def _poll_status(self):
        def work():
            st = ctl.status(self.host, self.selected_iface())
            GLib.idle_add(self._set_status, st)
        threading.Thread(target=work, daemon=True).start()
        return True

    def _set_status(self, st):
        self.state_label.set_text(st["state"])
        self.state_big.set_text(st["state"].capitalize())
        self.state_detail.set_text(ctl.format_status(st))
        self._update_buttons()
        return False

    # ---------------------------------------------------------------- acciones
    def _append(self, text):
        buf = self.log.get_buffer()
        buf.insert(buf.get_end_iter(), text)
        self.log.scroll_to_mark(buf.create_mark(None, buf.get_end_iter(), False), 0.0, False, 0, 0)
        return False

    def _run(self, cmd, on_done=None, success_marker=None, on_marker=None):
        if self.proc is not None and self.proc.poll() is None:
            return
        self._append("$ " + " ".join(cmd) + "\n")
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        except OSError as e:
            self._append(f"No se pudo lanzar: {e}\n")
            return
        self._update_buttons()
        proc = self.proc

        def reader():
            ok = False
            for line in proc.stdout:
                GLib.idle_add(self._append, line)
                if success_marker and success_marker in line and not ok:
                    ok = True
                    if on_marker:
                        GLib.idle_add(on_marker)
            rc = proc.wait()
            GLib.idle_add(self._finished, rc, ok, on_done)
        threading.Thread(target=reader, daemon=True).start()

    def _finished(self, rc, ok, on_done):
        self._append(f"[terminó con código {rc}]\n")
        if on_done:
            on_done(rc, ok)
        self._update_buttons()
        self.refresh_checks()
        return False

    def _on_pair(self, *_):
        sym = self.selected_symbols()
        self.toast.add_toast(Adw.Toast(title=f"En el GamePad: botón SYNC y los símbolos {ctl.show_symbols(sym)}", timeout=0))
        # Al emparejar empieza la transmisión (modo de la pestaña Transmitir) si no falta nada más para ella
        mode = self.mode_row.get_selected_item().get_string()
        stop = [c for c in ctl.blocking(self.start_checks) if c.key != "paired"]
        start_mode = None if stop else mode

        def emparejado():
            self.toast.add_toast(Adw.Toast(title="¡GamePad emparejado! Empieza la transmisión" if start_mode
                                           else "¡GamePad emparejado!"))
            if start_mode:
                self.stack.set_visible_child_name("start")
            return False

        self._run(ctl.pair_command(self.host, self.selected_iface(), sym, gui=True, start_mode=start_mode),
                  lambda rc, ok: None if ok else self.toast.add_toast(Adw.Toast(title="No se completó el emparejamiento")),
                  success_marker="GamePad emparejado", on_marker=emparejado)

    def _on_start(self, *_):
        mode = self.mode_row.get_selected_item().get_string()
        self._run(ctl.start_command(self.host, self.selected_iface(), mode, gui=True))
        self.stack.set_visible_child_name("start")

    def _on_stop(self, *_):
        subprocess.Popen(ctl.stop_command(self.host, gui=True))
        self._append("Deteniendo la transmisión…\n")

    def _on_close(self, *_):
        if self.proc is not None and self.proc.poll() is None:
            dlg = Adw.AlertDialog(heading="Hay una acción en marcha",
                                  body="Si cierras la ventana, la transmisión o el emparejamiento siguen corriendo. "
                                       "Detenlos con «Detener» o desde la terminal (drc_ctl.py stop).")
            dlg.add_response("ok", "Entendido")
            dlg.present(self)
        return False


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id="local.wiiu.drc_gui")

    def do_activate(self):
        prov = Gtk.CssProvider()
        prov.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        win = self.get_active_window() or Window(self)
        win.present()


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
