#!/usr/bin/env python3
"""
enlazadorADB.py — Interfaz gráfica ADB Inalámbrico · Android 11+
* @author Aldo Ocotoxtle Coyotl - aldo.ocotoxtle@gmail.com
* @version 1.0.12
==================================================================

DESCRIPCIÓN
-----------
Herramienta de escritorio para gestionar la depuración inalámbrica ADB
en dispositivos Android 11 o superior (protocolo TLS via mDNS/Zeroconf).

Permite:
  - Emparejar dispositivos escaneando un QR generado por el script
  - Activar la depuración inalámbrica en dispositivos conectados por USB
  - Visualizar y gestionar todos los dispositivos ADB conectados
  - Proyectar la pantalla de uno o varios dispositivos con scrcpy
  - Desconectar dispositivos individualmente o en lote

FLUJO DE EMPAREJAMIENTO (QR)
-----------------------------
  1. El script genera un nombre de servicio y contraseña aleatorios
  2. Los codifica en un QR con formato: WIFI:T:ADB;S:<servicio>;P:<contraseña>;;
  3. Escucha via mDNS el servicio _adb-tls-pairing._tcp.local.
  4. Cuando el dispositivo escanea el QR, anuncia ese servicio con su IP y puerto
  5. El script ejecuta: adb pair <IP>:<puerto> <contraseña>
  6. Tras el emparejamiento, escucha _adb-tls-connect._tcp.local.
  7. Ejecuta: adb connect <nombre-mDNS> para establecer la sesión de depuración

FLUJO DE ACTIVACIÓN USB
------------------------
  1. El dispositivo se conecta al PC con cable USB
  2. El usuario acepta el diálogo de autorización en el dispositivo
  3. El script activa: settings put global adb_wifi_enabled 1
  4. Reinicia adbd y lee el puerto TLS desde: getprop service.adb.tls.port
  5. Ejecuta: adb connect <IP>:<puerto TLS>

IDENTIFICACIÓN DE DISPOSITIVOS
--------------------------------
  Los dispositivos mDNS se identifican por su nombre de registro completo:
    adb-<SERIAL>-<SUFIJO>._adb-tls-connect._tcp
  Este nombre es único por dispositivo y persiste mientras adbd esté activo.

DEPENDENCIAS
------------
  pip install qrcode zeroconf pillow
  Opcional: scrcpy (https://github.com/Genymobile/scrcpy) para proyección

COMPATIBILIDAD
--------------
  Android 11 o superior (API 30+) — requiere depuración inalámbrica TLS
  Python 3.8+, Windows/Linux/macOS
"""

import subprocess, secrets, string, threading, time, sys, re
import tkinter as tk
from tkinter import ttk, messagebox

try:
    import qrcode
    from PIL import Image, ImageTk
    from zeroconf import ServiceBrowser, Zeroconf
except ImportError:
    root = tk.Tk(); root.withdraw()
    messagebox.showerror("Dependencias faltantes",
        "Instala:\n\npip install qrcode zeroconf pillow")
    sys.exit(1)

ADB_PAIRING_SERVICE = "_adb-tls-pairing._tcp.local."
ADB_CONNECT_SERVICE = "_adb-tls-connect._tcp.local."
PAIR_TIMEOUT        = 90
CONNECT_TIMEOUT     = 15
POLL_INTERVAL_MS    = 3000
QR_SIZE             = 260

C = {
    "bg": "#1a1a1a", "surface": "#242424", "surface2": "#2d2d2d",
    "border": "#3a3a3a", "accent": "#00c8b4", "accent_dim": "#007f74",
    "text": "#e8e8e8", "muted": "#888888",
    "success": "#4caf7d", "warning": "#f0a030", "error": "#e05555",
    "purple": "#a78bfa", "blue": "#60a5fa",
}

# ── Helpers ADB ───────────────────────────────────────────────────────────────
def run_adb(*args):
    """Ejecuta un comando adb con los argumentos dados.
    Retorna (código_retorno, salida_combinada_stdout_stderr).
    Maneja FileNotFoundError si adb no está en el PATH.
    """
    try:
        r = subprocess.run(["adb"] + list(args), capture_output=True, text=True, timeout=10)
        return r.returncode, (r.stdout + r.stderr).strip()
    except FileNotFoundError:
        return -1, "ADB no encontrado en el PATH"
    except subprocess.TimeoutExpired:
        return -1, "Tiempo de espera agotado"

def run_adb_shell(serial, *args):
    """Ejecuta un comando adb shell en el dispositivo con el serial dado.
    Retorna (código_retorno, salida). Equivale a: adb -s <serial> shell <args>.
    """
    try:
        r = subprocess.run(["adb", "-s", serial, "shell"] + list(args),
                           capture_output=True, text=True, timeout=10)
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as e:
        return -1, str(e)

def scrcpy_available():
    """Verifica si scrcpy está instalado y accesible en el PATH.
    Retorna True si scrcpy --version responde correctamente.
    """
    try:
        r = subprocess.run(["scrcpy", "--version"], capture_output=True, text=True, timeout=5)
        return r.returncode == 0
    except FileNotFoundError:
        return False

_device_name_cache: dict = {}

def get_device_name(serial):
    """Obtiene el nombre personalizado del dispositivo (con cache).
    Consulta en orden: settings get global device_name → getprop net.hostname.
    El resultado se guarda en _device_name_cache para evitar llamadas ADB
    repetidas en cada ciclo de refresco de la tabla.
    """
    if serial in _device_name_cache:
        return _device_name_cache[serial]
    _, out = run_adb("-s", serial, "shell", "settings", "get", "global", "device_name")
    out = out.strip()
    if out and out != "null" and not out.startswith("Error"):
        _device_name_cache[serial] = out
        return out
    _, out2 = run_adb("-s", serial, "shell", "getprop", "net.hostname")
    out2 = out2.strip()
    if out2 and out2 != "localhost" and not out2.startswith("Error"):
        _device_name_cache[serial] = out2
        return out2
    _device_name_cache[serial] = "—"
    return "—"

def invalidate_name_cache(serial=None):
    """Limpia el caché de nombres de dispositivos.
    Si se pasa serial, elimina solo esa entrada.
    Sin argumentos, limpia todo el caché (útil al reconectar).
    """
    if serial:
        _device_name_cache.pop(serial, None)
    else:
        _device_name_cache.clear()

def extract_serial(mdns_name):
    """Extrae el serial del dispositivo desde un nombre de servicio mDNS.
    Ejemplo: "adb-25707A0089-Z9ZI1q._adb-tls-connect._tcp" → "25707A0089".
    Retorna None si el formato no coincide.
    """
    m = re.match(r"adb-([A-Za-z0-9]+)-[A-Za-z0-9]+\._adb-tls", mdns_name)
    return m.group(1) if m else "—"

def get_devices():
    """Obtiene la lista de dispositivos ADB conectados con metadatos.

    Parsea la salida de "adb devices -l" y clasifica cada entrada en:
      - mDNS: dispositivos Android 11+ conectados via TLS inalámbrico
      - IP:   dispositivos conectados via TCP/IP clásico (puerto 5555)
      - USB:  dispositivos conectados por cable

    Deduplicación: si un dispositivo mDNS y una entrada IP apuntan a la
    misma dirección, se omite la entrada IP para evitar duplicados.

    Retorna lista de dicts con claves:
      addr, serial, status, model, via, name
    """
    _, out = run_adb("devices", "-l")
    mdns_entries = {}
    ip_entries   = {}
    usb_entries  = []

    for line in out.splitlines()[1:]:
        line = line.strip()
        if not line or "offline" in line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        addr   = parts[0]
        status = parts[1]
        model  = next((p.replace("model:", "") for p in parts if p.startswith("model:")), "—")

        if "._adb-tls-connect._tcp" in addr:
            s = extract_serial(addr)
            mdns_entries[s] = {"addr": addr, "serial": s, "status": status, "model": model, "via": "mDNS", "name": "—"}
        elif re.match(r"\d+\.\d+\.\d+\.\d+:\d+", addr):
            ip_entries[addr] = {"addr": addr, "serial": "—", "status": status, "model": model, "via": "IP", "name": "—"}
        else:
            usb_entries.append({"addr": addr, "serial": addr, "status": status, "model": model, "via": "USB", "name": "—"})

    result = list(mdns_entries.values())
    for addr, entry in ip_entries.items():
        ip_has_mdns = any(addr.split(":")[0] in e["addr"] for e in mdns_entries.values())
        if not ip_has_mdns:
            result.append(entry)
    result.extend(usb_entries)

    # Obtener nombre personalizado para cada dispositivo (usa caché)
    for d in result:
        ref = d["serial"] if d["via"] != "IP" else d["addr"]
        if ref and ref != "—":
            d["name"] = get_device_name(ref)
    return result

def gen_password(n=6):
    """Genera una contraseña aleatoria de n caracteres alfanuméricos.
    Se usa como contraseña de emparejamiento ADB en el QR.
    """
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(n))

def gen_service(n=8):
    """Genera un nombre de servicio mDNS aleatorio con prefijo "adb-".
    Se usa como nombre del servicio de emparejamiento en el QR.
    """
    return "adb-" + "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(n))

_scrcpy_procs: dict = {}
_scrcpy_lock = threading.Lock()

def launch_scrcpy(addr, serial, log_fn):
    """Lanza scrcpy en un proceso separado para proyectar la pantalla del dispositivo.

    Usa --serial <addr> para que scrcpy se conecte al dispositivo correcto
    incluso cuando hay múltiples dispositivos conectados simultáneamente.
    El proceso se registra en _scrcpy_procs protegido por _scrcpy_lock
    para evitar condiciones de carrera al lanzar varios en paralelo.

    Espera 1.5s tras el inicio para detectar fallos inmediatos y los
    reporta via log_fn con el mensaje de error de scrcpy.
    """
    with _scrcpy_lock:
        if addr in _scrcpy_procs and _scrcpy_procs[addr].poll() is None:
            log_fn(f"scrcpy ya esta activo para {serial or addr}", C["warning"])
            return
    label = serial or addr
    cmd = ["scrcpy", "--serial", addr,
           "--window-title", f"ADB · {label}", "--stay-awake"]
    log_fn(f"Lanzando scrcpy para {label}...", C["purple"])
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        with _scrcpy_lock:
            _scrcpy_procs[addr] = proc
        time.sleep(1.5)
        if proc.poll() is not None:
            err = proc.stderr.read().decode(errors="ignore").strip()
            log_fn(f"scrcpy fallo para {label}: {err[:120] or 'error desconocido'}", C["error"])
            with _scrcpy_lock:
                _scrcpy_procs.pop(addr, None)
        else:
            log_fn(f"scrcpy iniciado (PID {proc.pid}) para {label}", C["success"])
    except FileNotFoundError:
        log_fn("scrcpy no encontrado. Instalalo desde https://github.com/Genymobile/scrcpy", C["error"])
    except Exception as e:
        log_fn(f"Error al lanzar scrcpy para {label}: {e}", C["error"])

def stop_scrcpy(addr, log_fn):
    """Termina el proceso scrcpy asociado al addr dado.
    Usa _scrcpy_lock para acceso seguro al diccionario de procesos.
    """
    with _scrcpy_lock:
        proc = _scrcpy_procs.get(addr)
        if proc and proc.poll() is None:
            proc.terminate()
            log_fn(f"scrcpy detenido para {addr}", C["warning"])
            _scrcpy_procs.pop(addr, None)

def stop_all_scrcpy():
    """Termina todos los procesos scrcpy activos. Se llama al cerrar la app."""
    with _scrcpy_lock:
        for addr, proc in list(_scrcpy_procs.items()):
            if proc.poll() is None:
                proc.terminate()
        _scrcpy_procs.clear()


# ── Ventana USB ───────────────────────────────────────────────────────────────
class UsbDebugWindow(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.title("Habilitar Depuracion Inalambrica via USB")
        self.configure(bg=C["bg"])
        self.resizable(False, False)
        self.grab_set()
        self.focus_set()
        self._parent_log = parent._log_write
        self._usb_checked = set()
        self._all_checked_usb = False
        self._build_ui()
        self._scan_usb()

    def _build_ui(self):
        """Construye la interfaz de la ventana de activación USB.
        Incluye instrucciones paso a paso, tabla de dispositivos USB
        detectados y botones de acción.
        """
        hdr = tk.Frame(self, bg=C["blue"], pady=8)
        hdr.pack(fill="x")
        tk.Label(hdr, text="Habilitar Depuracion Inalambrica via USB",
                 bg=C["blue"], fg="#000000",
                 font=("Segoe UI", 11, "bold")).pack()
        tk.Label(hdr,
                 text="Conecta el dispositivo con cable USB y acepta la autorizacion de depuracion.",
                 bg=C["blue"], fg="#1a1a1a",
                 font=("Segoe UI", 8), wraplength=500, justify="center").pack(pady=(2, 0))

        steps_frame = tk.Frame(self, bg=C["surface"], pady=8, padx=16)
        steps_frame.pack(fill="x", padx=12, pady=(10, 0))
        pasos = [
            ("1", "Conecta el dispositivo al PC con cable USB"),
            ("2", "En el dispositivo: acepta el dialogo 'Permitir depuracion USB'"),
            ("3", "Presiona 'Escanear dispositivos USB' para detectarlo"),
            ("4", "Marca los dispositivos deseados y presiona 'Activar depuracion inalambrica'"),
            ("5", "Una vez activa, desconecta el cable y usa el QR en la ventana principal"),
        ]
        for num, texto in pasos:
            row = tk.Frame(steps_frame, bg=C["surface"])
            row.pack(fill="x", pady=2)
            tk.Label(row, text=num, bg=C["accent"], fg="#000000",
                     font=("Segoe UI", 8, "bold"), width=2, height=1, relief="flat"
                     ).pack(side="left", padx=(0, 8))
            tk.Label(row, text=texto, bg=C["surface"], fg=C["text"],
                     font=("Segoe UI", 9), anchor="w").pack(side="left", fill="x")

        list_frame = tk.LabelFrame(self, text=" Dispositivos USB detectados ",
                                   bg=C["surface"], fg=C["blue"],
                                   font=("Segoe UI", 10, "bold"), relief="groove", bd=1)
        list_frame.pack(fill="both", padx=12, pady=10)

        cols = ("chk", "name", "serial", "model", "status", "wifi_debug")
        self._tree = ttk.Treeview(list_frame, columns=cols, show="headings",
                                   height=5, selectmode="browse")
        self._tree.heading("chk",        text="",              anchor="center")
        self._tree.heading("name",       text="Nombre")
        self._tree.heading("serial",     text="Serial")
        self._tree.heading("model",      text="Modelo")
        self._tree.heading("status",     text="Estado ADB")
        self._tree.heading("wifi_debug", text="Dep. Inalambrica")
        self._tree.column("chk",        width=30,  anchor="center", stretch=False)
        self._tree.column("name",       width=130, anchor="w")
        self._tree.column("serial",     width=110, anchor="w")
        self._tree.column("model",      width=100, anchor="w")
        self._tree.column("status",     width=75,  anchor="center")
        self._tree.column("wifi_debug", width=120, anchor="center")
        self._tree.pack(fill="both", expand=True, padx=8, pady=8)
        self._tree.bind("<ButtonRelease-1>", self._on_usb_click)
        self._tree.heading("chk", command=self._toggle_all_usb)

        style = ttk.Style()
        style.configure("Treeview",
                         background=C["surface2"], fieldbackground=C["surface2"],
                         foreground=C["text"], rowheight=26, font=("Segoe UI", 9))
        style.configure("Treeview.Heading",
                         background=C["border"], foreground=C["blue"],
                         font=("Segoe UI", 9, "bold"), relief="flat")
        style.map("Treeview", background=[("selected", C["accent_dim"])])

        self._log_var = tk.StringVar(value="Esperando accion...")
        self._log_lbl = tk.Label(list_frame, textvariable=self._log_var,
                                  bg=C["surface"], fg=C["muted"],
                                  font=("Consolas", 8), wraplength=460, justify="left")
        self._log_lbl.pack(fill="x", padx=8, pady=(0, 6))

        btn_row = tk.Frame(self, bg=C["bg"])
        btn_row.pack(fill="x", padx=12, pady=(0, 12))

        tk.Button(btn_row, text="Escanear dispositivos USB",
                  bg=C["blue"], fg="#000000", activebackground="#3b82f6",
                  font=("Segoe UI", 9, "bold"), relief="flat", cursor="hand2", pady=6,
                  command=self._scan_usb
                  ).pack(side="left", fill="x", expand=True, padx=(0, 6))

        self._btn_activate = tk.Button(btn_row,
                  text="Activar Depuracion Inalambrica",
                  bg=C["accent"], fg="#000000", activebackground=C["accent_dim"],
                  font=("Segoe UI", 9, "bold"), relief="flat", cursor="hand2", pady=6,
                  command=self._activate_selected)
        self._btn_activate.pack(side="left", fill="x", expand=True, padx=(0, 6))

        tk.Button(btn_row, text="Cerrar",
                  bg=C["surface2"], fg=C["text"], activebackground=C["border"],
                  font=("Segoe UI", 9), relief="flat", cursor="hand2", pady=6,
                  command=self.destroy
                  ).pack(side="left", fill="x", expand=True)

    def _set_log(self, msg, color=None):
        """Actualiza el texto del label de log en la ventana USB."""
        self._log_var.set(msg)
        self._log_lbl.configure(fg=color or C["muted"])

    def _toggle_all_usb(self):
        """Alterna entre marcar todos / desmarcar todos en la tabla USB."""
        self._all_checked_usb = not self._all_checked_usb
        self._usb_checked.clear()
        sym = "☑" if self._all_checked_usb else "☐"
        for iid in self._tree.get_children():
            vals   = self._tree.item(iid)["values"]
            serial = vals[2] if len(vals) > 2 else None
            if serial:
                self._tree.set(iid, "chk", sym)
                if self._all_checked_usb:
                    self._usb_checked.add(serial)
        self._tree.heading("chk", text="☑" if self._all_checked_usb else "")

    def _on_usb_click(self, event):
        """Maneja clics en la tabla USB. Si el clic fue en la columna
        de checkbox (#1), alterna el estado marcado/desmarcado del dispositivo.
        """
        region = self._tree.identify_region(event.x, event.y)
        col    = self._tree.identify_column(event.x)
        iid    = self._tree.identify_row(event.y)
        if not iid or region != "cell" or col != "#1":
            return
        vals   = self._tree.item(iid)["values"]
        serial = vals[2]   # col 0=chk, 1=name, 2=serial
        if serial in self._usb_checked:
            self._usb_checked.discard(serial)
            self._tree.set(iid, "chk", "☐")
            self._all_checked_usb = False
            self._tree.heading("chk", text="")
        else:
            self._usb_checked.add(serial)
            self._tree.set(iid, "chk", "☑")

    def _get_usb_targets(self):
        """Retorna lista de seriales a operar.
        Prioridad: dispositivos marcados con checkbox → dispositivo seleccionado.
        """
        if self._usb_checked:
            return list(self._usb_checked)
        sel = self._tree.selection()
        if sel:
            return [self._tree.item(sel[0])["values"][2]]
        return []

    def _scan_usb(self):
        """Escanea los dispositivos USB conectados via "adb devices -l".
        Filtra entradas IP y mDNS — solo muestra dispositivos físicos USB.
        Consulta el estado de depuración inalámbrica de cada uno.
        """
        self._tree.delete(*self._tree.get_children())
        _, out = run_adb("devices", "-l")
        found = 0
        for line in out.splitlines()[1:]:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            addr   = parts[0]
            status = parts[1]
            model  = next((p.replace("model:", "") for p in parts if p.startswith("model:")), "—")
            is_ip   = re.match(r"\d+\.\d+\.\d+\.\d+:\d+", addr)
            is_mdns = "._adb-tls" in addr
            if is_ip or is_mdns:
                continue
            _, prop = run_adb_shell(addr, "getprop", "service.adb.tls.port")
            tls_active = prop.strip() and prop.strip() != "0"
            _, prop2 = run_adb_shell(addr, "settings", "get", "global", "adb_wifi_enabled")
            wifi_enabled = prop2.strip() == "1"
            if tls_active:
                wifi_status = f"Activa (puerto {prop.strip()})"
                color_tag = "active"
            elif wifi_enabled:
                wifi_status = "Habilitada (iniciando...)"
                color_tag = "active"
            else:
                wifi_status = "Inactiva"
                color_tag = "inactive"
            dev_name = get_device_name(addr)
            chk_sym = "☑" if addr in self._usb_checked else "☐"
            self._tree.insert("", "end",
                              values=(chk_sym, dev_name, addr, model, status, wifi_status),
                              tags=(color_tag,))
            found += 1

        self._tree.tag_configure("active",   foreground=C["success"])
        self._tree.tag_configure("inactive", foreground=C["muted"])

        if found == 0:
            self._set_log("No se detectaron dispositivos USB. Conecta el cable y acepta la autorizacion.", C["warning"])
        else:
            self._set_log(f"{found} dispositivo(s) USB detectado(s). Marca los que deseas activar.", C["success"])

    def _activate_selected(self):
        """Lanza la activación de depuración inalámbrica en los dispositivos
        marcados (o el seleccionado). Ejecuta _activate_one por cada serial
        en un thread separado para no bloquear la UI.
        """
        targets = self._get_usb_targets()
        if not targets:
            messagebox.showinfo("Sin seleccion",
                "Marca o selecciona al menos un dispositivo.", parent=self)
            return
        self._btn_activate.configure(state="disabled")
        self._set_log(f"Activando {len(targets)} dispositivo(s)...", C["warning"])

        def _run_all():
            for serial in targets:
                self._activate_one(serial)
            self.after(0, self._scan_usb)
            self.after(0, lambda: self._btn_activate.configure(state="normal"))

        threading.Thread(target=_run_all, daemon=True).start()

    def _activate_one(self, serial):
        """Activa la depuración inalámbrica TLS en un dispositivo USB específico.

        Pasos:
          1. Habilita development_settings_enabled y adb_wifi_enabled
          2. Reinicia adbd con ctl.restart
          3. Detecta la IP del dispositivo via "ip route"
          4. Lee el puerto TLS desde service.adb.tls.port
          5. Ejecuta adb connect <IP>:<puerto TLS>
        Si no obtiene puerto TLS, instruye al usuario a activarlo manualmente.
        """
        errors = []
        run_adb("-s", serial, "shell", "settings", "put", "global",
                "development_settings_enabled", "1")
        code, out = run_adb("-s", serial, "shell", "settings", "put",
                            "global", "adb_wifi_enabled", "1")
        if code != 0:
            errors.append(f"adb_wifi_enabled: {out}")
        run_adb("-s", serial, "shell", "setprop", "ctl.restart", "adbd")
        time.sleep(2)

        _, ip_out = run_adb_shell(serial, "ip", "route")
        ip = None
        for line in ip_out.splitlines():
            m = re.search(r"src (\d+\.\d+\.\d+\.\d+)", line)
            if m and not m.group(1).startswith("127."):
                ip = m.group(1)
                break

        _, port_out = run_adb_shell(serial, "getprop", "service.adb.tls.port")
        tls_port = port_out.strip() if port_out.strip() and port_out.strip() != "0" else None

        connected = False
        if ip and tls_port:
            c, o = run_adb("connect", f"{ip}:{tls_port}")
            if "connected" in o.lower():
                connected = True
                msg = f"Dep. inalambrica activa (TLS)\n   {ip}:{tls_port} -> {o}"
                self.after(0, lambda msg=msg: self._set_log(msg, C["success"]))
                self._parent_log(f"USB->WiFi TLS: {serial} -> {ip}:{tls_port}", C["success"])

        if not connected:
            hint = ("\nEl comando se envio pero requiere confirmacion manual.\n"
                    "En el dispositivo: Ajustes -> Opciones de desarrollador\n"
                    "-> Depuracion inalambrica (activa el toggle manualmente).\n"
                    "Luego usa 'Generar QR y Emparejar' en la ventana principal.")
            if errors:
                hint += f"\nDetalle: {'; '.join(errors)}"
            msg = f"[{serial}] No se conecto automaticamente.{hint}"
            self.after(0, lambda msg=msg: self._set_log(msg, C["warning"]))
            self._parent_log(f"USB->WiFi: {serial} - activa Dep. Inalambrica manualmente", C["warning"])


# ── App principal ─────────────────────────────────────────────────────────────
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ADB Inalambrico - Android 11+")
        self.configure(bg=C["bg"])
        self.resizable(True, True)
        self.minsize(940, 620)

        self._pairing           = False
        self._password          = ""
        self._service           = ""
        self._pair_host         = None
        self._pair_lock         = threading.Event()
        self._conn_lock         = threading.Event()
        self._qr_photo          = None
        self._scrcpy_ok         = scrcpy_available()
        self._iid_to_addr       = {}
        self._addr_checked_main = set()
        self._checked_main      = set()

        # Polling en background: get_devices() corre en un thread separado
        # para no bloquear la UI. _devices_cache se actualiza atomicamente.
        self._devices_cache     = []
        self._devices_lock      = threading.Lock()
        self._poll_stop         = threading.Event()
        self._poll_thread       = threading.Thread(target=self._poll_devices_bg,
                                                    daemon=True, name="DevicePoll")
        self._build_ui()

        # Iniciar polling y primer refresco de tabla
        self._poll_thread.start()
        self._schedule_ui_refresh()

        # Cierre limpio
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_close(self):
        """Cierre limpio de la aplicación.

        Antes de destruir la ventana:
          1. Señala al thread de polling que debe detenerse
          2. Termina todos los procesos scrcpy activos
          3. Espera hasta 2s a que el thread de polling termine
          4. Destruye la ventana
        """
        self._log_write("Cerrando aplicacion...", C["muted"])
        self._poll_stop.set()
        stop_all_scrcpy()
        self._poll_thread.join(timeout=2)
        self.destroy()

    def _poll_devices_bg(self):
        """Thread de polling en background (se ejecuta continuamente).

        Llama a get_devices() cada POLL_INTERVAL_MS milisegundos en un
        thread separado para que la UI nunca se congele mientras ADB
        responde. El resultado se guarda en _devices_cache protegido
        por _devices_lock y la UI lo consume en el hilo principal.
        """
        while not self._poll_stop.is_set():
            devices = get_devices()
            with self._devices_lock:
                self._devices_cache = devices
            self._poll_stop.wait(POLL_INTERVAL_MS / 1000)

    def _schedule_ui_refresh(self):
        """Programa el refresco de la tabla en el hilo de UI cada POLL_INTERVAL_MS.
        Lee _devices_cache (ya calculado por el thread de fondo) y actualiza
        la Treeview sin hacer ninguna llamada ADB en el hilo principal.
        """
        self._refresh_devices_ui()
        if not self._poll_stop.is_set():
            self.after(POLL_INTERVAL_MS, self._schedule_ui_refresh)

    def _build_ui(self):
        """Construye la ventana principal: header, panel QR (izquierda),
        tabla de dispositivos (derecha) y área de log (abajo).
        """
        hdr = tk.Frame(self, bg=C["surface"], pady=10)
        hdr.pack(fill="x")
        tk.Label(hdr, text="ADB Inalambrico  -  Android 11+",
                 bg=C["surface"], fg=C["accent"],
                 font=("Segoe UI", 14, "bold")).pack()
        scrcpy_status = "scrcpy disponible" if self._scrcpy_ok else "scrcpy no encontrado"
        tk.Label(hdr, text=scrcpy_status, bg=C["surface"],
                 fg=C["success"] if self._scrcpy_ok else C["warning"],
                 font=("Segoe UI", 9)).pack()

        body = tk.PanedWindow(self, orient="horizontal", bg=C["bg"],
                              sashwidth=6, sashrelief="flat", bd=0)
        body.pack(fill="both", expand=True, padx=12, pady=10)
        left  = tk.Frame(body, bg=C["bg"])
        right = tk.Frame(body, bg=C["bg"])
        body.add(left,  minsize=320, width=340)
        body.add(right, minsize=500)

        self._build_qr_panel(left)
        self._build_devices_panel(right)
        self._build_log()

    def _build_qr_panel(self, parent):
        """Construye el panel izquierdo con canvas QR, barra de progreso,
        botón de emparejamiento y botón de activación USB.
        """
        frame = tk.LabelFrame(parent, text=" Emparejar via QR ",
                              bg=C["surface"], fg=C["accent"],
                              font=("Segoe UI", 10, "bold"), relief="groove", bd=1)
        frame.pack(fill="both", expand=True, pady=(0, 6))

        qr_container = tk.Frame(frame, bg=C["surface"],
                                width=QR_SIZE + 20, height=QR_SIZE + 20)
        qr_container.pack(pady=(12, 4))
        qr_container.pack_propagate(False)

        self._qr_canvas = tk.Canvas(qr_container, width=QR_SIZE, height=QR_SIZE,
                                    bg=C["surface2"], highlightthickness=1,
                                    highlightbackground=C["border"])
        self._qr_canvas.pack(expand=True)
        self._draw_qr_placeholder()

        self._status_var = tk.StringVar(value="Presiona el boton para iniciar")
        self._status_lbl = tk.Label(frame, textvariable=self._status_var,
                                    bg=C["surface"], fg=C["muted"],
                                    font=("Segoe UI", 9), wraplength=280, justify="center")
        self._status_lbl.pack(pady=(4, 6))

        self._progress = ttk.Progressbar(frame, mode="indeterminate", length=280)
        self._progress.pack(pady=(0, 8))

        self._btn_pair = tk.Button(
            frame, text="Generar QR y Emparejar",
            bg=C["accent"], fg="#000000",
            activebackground=C["accent_dim"], activeforeground="#ffffff",
            font=("Segoe UI", 10, "bold"), relief="flat", cursor="hand2", pady=7,
            command=self._start_pairing)
        self._btn_pair.pack(fill="x", padx=16, pady=(0, 6))

        tk.Button(frame, text="Habilitar via USB",
                  bg=C["blue"], fg="#000000", activebackground="#3b82f6",
                  font=("Segoe UI", 9, "bold"), relief="flat", cursor="hand2", pady=6,
                  command=self._open_usb_window
                  ).pack(fill="x", padx=16, pady=(0, 14))

    def _draw_qr_placeholder(self):
        """Dibuja el estado vacío del canvas QR (ícono + texto indicativo)."""
        cx, cy = QR_SIZE // 2, QR_SIZE // 2
        self._qr_canvas.delete("all")
        self._qr_canvas.create_text(cx, cy - 14, text="📱",
                                    font=("Segoe UI", 36), fill=C["muted"])
        self._qr_canvas.create_text(cx, cy + 30, text="El QR aparecera aqui",
                                    font=("Segoe UI", 10), fill=C["muted"])

    def _render_qr(self, service, password):
        """Genera y renderiza el código QR en el canvas.
        El formato del dato es: WIFI:T:ADB;S:<service>;P:<password>;;
        que Android reconoce como QR de emparejamiento ADB.
        """
        qr_data = f"WIFI:T:ADB;S:{service};P:{password};;"
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M,
                           box_size=8, border=2)
        qr.add_data(qr_data)
        qr.make(fit=True)
        img = qr.make_image(fill_color="#000000", back_color="#ffffff").convert("RGB")
        img = img.resize((QR_SIZE, QR_SIZE), Image.NEAREST)
        self._qr_photo = ImageTk.PhotoImage(img)
        self._qr_canvas.delete("all")
        self._qr_canvas.create_image(QR_SIZE // 2, QR_SIZE // 2,
                                     anchor="center", image=self._qr_photo)

    def _open_usb_window(self):
        UsbDebugWindow(self)

    def _build_devices_panel(self, parent):
        """Construye la tabla principal de dispositivos conectados con
        columnas: checkbox, nombre, modelo, serial, IP/mDNS, vía, estado, pantalla.
        Incluye los botones de acción: refrescar, proyectar, detener, desconectar.
        """
        frame = tk.LabelFrame(parent, text=" Dispositivos conectados ",
                              bg=C["surface"], fg=C["accent"],
                              font=("Segoe UI", 10, "bold"), relief="groove", bd=1)
        frame.pack(fill="both", expand=True, pady=(0, 6))

        tk.Label(frame,
                 text="Haz clic en ☐ para marcar varios dispositivos y operar en lote.",
                 bg=C["surface"], fg=C["muted"], font=("Segoe UI", 8)
                 ).pack(anchor="w", padx=10, pady=(4, 0))

        cols = ("chk", "name", "model", "serial", "address", "via", "status", "screen")
        self._tree = ttk.Treeview(frame, columns=cols, show="headings",
                                   height=11, selectmode="browse")
        self._tree.heading("chk",     text="",              anchor="center")
        self._tree.heading("name",    text="Nombre")
        self._tree.heading("model",   text="Modelo")
        self._tree.heading("serial",  text="Serial")
        self._tree.heading("address", text="IP / mDNS")
        self._tree.heading("via",     text="Via")
        self._tree.heading("status",  text="Estado")
        self._tree.heading("screen",  text="Pantalla")
        self._tree.column("chk",     width=30,  anchor="center", stretch=False)
        self._tree.column("name",    width=120, anchor="w")
        self._tree.column("model",   width=100, anchor="w")
        self._tree.column("serial",  width=95,  anchor="w")
        self._tree.column("address", width=160, anchor="w")
        self._tree.column("via",     width=45,  anchor="center")
        self._tree.column("status",  width=55,  anchor="center")
        self._tree.column("screen",  width=60,  anchor="center")
        self._tree.bind("<ButtonRelease-1>", self._on_main_click)
        self._tree.bind("<Double-1>",        self._on_double_click)
        self._tree.heading("chk", command=self._toggle_all_main)
        self._all_checked_main = False

        sb = ttk.Scrollbar(frame, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y", padx=(0, 4), pady=8)
        self._tree.pack(fill="both", expand=True, padx=(8, 0), pady=(4, 8))

        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Treeview",
                         background=C["surface2"], fieldbackground=C["surface2"],
                         foreground=C["text"], rowheight=28,
                         font=("Segoe UI", 9), borderwidth=0)
        style.configure("Treeview.Heading",
                         background=C["border"], foreground=C["accent"],
                         font=("Segoe UI", 9, "bold"), relief="flat")
        style.map("Treeview", background=[("selected", C["accent_dim"])])

        btn_row = tk.Frame(frame, bg=C["surface"])
        btn_row.pack(fill="x", padx=8, pady=(0, 10))

        tk.Button(btn_row, text="Refrescar",
                  bg=C["surface2"], fg=C["text"], activebackground=C["border"],
                  font=("Segoe UI", 9), relief="flat", cursor="hand2", pady=5,
                  command=self._force_refresh
                  ).pack(side="left", fill="x", expand=True, padx=(0, 3))

        tk.Button(btn_row, text="Proyectar pantalla",
                  bg=C["purple"], fg="#ffffff", activebackground="#7c5fc9",
                  font=("Segoe UI", 9, "bold"), relief="flat", cursor="hand2", pady=5,
                  command=self._project_selected
                  ).pack(side="left", fill="x", expand=True, padx=(0, 3))

        tk.Button(btn_row, text="Detener proyeccion",
                  bg=C["surface2"], fg=C["muted"], activebackground=C["border"],
                  font=("Segoe UI", 9), relief="flat", cursor="hand2", pady=5,
                  command=self._stop_projection_selected
                  ).pack(side="left", fill="x", expand=True, padx=(0, 3))

        tk.Button(btn_row, text="Desconectar",
                  bg=C["error"], fg="#ffffff", activebackground="#b03030",
                  font=("Segoe UI", 9), relief="flat", cursor="hand2", pady=5,
                  command=self._disconnect_selected
                  ).pack(side="left", fill="x", expand=True)

    def _build_log(self):
        """Construye el widget de log en la parte inferior de la ventana.
        Muestra mensajes con timestamp y colores según severidad.
        """
        frame = tk.LabelFrame(self, text=" Log ",
                              bg=C["surface"], fg=C["muted"],
                              font=("Segoe UI", 9), relief="groove", bd=1)
        frame.pack(fill="x", padx=12, pady=(0, 10))
        self._log = tk.Text(frame, bg=C["bg"], fg=C["muted"],
                            font=("Consolas", 8), height=5,
                            relief="flat", state="disabled",
                            wrap="word", insertbackground=C["text"])
        sb = ttk.Scrollbar(frame, command=self._log.yview)
        self._log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self._log.pack(fill="x", padx=4, pady=4)

    def _log_write(self, msg, color=None):
        """Agrega una línea al log con timestamp [HH:MM:SS].
        Hilo-seguro: usa self.after(0, ...) para ejecutar en el hilo de UI.
        """
        def _do():
            self._log.configure(state="normal")
            ts  = time.strftime("%H:%M:%S")
            idx = self._log.index("end")
            self._log.insert("end", f"[{ts}] {msg}\n")
            if color:
                tag = f"clr_{color.replace('#','')}"
                self._log.tag_config(tag, foreground=color)
                self._log.tag_add(tag, idx, self._log.index("end-1c"))
            self._log.see("end")
            self._log.configure(state="disabled")
        self.after(0, _do)

    def _set_status(self, text, color=None):
        def _do():
            self._status_var.set(text)
            self._status_lbl.configure(fg=color or C["muted"])
        self.after(0, _do)

    # ── Checkbox principal ────────────────────────────────────────────────────
    def _toggle_all_main(self):
        """Alterna entre marcar todos / desmarcar todos en la tabla principal."""
        self._all_checked_main = not self._all_checked_main
        self._addr_checked_main.clear()
        self._checked_main.clear()
        sym = "☑" if self._all_checked_main else "☐"
        for iid in self._tree.get_children():
            addr = self._iid_to_addr.get(iid)
            if addr:
                self._tree.set(iid, "chk", sym)
                if self._all_checked_main:
                    self._addr_checked_main.add(addr)
                    self._checked_main.add(iid)
        self._tree.heading("chk", text="☑" if self._all_checked_main else "")

    def _on_main_click(self, event):
        """Maneja clics en la tabla principal. Si el clic cae en la
        columna de checkbox (#1), alterna el estado marcado/desmarcado
        del dispositivo y actualiza _addr_checked_main.
        """
        region = self._tree.identify_region(event.x, event.y)
        col    = self._tree.identify_column(event.x)
        iid    = self._tree.identify_row(event.y)
        if not iid or region != "cell" or col != "#1":
            return
        addr = self._iid_to_addr.get(iid)
        if not addr:
            return
        if addr in self._addr_checked_main:
            self._addr_checked_main.discard(addr)
            self._checked_main.discard(iid)
            self._tree.set(iid, "chk", "☐")
            self._all_checked_main = False
            self._tree.heading("chk", text="")
        else:
            self._addr_checked_main.add(addr)
            self._checked_main.add(iid)
            self._tree.set(iid, "chk", "☑")

    def _get_checked_devices(self):
        """Retorna lista de (addr, serial) de todos los dispositivos marcados."""
        result = []
        for iid in list(self._checked_main):
            try:
                vals   = self._tree.item(iid)["values"]
                addr   = self._iid_to_addr.get(iid)
                serial = vals[3]
                if addr:
                    result.append((addr, serial))
            except Exception:
                pass
        return result

    # ── Refresco de tabla (hilo UI) ───────────────────────────────────────────
    def _force_refresh(self):
        """Fuerza una nueva consulta ADB inmediata invalidando el caché de nombres
        y disparando un refresco de UI en el siguiente ciclo.
        """
        invalidate_name_cache()
        with self._devices_lock:
            self._devices_cache = []
        threading.Thread(target=lambda: (
            setattr(self, '_devices_cache', get_devices()) or
            self.after(0, self._refresh_devices_ui)
        ), daemon=True).start()

    def _refresh_devices_ui(self):
        """Actualiza la Treeview con el contenido de _devices_cache.

        No hace ninguna llamada ADB — solo lee el caché calculado por
        _poll_devices_bg en background. Preserva la selección activa y
        el estado de los checkboxes usando _iid_to_addr y _addr_checked_main.
        """
        with self._devices_lock:
            devices = list(self._devices_cache)

        sel_addr = None
        sel = self._tree.selection()
        if sel:
            sel_addr = self._iid_to_addr.get(sel[0])

        self._iid_to_addr.clear()
        self._checked_main.clear()
        self._tree.delete(*self._tree.get_children())

        for d in devices:
            addr = d["addr"]
            if d["via"] == "mDNS":
                addr_display = addr.split("._adb-tls")[0]
                tag = "ok" if d["status"] == "device" else "warn"
            elif d["via"] == "USB":
                addr_display = addr
                tag = "usb"
            else:
                addr_display = addr
                tag = "ok" if d["status"] == "device" else "warn"

            is_projecting = addr in _scrcpy_procs and _scrcpy_procs[addr].poll() is None
            screen_val = "Activa" if is_projecting else "—"
            if is_projecting:
                tag = "projecting"

            chk_sym = "☑" if addr in self._addr_checked_main else "☐"
            iid = self._tree.insert("", "end",
                              values=(chk_sym, d.get("name", "—"), d["model"], d["serial"],
                                      addr_display, d["via"], d["status"], screen_val),
                              tags=(tag,))
            self._iid_to_addr[iid] = addr
            if addr in self._addr_checked_main:
                self._checked_main.add(iid)
            if sel_addr and sel_addr == addr:
                self._tree.selection_set(iid)

        self._tree.tag_configure("ok",        foreground=C["success"])
        self._tree.tag_configure("warn",       foreground=C["warning"])
        self._tree.tag_configure("usb",        foreground=C["blue"])
        self._tree.tag_configure("projecting", foreground=C["purple"])

    def _get_selected_device(self):
        sel = self._tree.selection()
        if not sel:
            return None, None
        iid    = sel[0]
        vals   = self._tree.item(iid)["values"]
        serial = vals[3]
        addr   = self._iid_to_addr.get(iid, vals[4])
        return addr, serial

    def _on_double_click(self, event):
        col = self._tree.identify_column(event.x)
        if col == "#1":
            return
        self._project_selected()

    def _resolve_targets(self):
        """Devuelve lista de (addr, serial): marcados si hay, si no el seleccionado."""
        targets = self._get_checked_devices()
        if not targets:
            addr, serial = self._get_selected_device()
            if addr:
                targets = [(addr, serial)]
        return targets

    # ── Acciones en lote ──────────────────────────────────────────────────────
    def _project_selected(self):
        if not self._scrcpy_ok:
            messagebox.showwarning("scrcpy no disponible",
                "scrcpy no esta instalado o no esta en el PATH.\n\n"
                "Descargalo en: https://github.com/Genymobile/scrcpy")
            return
        targets = self._resolve_targets()
        if not targets:
            messagebox.showinfo("Sin seleccion",
                "Marca o selecciona al menos un dispositivo.")
            return
        for addr, serial in targets:
            threading.Thread(target=launch_scrcpy,
                             args=(addr, serial, self._log_write), daemon=True).start()

    def _stop_projection_selected(self):
        targets = self._resolve_targets()
        if not targets:
            messagebox.showinfo("Sin seleccion",
                "Marca o selecciona al menos un dispositivo.")
            return
        for addr, _ in targets:
            stop_scrcpy(addr, self._log_write)

    def _disconnect_selected(self):
        targets = self._resolve_targets()
        if not targets:
            messagebox.showinfo("Sin seleccion",
                "Marca o selecciona al menos un dispositivo.")
            return
        for addr, _ in targets:
            stop_scrcpy(addr, self._log_write)
            _, out = run_adb("disconnect", addr)
            self._log_write(f"disconnect {addr} -> {out}", C["warning"])
            self._addr_checked_main.discard(addr)
            invalidate_name_cache(addr)
        self._checked_main.clear()

    # ── Emparejamiento QR ─────────────────────────────────────────────────────
    def _start_pairing(self):
        """Inicia el flujo de emparejamiento QR en un thread separado.
        Genera credenciales nuevas, renderiza el QR y lanza _pair_thread.
        """
        if self._pairing:
            return
        code, _ = run_adb("version")
        if code != 0:
            messagebox.showerror("ADB no encontrado", "Asegurate de que ADB este en el PATH.")
            return
        self._pairing   = True
        self._password  = gen_password()
        self._service   = gen_service()
        self._pair_lock.clear()
        self._conn_lock.clear()
        self._pair_host = None
        self._btn_pair.configure(state="disabled", text="Emparejando...")
        self._progress.start(12)
        self._render_qr(self._service, self._password)
        self._set_status(
            f"Escanea el QR en el dispositivo\n"
            f"Ajustes -> Op. desarrollador -> Depuracion inalambrica -> Vincular con QR\n"
            f"(timeout: {PAIR_TIMEOUT}s)", C["accent"])
        self._log_write(f"QR generado - servicio={self._service}", C["accent"])
        threading.Thread(target=self._pair_thread, daemon=True).start()

    def _pair_thread(self):
        """Thread de emparejamiento ADB via mDNS (se ejecuta en background).

        Fase 1 — Emparejamiento:
          Escucha _adb-tls-pairing._tcp.local. con Zeroconf.
          Al detectar el dispositivo ejecuta: adb pair <IP>:<puerto> <contraseña>.

        Fase 2 — Conexión:
          Escucha _adb-tls-connect._tcp.local. con Zeroconf.
          Al detectar el servicio ejecuta: adb connect <nombre-mDNS>.
          Fallback a adb connect <IP>:<puerto> si el nombre mDNS falla.
        """
        self._log_write("Escuchando mDNS para emparejamiento...")
        zc = Zeroconf()

        class PairListener:
            def add_service(inner, zc_, stype, name):
                info = zc_.get_service_info(stype, name)
                if not info or self._pair_lock.is_set():
                    return
                host = ".".join(str(b) for b in info.addresses[0]) if info.addresses else info.server
                port = info.port
                self._log_write(f"Dispositivo detectado: {host}:{port}")
                self._set_status(f"Emparejando con {host}:{port}...", C["warning"])
                code, out = run_adb("pair", f"{host}:{port}", self._password)
                self._log_write(f"adb pair -> {out}",
                                C["success"] if code == 0 else C["error"])
                if code == 0 or "Successfully paired" in out:
                    self._pair_host = host
                    self._pair_lock.set()
            def remove_service(inner, *a): pass
            def update_service(inner, *a): pass

        ServiceBrowser(zc, ADB_PAIRING_SERVICE, PairListener())
        paired = self._pair_lock.wait(PAIR_TIMEOUT)
        zc.close()

        if not paired:
            self._log_write("Timeout: no se detecto el dispositivo.", C["error"])
            self._set_status("Tiempo agotado. Intenta de nuevo.", C["error"])
            self._finish_pairing(False)
            return

        self._set_status("Emparejado! Esperando conexion mDNS...", C["success"])
        self._log_write("Emparejamiento OK. Esperando servicio de conexion mDNS...")

        zc2 = Zeroconf()
        pair_host = self._pair_host

        class ConnListener:
            def add_service(inner, zc_, stype, name):
                info = zc_.get_service_info(stype, name)
                if not info or self._conn_lock.is_set():
                    return
                host = ".".join(str(b) for b in info.addresses[0]) if info.addresses else info.server
                port = info.port
                if pair_host and host != pair_host:
                    return
                connect_target = name.rstrip(".").replace(".local", "")
                self._log_write(f"Conectando via mDNS: {connect_target}")
                code, out = run_adb("connect", connect_target)
                if code != 0 or "connected" not in out:
                    self._log_write(f"Fallback a IP: {host}:{port}", C["warning"])
                    code, out = run_adb("connect", f"{host}:{port}")
                self._log_write(f"adb connect -> {out}",
                                C["success"] if "connected" in out else C["warning"])
                if "connected" in out:
                    self._conn_lock.set()
            def remove_service(inner, *a): pass
            def update_service(inner, *a): pass

        ServiceBrowser(zc2, ADB_CONNECT_SERVICE, ConnListener())
        connected = self._conn_lock.wait(CONNECT_TIMEOUT)
        zc2.close()

        if connected:
            self._set_status("Dispositivo conectado via mDNS!", C["success"])
            self._log_write("Conexion ADB establecida via mDNS.", C["success"])
        else:
            self._set_status("Emparejado. Conexion automatica no detectada.", C["warning"])
        self._finish_pairing(True)

    def _finish_pairing(self, success):
        """Restaura el estado de la UI tras el emparejamiento (éxito o fallo).
        Detiene la barra de progreso, reactiva el botón y refresca la tabla.
        """
        def _do():
            self._pairing = False
            self._progress.stop()
            self._btn_pair.configure(state="normal", text="Generar QR y Emparejar")
            if not success:
                self._draw_qr_placeholder()
        self.after(0, _do)


if __name__ == "__main__":
    app = App()
    app.mainloop()
