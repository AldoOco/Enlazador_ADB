# Enlazador_ADB
Herramienta de escritorio para gestionar la depuración inalámbrica ADB en dispositivos Android 11 o superior

* @author Aldo Ocotoxtle Coyotl - aldo.ocotoxtle@gmail.com
* @version 1.0.12

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

  Se tiene que tener el scrcpy configurado en las variables de entorno

COMPATIBILIDAD
--------------
  Android 11 o superior (API 30+) — requiere depuración inalámbrica TLS
  Python 3.8+, Windows/Linux/macOS
