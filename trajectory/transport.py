"""ADB transports are serial-specific; capabilities remain in memory."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


def find_adb(explicit=None):
    sdk = os.environ.get('ANDROID_HOME') or os.environ.get('ANDROID_SDK_ROOT')
    choices = [explicit, os.environ.get('DAXQO_ADB'), shutil.which('adb')]
    if sdk:
        choices.append(str(Path(sdk) / 'platform-tools' / ('adb.exe' if os.name == 'nt' else 'adb')))
    for choice in choices:
        if choice and Path(choice).is_file():
            return str(Path(choice).resolve())
    raise RuntimeError('ADB unavailable: set DAXQO_ADB or --adb')


class Adb:
    def __init__(self, path=None):
        self.path = find_adb(path)

    def run(self, serial, *args, timeout=30):
        if serial is not None and not re.fullmatch(r'[A-Za-z0-9_.:\-]+', serial):
            raise ValueError('Invalid device serial')
        result = subprocess.run([self.path, *(['-s', serial] if serial else []), *map(str, args)],
                                capture_output=True, timeout=timeout)
        if result.returncode:
            # Do not return stderr: shell/intent errors can echo capabilities.
            raise RuntimeError('ADB operation failed: ' + str(args[0] if args else 'unknown'))
        return result.stdout.decode('utf-8', errors='replace').strip()

    def devices(self):
        devices = []
        for line in self.run(None, 'devices', '-l').splitlines()[1:]:
            parts = line.split()
            if len(parts) < 2:
                continue
            props = dict(p.split(':', 1) for p in parts[2:] if ':' in p)
            devices.append({'serial': parts[0], 'connection': parts[1], **props})
        return devices

    def package(self, serial):
        installed = set(self.run(serial, 'shell', 'pm', 'list', 'packages', 'io.daxqo').splitlines())
        model = self.run(serial, 'shell', 'getprop', 'ro.product.manufacturer').lower()
        preferred = ['io.daxqo.xm', 'io.daxqo.app'] if any(x in model for x in ('xiaomi', 'redmi')) else ['io.daxqo.app', 'io.daxqo.xm']
        return next((p for p in preferred if 'package:' + p in installed), None)


class DeviceClient:
    """Use the established debug pairing or an explicit environment capability.

    Debug run-as reads only this DAXQO APK's pairing preference, never model keys.
    Release builds require DAXQO_CAPABILITY_<serial> or an explicitly supplied token.
    """
    def __init__(self, adb: Adb, serial: str, package=None, token=None):
        self.adb, self.serial = adb, serial
        self.package = package or adb.package(serial)
        if not self.package or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+', self.package):
            raise RuntimeError('Install the appropriate DAXQO APK first')
        self.port = None
        self.token = token or os.environ.get('DAXQO_CAPABILITY_' + re.sub(r'[^A-Za-z0-9]', '_', serial))
        if not self.token:
            try:
                raw = adb.run(serial, 'exec-out', 'run-as', self.package, 'cat', 'shared_prefs/pairing_private.xml')
                entry = ET.fromstring(raw).find("string[@name='capability']")
                self.token = entry.text if entry is not None else None
            except Exception:
                raise RuntimeError('DAXQO pairing unavailable; pair the app or provide a local capability') from None
        if not isinstance(self.token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,512}', self.token):
            raise RuntimeError('DAXQO pairing unavailable')
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.port = int(adb.run(serial, 'forward', 'tcp:0', 'tcp:18883'))

    def request(self, path, body=None, timeout=18):
        if not path.startswith(('/api/device/', '/api/trajectory/')):
            raise ValueError('Unsupported route')
        data = json.dumps(body, ensure_ascii=False).encode('utf-8') if body is not None else None
        request = urllib.request.Request('http://127.0.0.1:' + str(self.port) + path, data=data,
                  headers={'Host': '127.0.0.1:18883', 'Authorization': 'Bearer ' + self.token,
                           'Content-Type': 'application/json', 'Connection': 'close'})
        try:
            with self.opener.open(request, timeout=timeout) as response:
                raw = response.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise RuntimeError('Device response exceeded limit')
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise RuntimeError('Invalid device response')
            return value
        except Exception as error:
            raise RuntimeError('DAXQO bridge request failed (' + type(error).__name__ + ')') from None

    def action(self, action, args=None, context=None, action_id=None):
        import uuid
        ident = action_id or str(uuid.uuid4())
        return self.request('/api/device/execute', {'id': ident, 'action': action,
                 'args': args or {}, **({'trajectory': context} if context else {})})

    def service_request(self, target, path, body=None, timeout=45):
        """Reuse the paired Harness APIs; do not read model configuration files."""
        port = {'agent': 18880, 'runtime': 18882}.get(target)
        if port is None or not path.startswith('/api/'):
            raise ValueError('Unsupported local service')
        forwarded = self.adb.run(self.serial, 'forward', 'tcp:0', 'tcp:' + str(port))
        try:
            raw = json.dumps(body, ensure_ascii=False).encode('utf-8') if body is not None else None
            request = urllib.request.Request('http://127.0.0.1:' + forwarded + path, data=raw,
                    headers={'Host': '127.0.0.1:' + str(port), 'Authorization': 'Bearer ' + self.token,
                             'Content-Type': 'application/json', 'Connection': 'close'})
            with self.opener.open(request, timeout=timeout) as response:
                value = response.read(5 * 1024 * 1024 + 1)
            if len(value) > 5 * 1024 * 1024: raise RuntimeError('Service response exceeded limit')
            return json.loads(value)
        except Exception as error:
            raise RuntimeError('DAXQO local service request failed (' + type(error).__name__ + ')') from None
        finally:
            self.adb.run(self.serial, 'forward', '--remove', 'tcp:' + forwarded)

    def close(self):
        if self.port is not None:
            try:
                self.adb.run(self.serial, 'forward', '--remove', 'tcp:' + str(self.port))
            finally:
                self.port = None
                self.token = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None
