#!/usr/bin/env python3
"""PrivScope — yetkili hesap envanteri.

vCenter'daki açık (poweredOn) VM'lerin yetkili/lokal hesaplarını toplar:
  * Windows -> WinRM (pypsrp), grup üyeleri SID ile okunur
  * Linux   -> SSH (paramiko)
Sonuç tek dosyalık HTML rapor (Domain / Windows / Linux) olarak kaydedilir.

Araç yalnızca okuma yapar. Şifreler sadece bellekte tutulur; diske yazılmaz,
log'a basılmaz.
"""
import html
import json
import os
import queue
import re
import ssl
import subprocess
import tempfile
import threading
import tkinter as tk
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

APP_TITLE = 'PrivScope'

# Kapsam dışı VM adları (appliance / altyapı)
EXCLUDE_RE = re.compile(r'^vCLS-|vcsa|ciscoise|qualys|claroty|hotspot|pamng|photon', re.I)

WIN_COLS = ['VM Adı', 'IP', 'Administrators', 'Remote Desktop Users', 'Local Users',
            'Domain / Workgroup']
LIN_COLS = ['VM Adı', 'IP', 'Root Yetkili (UID 0)', 'wheel / sudo Grup Üyeleri',
            'Sudoers Kuralları', 'Local Users']

# Bir kullanıcının bu sayıdan fazla kısıtlı sudo komutu varsa tek satıra indirilir
SUDO_COLLAPSE_AFTER = 3


# --------------------------------------------------------------------------
# Yardımcılar
# --------------------------------------------------------------------------
class Reporter:
    """Worker thread -> GUI kuyruğu. Bilinen şifreleri log'dan maskeler."""

    def __init__(self, q, secrets):
        self.q = q
        self.secrets = [s for s in secrets if s]
        self.cancel = threading.Event()

    def scrub(self, text):
        text = str(text)
        for s in self.secrets:
            text = text.replace(s, '***')
        return text

    def add_secrets(self, secrets):
        self.secrets += [s for s in secrets if s]

    def log(self, msg):
        self.q.put(('log', self.scrub(msg)))

    def progress(self, done, total):
        self.q.put(('progress', done, total))

    def reason(self, ex):
        """Hata nedenini tek satır, şifresiz ve kısa döndürür."""
        msg = ' '.join(f'{type(ex).__name__}: {ex}'.split())
        return self.scrub(msg)[:200]


def first_ipv4(vm):
    """guest.net'ten ilk kullanılabilir IPv4; yoksa guest.ipAddress (IPv4 ise)."""
    ipv4 = re.compile(r'^\d{1,3}(\.\d{1,3}){3}$')
    for nic in vm.get('guest.net') or []:
        for ip in getattr(nic, 'ipAddress', None) or []:
            if ipv4.match(ip) and not ip.startswith('169.254.'):
                return ip
    ip = vm.get('guest.ipAddress') or ''
    return ip if ipv4.match(ip) else ''


def classify_os(vm):
    """'windows' / 'linux' / None (tanınmayan)."""
    family = (vm.get('guest.guestFamily') or '').lower()
    gid = (vm.get('guest.guestId') or vm.get('config.guestId') or '').lower()
    full = (vm.get('guest.guestFullName') or vm.get('config.guestFullName') or '').lower()
    if family == 'windowsguest' or gid.startswith('windows') or 'windows' in full:
        return 'windows'
    distro = r'(rhel|red ?hat|centos|ubuntu|debian|sles|suse|oracle|rocky|alma|fedora|amazon|coreos|freebsd)'
    if (family == 'linuxguest' or 'linux' in gid or 'linux' in full
            or re.match('^' + distro, gid) or re.search(distro, full)):
        return 'linux'
    return None


# --------------------------------------------------------------------------
# vCenter
# --------------------------------------------------------------------------
def fetch_vms(host, user, pw, rep):
    """poweredOn VM'leri (property collector ile toplu) döndürür."""
    from pyVim.connect import SmartConnect, Disconnect
    from pyVmomi import vim, vmodl

    port = 443
    if ':' in host:
        host, p = host.rsplit(':', 1)
        port = int(p)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    si = SmartConnect(host=host, user=user, pwd=pw, port=port, sslContext=ctx)
    try:
        content = si.RetrieveContent()
        view = content.viewManager.CreateContainerView(
            content.rootFolder, [vim.VirtualMachine], True)
        try:
            props = ['name', 'runtime.powerState', 'guest.guestFamily', 'guest.guestId',
                     'guest.guestFullName', 'config.guestId', 'config.guestFullName',
                     'guest.ipAddress', 'guest.net', 'guest.hostName', 'config.template']
            trav = vim.PropertyCollector.TraversalSpec(
                name='tv', type=vim.view.ContainerView, path='view', skip=False)
            obj_spec = vim.PropertyCollector.ObjectSpec(obj=view, skip=True, selectSet=[trav])
            prop_spec = vim.PropertyCollector.PropertySpec(type=vim.VirtualMachine, pathSet=props)
            fspec = vim.PropertyCollector.FilterSpec(objectSet=[obj_spec], propSet=[prop_spec])
            pc = content.propertyCollector
            res = pc.RetrievePropertiesEx([fspec], vmodl.query.PropertyCollector.RetrieveOptions())
            vms = []
            while res:
                for o in res.objects:
                    vms.append({p.name: p.val for p in o.propSet})
                if not res.token:
                    break
                res = pc.ContinueRetrievePropertiesEx(res.token)
        finally:
            view.Destroy()
    finally:
        Disconnect(si)

    on = [v for v in vms
          if str(v.get('runtime.powerState')) == 'poweredOn' and not v.get('config.template')]
    rep.log(f'vCenter: {len(vms)} VM, {len(on)} tanesi açık.')
    return on


# --------------------------------------------------------------------------
# IP listesi kaynağı (RVTools xlsx / Excel / CSV / txt) — vCenter gerekmez
# --------------------------------------------------------------------------
_IPV4 = re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b')
_IP_H = {'primaryipaddress', 'ipaddress', 'ip', 'ipadresi', 'ipadres', 'host', 'sunucuip'}
_NAME_H = {'vm', 'vmname', 'name', 'sunucu', 'sunucuadi', 'hostname', 'vmadi'}
_POWER_H = {'powerstate'}
_OS_H = ('osaccordingtothevmwaretools', 'osaccordingtotheconfigurationfile', 'os', 'guestos')


_USER_H = {'username', 'user', 'kullanici', 'kullaniciadi', 'login', 'loginname'}
_PASS_H = {'password', 'pass', 'sifre', 'parola', 'pwd'}
_DNS_H = {'dnsname', 'fqdn'}
_ACCT_H = {'accounttype', 'hesapturu', 'hesaptipi', 'hesap'}


def parse_acct(v):
    """'Domain' -> 'domain', 'Lokal'/'Local' -> 'local', diğer -> None (otomatik)."""
    v = _norm(v)
    return 'domain' if v.startswith('dom') else 'local' if v.startswith(('lok', 'loc')) else None
_TR_ASCII = str.maketrans('İıŞşÇçÖöÜüĞğ', 'IiSsCcOoUuGg')


def _norm(s):
    if s is None:
        return ''
    return re.sub(r'[^a-z0-9]', '', str(s).translate(_TR_ASCII).lower())


def rows_to_targets(rows):
    """Satırlardan {name, ip, kind} çıkarır. Başlık bulunamazsa hücrelerdeki IP'leri alır."""
    hi = ci = None
    for i, row in enumerate(rows[:5]):
        h = [_norm(c) for c in row]
        ci = next((j for j, v in enumerate(h) if v in _IP_H), None)
        if ci is not None:
            hi = i
            break
    found = OrderedDict()
    if hi is None:
        for row in rows:
            for cell in row:
                for ip in _IPV4.findall(str(cell or '')):
                    found.setdefault(ip, {'name': ip, 'ip': ip, 'kind': None})
        return list(found.values())

    h = [_norm(c) for c in rows[hi]]
    ni = next((j for j, v in enumerate(h) if v in _NAME_H), None)
    pi = next((j for j, v in enumerate(h) if v in _POWER_H), None)
    oi = [h.index(n) for n in _OS_H if n in h]
    ui = next((j for j, v in enumerate(h) if v in _USER_H), None)
    wi = next((j for j, v in enumerate(h) if v in _PASS_H), None)
    ai = next((j for j, v in enumerate(h) if v in _ACCT_H), None)
    di = next((j for j, v in enumerate(h) if v in _DNS_H), None)

    def cell(row, j):
        return row[j] if j is not None and j < len(row) else None

    for row in rows[hi + 1:]:
        m = _IPV4.search(str(cell(row, ci) or ''))
        if not m or m.group(0).startswith('169.254.'):
            continue
        if pi is not None and 'poweredon' not in _norm(cell(row, pi)):
            continue
        vm = {'guest.guestFullName': ' '.join(str(cell(row, j) or '') for j in oi)}
        ip = m.group(0)
        user = str(cell(row, ui) or '').strip()
        pw = cell(row, wi)
        found.setdefault(ip, {'name': str(cell(row, ni) or ip).strip(), 'ip': ip,
                              'kind': classify_os(vm), 'user': user,
                              'pw': '' if pw is None else str(pw),
                              'acct': parse_acct(cell(row, ai)),
                              'dns': str(cell(row, di) or '').strip()})
    return list(found.values())


def load_targets(path):
    """xlsx/csv/txt dosyasından hedef listesi (vInfo sayfası varsa önce o denenir)."""
    if path.lower().endswith(('.xlsx', '.xlsm')):
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        sheets = sorted(wb.worksheets, key=lambda w: w.title.lower() != 'vinfo')
        for ws in sheets:
            targets = rows_to_targets([list(r) for r in ws.iter_rows(values_only=True)])
            if targets:
                return targets
        return []
    import csv
    with open(path, newline='', encoding='utf-8-sig', errors='replace') as f:
        text = f.read()
    delim = ';' if text.count(';') > text.count(',') else ','
    return rows_to_targets(list(csv.reader(text.splitlines(), delimiter=delim)))


def detect_kind(ip):
    """Port yoklamasıyla OS tahmini: 5985/5986 -> windows, 22 -> linux, yoksa None."""
    import socket

    def is_open(port):
        try:
            socket.create_connection((ip, port), timeout=3).close()
            return True
        except OSError:
            return False
    if is_open(5985) or is_open(5986):
        return 'windows'
    return 'linux' if is_open(22) else None


# --------------------------------------------------------------------------
# Windows (WinRM / pypsrp)
# --------------------------------------------------------------------------
WIN_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
function Members($sid){
  try {
    $g=(New-Object System.Security.Principal.SecurityIdentifier($sid)).Translate([System.Security.Principal.NTAccount]).Value.Split('\')[1]
    $grp=[ADSI]"WinNT://./$g,group"
    foreach($m in @($grp.Invoke('Members'))){
      $p=$m.GetType().InvokeMember('ADsPath','GetProperty',$null,$m,$null)
      $c=$m.GetType().InvokeMember('Class','GetProperty',$null,$m,$null)
      $n=($p -replace '^WinNT://','') -replace '/','\'
      if($n -match '^S-1-5-'){"$n (Silinmis hesap)"}elseif($c -eq 'Group'){"$n [Grup]"}else{$n}
    }
  } catch { "(okunamadi: $($_.Exception.Message))" }
}
"@@ADMIN"; Members 'S-1-5-32-544'
"@@RDP";   Members 'S-1-5-32-555'
"@@USERS"
try { $u = Get-CimInstance Win32_UserAccount -Filter "LocalAccount=True" -ErrorAction Stop }
catch { $u = Get-WmiObject Win32_UserAccount -Filter "LocalAccount=True" }
foreach($x in @($u)){ if($x.Disabled){"$($x.Name) (Disabled)"}else{$x.Name} }
"@@SYS"
try { $cs = Get-CimInstance Win32_ComputerSystem -ErrorAction Stop } catch { $cs = Get-WmiObject Win32_ComputerSystem }
if($cs.PartOfDomain){"Domain: $($cs.Domain)"}else{"Workgroup (lokal): $($cs.Domain)"}
"@@END"
"""


def parse_sections(text):
    """'@@X' işaretçilerine göre bölümlere ayırır -> {X: [satırlar]}."""
    sec, cur = {}, None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith('@@'):
            cur = line[2:]
            sec[cur] = []
        elif cur and line:
            sec[cur].append(line)
    return sec


def _winrm_reason(ex):
    """WinRM hatasını okunur, kısa bir Türkçe sebebe çevirir."""
    text = f'{type(ex).__name__} {ex}'.lower()
    if 'authenticat' in text:
        return 'kimlik doğrulama reddedildi (kullanıcı/şifre bu makinede geçersiz)'
    if 'timeout' in text or 'timed out' in text:
        return 'zaman aşımı (güvenlik duvarı ya da makine yanıt vermiyor)'
    if 'refused' in text or '10061' in text:
        return 'bağlantı reddedildi (port kapalı, WinRM dinlemiyor)'
    return ' '.join(str(ex).split())[:90] or type(ex).__name__


class WinRMError(RuntimeError):
    """unreachable=True: iki port da ağ nedeniyle düştü (kimlik reddi yok) -> ADSI denenebilir."""

    def __init__(self, msg, unreachable):
        super().__init__(msg)
        self.unreachable = unreachable


def winrm_exec(host, user, pw, script):
    """PowerShell'i WinRM ile çalıştırır, '@@' bölümlerini döndürür."""
    from pypsrp.client import Client

    errors = []
    # Önce HTTP (5985), olmazsa HTTPS (5986)
    for ssl_on, port in ((False, 5985), (True, 5986)):
        try:
            client = Client(host, username=user, password=pw, ssl=ssl_on, port=port,
                            cert_validation=False, auth='negotiate',
                            connection_timeout=15, operation_timeout=60, read_timeout=90)
            out, streams, _ = client.execute_ps(script)
        except Exception as ex:  # bağlantı/kimlik doğrulama hataları
            errors.append((port, _winrm_reason(ex)))
            continue
        if '@@END' not in out:
            errs = [str(e) for e in getattr(streams, 'error', [])]
            raise RuntimeError(errs[-1] if errs else 'beklenmeyen WinRM çıktısı')
        return parse_sections(out)
    net = all(m.startswith(('zaman aşımı', 'bağlantı reddedildi')) for _, m in errors)
    if len({m for _, m in errors}) == 1:  # iki port aynı sebepten düştüyse tek satır
        raise WinRMError(f'{errors[0][0]}/{errors[1][0]}: {errors[0][1]}', net)
    raise WinRMError(' | '.join(f'{pt}: {m}' for pt, m in errors) or 'bağlantı kurulamadı', net)


# WinRM yokken: 445 üzerinden ADSI (WinNT://) — PrivScope'un çalıştığı Windows'tan yerel PowerShell ile.
# Grup SID ile bulunur (Türkçe OS'ta da çalışır). Kimlik bilgisi komut satırına değil ortam değişkenine konur.
ADSI_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$h = $env:PS_HOST
if ($env:PS_USER) { $comp = New-Object System.DirectoryServices.DirectoryEntry("WinNT://$h,computer", $env:PS_USER, $env:PS_PASS) } else { $comp = New-Object System.DirectoryServices.DirectoryEntry("WinNT://$h,computer") }
$comp.RefreshCache()
$kids = @($comp.Children)
function Members($sid){
  $g = $kids | Where-Object { $_.SchemaClassName -eq 'Group' -and (New-Object System.Security.Principal.SecurityIdentifier($_.objectSid.Value,0)).Value -eq $sid } | Select-Object -First 1
  if(-not $g){ '(grup bulunamadi)'; return }
  foreach($m in @($g.Invoke('Members'))){
    $p=$m.GetType().InvokeMember('ADsPath','GetProperty',$null,$m,$null)
    $c=$m.GetType().InvokeMember('Class','GetProperty',$null,$m,$null)
    $n=($p -replace '^WinNT://','') -replace '/','\'
    if($n -match '(^|\\)S-1-5-'){"$n (Silinmis hesap)"}elseif($c -eq 'Group'){"$n [Grup]"}else{$n}
  }
}
"@@ADMIN"; Members 'S-1-5-32-544'
"@@RDP";   Members 'S-1-5-32-555'
"@@USERS"
foreach($u in ($kids | Where-Object { $_.SchemaClassName -eq 'User' })){
  if($u.UserFlags.Value -band 2){"$($u.Name) (Disabled)"}else{"$($u.Name)"}
}
"@@END"
"""


def _ps_local(script, host, user, pw):
    """Betiği yerel PowerShell'de çalıştırır, '@@' bölümlerini döndürür (şifre ortam değişkeninde)."""
    env = dict(os.environ, PS_HOST=host, PS_USER=user or '', PS_PASS=pw or '')
    # pwsh 7 / makine düzeyindeki PS7 yolları Windows PowerShell 5.1'de modül yüklemeyi bozar: kendi yollarını ver
    env['PSModulePath'] = os.pathsep.join([
        os.path.join(os.environ.get('ProgramFiles', r'C:\Program Files'), 'WindowsPowerShell', 'Modules'),
        os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32', 'WindowsPowerShell', 'v1.0', 'Modules')])
    fd, path = tempfile.mkstemp(suffix='.ps1')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8-sig') as f:  # betikte şifre yok
            f.write(script)
        r = subprocess.run(
            ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', path],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            timeout=120, env=env, creationflags=0x08000000)  # CREATE_NO_WINDOW
    except subprocess.TimeoutExpired:
        raise RuntimeError('zaman aşımı')
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    if '@@END' not in r.stdout:
        err = ' '.join(r.stderr.split())
        low = err.lower()
        if 'access is denied' in low or 'erişim engellendi' in low:
            raise RuntimeError('erişim reddedildi (hesap bu makinede yönetici değil ya da şifre geçersiz)')
        if 'network path' in low or 'rpc server' in low or 'ağ yolu' in low:
            raise RuntimeError('RPC yoluna ulaşılamadı')
        raise RuntimeError(err[:120] or 'beklenmeyen çıktı')
    return parse_sections(r.stdout)


def adsi_exec(host, user, pw):
    return _ps_local(ADSI_SCRIPT, host, user, pw)


# WinRM yokken: WMI/DCOM (135 + dinamik RPC). Grup üyelerini ve domain/workgroup bilgisini güvenilir verir.
WMI_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$opt = New-CimSessionOption -Protocol Dcom
if ($env:PS_USER) { $cred = New-Object System.Management.Automation.PSCredential($env:PS_USER, (ConvertTo-SecureString $env:PS_PASS -AsPlainText -Force)); $s = New-CimSession -ComputerName $env:PS_HOST -Credential $cred -SessionOption $opt } else { $s = New-CimSession -ComputerName $env:PS_HOST -SessionOption $opt }
function Members($sid){
  $g = Get-CimInstance -CimSession $s -ClassName Win32_Group -Filter "SID='$sid'"
  if(-not $g){ '(grup bulunamadi)'; return }
  foreach($m in @(Get-CimAssociatedInstance -CimSession $s -InputObject $g -Association Win32_GroupUser)){
    $n = "$($m.Domain)\$($m.Name)"
    $c = $m.CimClass.CimClassName
    if($c -eq 'Win32_Group'){"$n [Grup]"} elseif($c -eq 'Win32_UserAccount' -and $m.Disabled){"$n (Disabled)"} else {$n}
  }
}
"@@ADMIN"; Members 'S-1-5-32-544'
"@@RDP";   Members 'S-1-5-32-555'
"@@USERS"
foreach($u in @(Get-CimInstance -CimSession $s -ClassName Win32_UserAccount -Filter 'LocalAccount=True')){ if($u.Disabled){"$($u.Name) (Disabled)"}else{"$($u.Name)"} }
"@@SYS"
$cs = Get-CimInstance -CimSession $s -ClassName Win32_ComputerSystem
if($cs.PartOfDomain){"Domain: $($cs.Domain)"}else{"Workgroup (lokal): $($cs.Domain)"}
"@@END"
"""


def wmi_exec(host, user, pw):
    return _ps_local(WMI_SCRIPT, host, user, pw)


def _fix_member(line):
    """'DOMAIN\\HOST\\hesap' (yerel hesap, üç parça) -> 'HOST\\hesap'."""
    parts = line.split('\\')
    return f'{parts[1]}\\{parts[2]}' if len(parts) == 3 else line


def scan_windows(vm, user, pw, rep):
    try:
        sec = winrm_exec(vm['ip'], user, pw, WIN_SCRIPT)
    except WinRMError as ex:
        if not ex.unreachable:
            raise
        sec, errs = None, []
        for label, fn in (('WMI/DCOM', wmi_exec), ('ADSI/445', adsi_exec)):  # WinRM yok/kapalı
            try:
                sec = fn(vm['ip'], user, pw)
                break
            except Exception as ax:
                errs.append(f'{label}: {" ".join(str(ax).split())[:100]}')
        if sec is None:
            raise RuntimeError(f'{ex} || ' + ' | '.join(errs))
        if label == 'ADSI/445':  # ADSI bazı makinelerde grup üyesi vermez; Administrators asla boş olamaz
            sec['SYS'] = ['Domain: bilinmiyor (WinRM/WMI yok, ADSI/445 ile okundu)']
            if not sec.get('ADMIN'):
                sec['ADMIN'] = ['(okunamadi: uzak SAM üye listesi vermedi)']
    return {
        'Administrators': '\n'.join(_fix_member(x) for x in sec.get('ADMIN', [])),
        'Remote Desktop Users': '\n'.join(_fix_member(x) for x in sec.get('RDP', [])),
        'Local Users': '\n'.join(sec.get('USERS', [])),
        'Domain / Workgroup': '\n'.join(sec.get('SYS', [])),
    }


# Domain yetkili grupları: DC üzerinde çalışır (ActiveDirectory modülü gerekir).
# Gruplar SID ile bulunur (Türkçe DC'de de çalışır). İç içe gruplar açılır: alt üyeler '> ' ile başlar.
_AD_EXPAND = r"""
function Who($dn){
  $u = Get-ADUser -Identity $dn -Properties Enabled
  if($u.Enabled){ $u.SamAccountName } else { "$($u.SamAccountName) (Disabled)" }
}
function Expand($dn){
  foreach($n in @(Get-ADGroupMember -Identity $dn -Recursive)){
    if($n.objectClass -eq 'user'){ "> " + (Who $n.distinguishedName) } else { "> $($n.SamAccountName)" }
  }
}
function Dump($dn){
  foreach($m in @(Get-ADGroupMember -Identity $dn)){
    if($m.objectClass -eq 'user'){ Who $m.distinguishedName }
    elseif($m.objectClass -eq 'group'){ "$($m.SamAccountName) [Grup]"; try { Expand $m.distinguishedName } catch { "> (okunamadi)" } }
    else { $m.SamAccountName }
  }
}
"""
DOMAIN_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Import-Module ActiveDirectory
""" + _AD_EXPAND + r"""
$d = (Get-ADDomain).DomainSID.Value
$groups = [ordered]@{
  'Domain Admins'="$d-512"; 'Enterprise Admins'="$d-519"; 'Schema Admins'="$d-518"
  'Administrators (Builtin)'='S-1-5-32-544'; 'Account Operators'='S-1-5-32-548'
  'Server Operators'='S-1-5-32-549'; 'Print Operators'='S-1-5-32-550'
  'Backup Operators'='S-1-5-32-551'; 'Group Policy Creator Owners'="$d-520"
}
foreach($k in $groups.Keys){
  "@@G|$k"
  try {
    foreach($m in @(Get-ADGroupMember -Identity $groups[$k])){
      if($m.objectClass -eq 'user'){
        Who $m.distinguishedName
      } elseif($m.objectClass -eq 'group'){
        "$($m.SamAccountName) [Grup]"
        try { Expand $m.distinguishedName } catch { "> (okunamadi)" }
      } else {$m.SamAccountName}
    }
  } catch { "(okunamadi: $($_.Exception.Message))" }
}
# Ek gruplar (adı kullanıcı verir) ve otomatik keşif (adminCount=1, RID >= 1000 = özel gruplar).
# Ayarlar base64 JSON olarak gelir, koda enjekte edilmez.
$opt = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('%%OPT%%')) | ConvertFrom-Json
$done = @{}
foreach($name in @($opt.extra)){
  "@@G|$name"
  try { $g = Get-ADGroup -Identity $name -ErrorAction Stop; $done[$g.SID.Value] = 1; Dump $g.DistinguishedName } catch { "(not found)" }
}
if($opt.auto){
  foreach($g in @(Get-ADGroup -Filter 'adminCount -eq 1' -Properties adminCount)){
    if(([int]($g.SID.Value.Split('-')[-1])) -ge 1000 -and -not $done.ContainsKey($g.SID.Value)){
      "@@G|$($g.SamAccountName) (auto)"
      try { Dump $g.DistinguishedName } catch { "(okunamadi: $($_.Exception.Message))" }
    }
  }
}
"@@END"
"""

# Makinelerin yerel grubunda geçen domain gruplarını DC'de çözer (adlar base64 JSON, koda enjekte edilmez).
EXPAND_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Import-Module ActiveDirectory
""" + _AD_EXPAND + r"""
$names = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('%%B64%%')) | ConvertFrom-Json
foreach($n in @($names)){
  "@@G|$n"
  try { $g = Get-ADGroup -Identity $n; Expand $g.DistinguishedName } catch { "(not found)" }
}
"@@END"
"""


def scan_domain(dc, user, pw, extra=(), auto=False):
    """[(grup adı, [üyeler])] döndürür. extra: ek grup adları; auto: adminCount=1 özel grupları bul."""
    import base64
    opt = base64.b64encode(json.dumps({'extra': list(extra), 'auto': bool(auto)}).encode('utf-8')).decode()
    sec = winrm_exec(dc, user, pw, DOMAIN_SCRIPT.replace('%%OPT%%', opt))
    return [(k[2:], v) for k, v in sec.items() if k.startswith('G|')]


_GROUP_LINE = re.compile(r'^(?:.+\\)?(.+?) \[Grup\]$')


def nested_group_names(rows):
    """Windows satırlarındaki 'X\\Grup [Grup]' üyelerinin grup adları."""
    names = set()
    for r in rows:
        for col in ('Administrators', 'Remote Desktop Users'):
            for line in str(r.get(col, '')).split('\n'):
                m = _GROUP_LINE.match(line)
                if m:
                    names.add(m.group(1))
    return names


def expand_domain_groups(dc, user, pw, names):
    """{grup adı: ['> üye', ...]} — domain'de bulunan gruplar için."""
    import base64
    b64 = base64.b64encode(json.dumps(sorted(names)).encode('utf-8')).decode()
    sec = winrm_exec(dc, user, pw, EXPAND_SCRIPT.replace('%%B64%%', b64))
    return {k[2:]: [x for x in v if x.startswith('>')] for k, v in sec.items() if k.startswith('G|')}


def inject_nested(text, expansions):
    """Metindeki '... [Grup]' satırlarının altına çözülmüş üyeleri ekler."""
    out = []
    for line in str(text).split('\n'):
        out.append(line)
        m = _GROUP_LINE.match(line)
        if m and m.group(1) in expansions:
            out.extend(expansions[m.group(1)])
    return '\n'.join(out)


# --------------------------------------------------------------------------
# Linux (SSH / paramiko)
# --------------------------------------------------------------------------
LINUX_SCRIPT = r"""
echo "@@UID0"; awk -F: '$3==0{print $1}' /etc/passwd
echo "@@GRP"
for g in wheel sudo admin; do
  m=$(getent group "$g" | cut -d: -f4)
  [ -n "$m" ] && echo "$g: $m"
done
echo "@@SUDOERS"
grep -hEv '^[[:space:]]*(#|$)' /etc/sudoers /etc/sudoers.d/* 2>/dev/null | grep -Ev '^[[:space:]]*(Defaults|@include)'
echo "@@TPW"
grep -hE '^[[:space:]]*Defaults.*targetpw' /etc/sudoers /etc/sudoers.d/* 2>/dev/null
echo "@@USERS"
awk -F: 'NR==FNR{s[$1]=$2;next} $7!~/(nologin|false|sync|shutdown|halt)$/{print $1 ((s[$1]~/^[!*]/)?" (Locked)":"")}' /etc/shadow /etc/passwd
echo "@@END"
"""

_ALIAS_RE = re.compile(r'^(Cmnd|User|Host|Runas)_Alias\b')
_RULE_RE = re.compile(r'^(\S+)\s+[^=\s]+\s*=\s*(?:\([^)]*\)\s*)?(.*)$')
_TAG_RE = re.compile(r'^(?:(?:NO)?(?:PASSWD|EXEC|SETENV)|LOG_INPUT|LOG_OUTPUT|MAIL|NOMAIL)\s*:\s*', re.I)


def clean_sudoers(lines, targetpw_lines=()):
    """Alias'ları atar, aynı kullanıcının çok sayıdaki kısıtlı komut kuralını tek satıra indirir."""
    order = OrderedDict()  # kullanıcı -> [(ham satır, komut sayısı, nopasswd, kısıtlı_mı)]
    for line in lines:
        if _ALIAS_RE.match(line):
            continue
        m = _RULE_RE.match(line)
        if not m:
            order.setdefault(None, []).append((line, 0, False, False))
            continue
        who, cmds = m.group(1), m.group(2).strip()
        nopw = False
        while True:
            t = _TAG_RE.match(cmds)
            if not t:
                break
            nopw = nopw or t.group(0).upper().startswith('NOPASSWD')
            cmds = cmds[t.end():]
        restricted = cmds.strip() != 'ALL'
        order.setdefault(who, []).append((line, len(cmds.split(',')), nopw, restricted))

    out = []
    for who, rules in order.items():
        restricted = [r for r in rules if r[3]]
        total = sum(r[1] for r in restricted)
        if who is not None and total > SUDO_COLLAPSE_AFTER:
            out.extend(r[0] for r in rules if not r[3])
            tag = ' (NOPASSWD)' if any(r[2] for r in restricted) else ''
            out.append(f'{who}: {total} kısıtlı komut{tag}')
        else:
            out.extend(r[0] for r in rules)
    out = list(OrderedDict.fromkeys(out))  # tekrarları at, sırayı koru
    if targetpw_lines:
        out.append('[!] Defaults targetpw (sudo, hedef kullanıcının şifresini ister)')
    return out


def scan_linux(vm, user, pw, rep):
    import paramiko

    is_root = user == 'root'
    cmd = 'bash -s' if is_root else "sudo -S -p '' bash -s"
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(vm['ip'], username=user, password=pw, timeout=10, auth_timeout=15,
                       look_for_keys=False, allow_agent=False)
        stdin, stdout, stderr = client.exec_command(cmd, timeout=90)
        if not is_root:
            stdin.write(pw + '\n')
        stdin.write(LINUX_SCRIPT)
        stdin.channel.shutdown_write()
        out = stdout.read().decode('utf-8', 'replace')
        err = stderr.read().decode('utf-8', 'replace')
    except paramiko.AuthenticationException:
        raise RuntimeError('şifre yanlış')
    finally:
        client.close()

    if '@@END' not in out:
        if 'not in the sudoers' in err or 'incorrect password' in err:
            raise RuntimeError('sudo yetkisi yok / sudo şifresi hatalı')
        raise RuntimeError((err.strip().splitlines() or ['bilinmeyen hata'])[-1])

    sec = parse_sections(out)
    return {
        'Root Yetkili (UID 0)': '\n'.join(sec.get('UID0', [])),
        'wheel / sudo Grup Üyeleri': '\n'.join(sec.get('GRP', [])),
        'Sudoers Kuralları': '\n'.join(clean_sudoers(sec.get('SUDOERS', []), sec.get('TPW', []))),
        'Local Users': '\n'.join(sec.get('USERS', [])),
    }


# --------------------------------------------------------------------------
# HTML rapor (tek dosya; sidebar + rozet + arama; taban dil İngilizce, TR sözlükle)
# --------------------------------------------------------------------------
HDR_EN = {'VM Adı': 'VM Name', 'Root Yetkili (UID 0)': 'Root Accounts (UID 0)',
          'wheel / sudo Grup Üyeleri': 'wheel / sudo Group Members',
          'Sudoers Kuralları': 'Sudoers Rules'}
TR = {  # İngilizce metin -> Türkçe
    'Overview': 'Özet', 'Domain Privileges': 'Domain Yetkileri', 'Windows Servers': 'Windows Sunucular',
    'Linux Servers': 'Linux Sunucular', 'Unreachable': 'Erişilemeyenler',
    'Privileged account inventory': 'Yetkili hesap envanteri',
    'VM Name': 'VM Adı', 'Local Users': 'Lokal Kullanıcılar',
    'Root Accounts (UID 0)': 'Root Yetkili (UID 0)', 'wheel / sudo Group Members': 'wheel / sudo Grup Üyeleri',
    'Sudoers Rules': 'Sudoers Kuralları', 'Group': 'Grup', 'Members': 'Üyeler', 'Count': 'Adet',
    'Platform': 'Platform', 'Reason': 'Sebep', 'No data': 'Veri yok', 'No members': 'Üye yok',
    'Generated': 'Oluşturulma', 'Domain / Workgroup': 'Domain / Çalışma grubu',
    'servers scanned': 'sunucu tarandı', 'Servers scanned': 'Taranan sunucu', 'reachable': 'erişilebilir',
    'Domain privileged accounts': 'Domain yetkili hesaplar', 'Privileged local accounts': 'Yetkili lokal hesaplar',
    'Findings': 'Bulgular', 'Finding': 'Bulgu', 'Servers': 'Sunucular', 'No findings': 'Bulgu yok',
    'Nested groups are expanded': 'İç içe gruplar açıldı', 'Disabled': 'Devre dışı', 'Locked': 'Kilitli',
    'Orphan SID': 'Silinmiş hesap', 'Search…': 'Ara…', 'Domain controller / no local users': 'Domain controller / lokal kullanıcı yok',
    'Orphan SIDs in admin groups': 'Yönetici gruplarında silinmiş hesap (orphan SID)',
    'Disabled accounts in admin groups': 'Yönetici gruplarında devre dışı hesap',
    'Non-root UID 0 accounts': 'root dışı UID 0 hesabı',
    'Unrestricted sudo (NOPASSWD: ALL)': 'Kısıtsız sudo (NOPASSWD: ALL)',
    'sudo asks for target user password (targetpw)': 'sudo hedef kullanıcı şifresi istiyor (targetpw)',
    'Domain Admins members': 'Domain Admins üyeleri', 'Privileged Accounts': 'Yetkili Hesaplar', 'Account': 'Hesap', 'Groups': 'Gruplar', 'Status': 'Durum', 'Active': 'Aktif', 'groups': 'grup', 'Unknown': 'Bilinmiyor',
}

_CSS = """
:root{--bg:#f4f2ee;--card:#fff;--edge:#e0dcd3;--ink:#1c1b19;--mute:#6b665c;--teal:#0f766e;--teal-bg:#e6f4f1;
--teal-ink:#0b5d57;--dark:#12332f;--red:#a1291f;--red-bg:#fbeae8;--amb:#8a4b08;--amb-bg:#fdf1dc;--off-bg:#eceae4}
*{box-sizing:border-box}
body{margin:0;font:13px/1.45 'IBM Plex Sans','Segoe UI',system-ui,sans-serif;color:var(--ink);background:var(--bg)}
.layout{display:flex;min-height:100vh}
.side{width:248px;flex-shrink:0;background:var(--dark);color:#f4f2ee;padding:22px 14px;display:flex;
  flex-direction:column;gap:6px;position:sticky;top:0;height:100vh}
.brand{padding:0 8px 18px}.brand b{display:block;font-size:17px}.brand span{font-size:11px;color:#8fb3ad}
.main-btn{display:flex;align-items:center;justify-content:space-between;gap:8px;width:100%;padding:10px 12px;
  border:0;border-radius:8px;background:transparent;color:#cfe3df;font:inherit;font-size:13px;cursor:pointer;text-align:left}
.main-btn:hover{background:#1c4b45}
.main-btn.active-sidebar{background:#1c4b45;color:#fff;font-weight:600}
.main-btn-badge{min-width:22px;text-align:center;padding:2px 7px;border-radius:10px;background:#2a5c55;
  color:#fff;font-size:11px;font-weight:600}
.main-btn-badge-critical{background:#c2362e}
.lang{margin-top:auto;padding:8px;border:1px solid #2a5c55;border-radius:8px;background:transparent;
  color:#cfe3df;font:inherit;font-weight:600;cursor:pointer}
.lang:hover{background:#1c4b45}
.main{flex:1;min-width:0;padding:26px 32px}
.top{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;margin-bottom:18px;flex-wrap:wrap}
.top h1{margin:0;font-size:24px}.meta{color:var(--mute);margin-top:2px}
#q{width:280px;max-width:100%;padding:9px 12px;border:1px solid #cfcabe;border-radius:8px;background:#fff;font:inherit}
.container{display:none}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px;margin-bottom:22px}
.kpi{background:var(--card);border:1px solid var(--edge);border-radius:12px;padding:16px 18px}
.kpi small{color:var(--mute);font-size:12px}.kpi b{display:block;font-size:30px;margin:4px 0 2px}
.kpi span{font-size:12px;color:var(--mute)}
.kpi.bad{background:var(--red-bg);border-color:#f0c9c4}.kpi.bad small,.kpi.bad b,.kpi.bad span{color:var(--red)}
h2{font-size:16px;margin:22px 0 10px}
.tw{overflow:auto;background:var(--card);border:1px solid var(--edge);border-radius:12px}
table{border-collapse:collapse;width:100%}
th{position:sticky;top:0;background:var(--dark);color:#f4f2ee;text-align:left;padding:11px 16px;
  font-size:12px;letter-spacing:.3px;font-weight:600;white-space:nowrap}
td{padding:11px 16px;border-top:1px solid #ece9e2;vertical-align:top;min-width:120px}
tr:hover td{background:#faf9f6}
tr.err td{background:#f7f6f2;color:var(--mute)}
.vm{font-weight:600}.mono{font-family:'IBM Plex Mono',Consolas,monospace;font-size:12px}
.ln{padding:2px 0;word-break:break-word}.ln.sub{padding-left:18px;color:#57534b}
.br{color:#a8a397;margin-right:6px}.none,.note{color:#8a857a}.note{font-style:italic}.warn{color:var(--amb)}
.pill{display:inline-block;margin-left:8px;padding:1px 8px;border-radius:8px;font-size:11px;font-weight:600;
  vertical-align:middle;background:var(--off-bg);color:#57534b}
.pill.grp{background:var(--teal-bg);color:var(--teal-ink)}.pill.bad{background:var(--red-bg);color:var(--red)}
.pill.amb{background:var(--amb-bg);color:var(--amb)}
.pill.first{margin-left:0;margin-right:8px}
.groups{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px;align-items:start}
.gcard{background:var(--card);border:1px solid var(--edge);border-radius:12px;overflow:hidden}
.gh{display:flex;justify-content:space-between;align-items:center;padding:12px 16px;background:#f8f7f3;
  border-bottom:1px solid var(--edge);font-size:14px;font-weight:600}
.cnt{padding:2px 9px;border-radius:10px;background:var(--teal-bg);color:var(--teal-ink);font-size:12px}
.cnt.zero{background:var(--off-bg);color:#57534b}
.gb{padding:8px 16px}
.gcard.err .gb{color:var(--red)}
@media (max-width:820px){.layout{flex-direction:column}.side{width:auto;height:auto;position:static;
  flex-direction:row;flex-wrap:wrap}.lang{margin-top:0}.main{padding:18px}}
@media print{.side,#q{display:none}.container{display:block!important;page-break-after:always}}
"""

_JS = """
var TR=%%TR%%;var lang='en';
function tx(k){return(lang==='tr'&&TR[k])?TR[k]:k;}
function show(id,btn){
  document.querySelectorAll('.container').forEach(function(c){c.style.display='none'});
  document.getElementById(id).style.display='block';
  document.querySelectorAll('.main-btn').forEach(function(b){b.classList.toggle('active-sidebar',b===btn)});
  var t=document.getElementById('pageTitle');t.setAttribute('data-i18n',btn.dataset.label);t.textContent=tx(btn.dataset.label);
  var q=document.getElementById('q');q.value='';filter('');
}
function filter(v){
  v=v.toLowerCase();
  document.querySelectorAll('.container').forEach(function(c){
    if(c.style.display==='none')return;
    c.querySelectorAll('tbody tr,.gcard').forEach(function(r){r.style.display=(!v||r.textContent.toLowerCase().indexOf(v)>=0)?'':'none'});
  });
}
document.querySelectorAll('.main-btn').forEach(function(b){b.addEventListener('click',function(){show(b.dataset.target,b)})});
document.getElementById('q').addEventListener('input',function(e){filter(e.target.value)});
document.querySelectorAll('.main-btn-badge').forEach(function(b){
  if(b.textContent.trim()==='0'||b.textContent.trim()==='-')b.style.display='none';});
function setLang(l){lang=l;document.documentElement.lang=l;
  document.querySelectorAll('[data-i18n]').forEach(function(e){e.textContent=tx(e.getAttribute('data-i18n'))});
  document.getElementById('q').placeholder=tx('Search…');
  document.getElementById('langBtn').textContent=(l==='tr')?'EN':'TR';}
document.getElementById('langBtn').addEventListener('click',function(){setLang(lang==='tr'?'en':'tr')});
document.getElementById('q').placeholder=tx('Search…');
var first=document.querySelector('.main-btn');show(first.dataset.target,first);
"""

_TAGS = ((' [Grup]', 'Group', 'grp'), (' (Disabled)', 'Disabled', ''), (' (Locked)', 'Locked', ''),
         (' (Silinmis hesap)', 'Orphan SID', 'bad'))


def _i18n(text, tag='span', cls=''):
    c = f" class='{cls}'" if cls else ''
    return f"<{tag}{c} data-i18n=\"{html.escape(text)}\">{html.escape(text)}</{tag}>"


def _pill(label, cls='', first=False):
    return (f"<span class='pill {cls}{' first' if first else ''}' data-i18n=\"{html.escape(label)}\">"
            f"{html.escape(label)}</span>")


def _line(line, mono=False):
    """Bir metin satırı -> HTML (iç içe üyeler '> ' ile başlar; etiketler rozete çevrilir)."""
    sub = line.startswith('>')
    if sub:
        line = line[1:].strip()
    pills = ''
    for tag, label, cls in _TAGS:
        if line.endswith(tag):
            line, pills = line[:-len(tag)], _pill(label, cls)
            break
    cls = 'ln' + (' sub' if sub else '') + (' mono' if mono else '')
    if line.startswith(('(okunamadi', '(not found', '(grup bulunamadi')):
        cls += ' note'
    elif line.startswith('[!]'):
        cls += ' warn'
    prefix = "<span class='br'>└</span>" if sub else ''
    return f"<div class='{cls}'>{prefix}{html.escape(line)}{pills}</div>"


def _cell(text, mono=False):
    lines = [x for x in str(text or '').split('\n') if x.strip()]
    return ''.join(_line(x, mono) for x in lines) or "<span class='none'>—</span>"


def _domain_cell(text):
    t = str(text or '')
    if t.startswith('Domain: bilinmiyor'):
        return _pill('Unknown', '', True) + html.escape(t.split(':', 1)[1].strip())
    if t.startswith('Domain:'):
        return f"<span class='pill grp first'>{html.escape(t.split(':', 1)[1].strip())}</span>"
    if t.startswith('Workgroup'):
        return f"<span class='pill amb first'>{html.escape(t)}</span>"
    return "<span class='none'>—</span>"


def _is_account(line):
    return bool(line.strip()) and not line.endswith(' [Grup]') and not line.startswith(('(', '[!]'))


def _account_name(line):
    line = line.lstrip('> ').strip()
    for tag, _, _ in _TAGS[1:]:
        if line.endswith(tag):
            line = line[:-len(tag)]
    return line


def _table(cols, rows):
    head = ''.join(_i18n(HDR_EN.get(c, c), 'th') for c in cols)
    body = []
    for r in sorted(rows, key=lambda x: x['VM Adı'].lower()):
        name = f"<td class='vm'>{html.escape(r['VM Adı'])}</td>"
        ip = f"<td class='mono'>{html.escape(r.get('IP', ''))}</td>"
        if r.get('_error'):
            msg = html.escape(str(r.get(cols[2], '')))
            body.append(f"<tr class='err'>{name}{ip}<td colspan='{len(cols) - 2}'>"
                        f"{_pill('Unreachable', 'bad', True)}{msg}</td></tr>")
            continue
        cells = ''.join(
            f"<td>{_domain_cell(r.get(c))}</td>" if c == 'Domain / Workgroup'
            else f"<td>{_cell(r.get(c, ''), mono=c == 'Sudoers Kuralları')}</td>" for c in cols[2:])
        body.append(f"<tr>{name}{ip}{cells}</tr>")
    return f"<div class='tw'><table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table></div>"


def _domain_accounts(domain):
    """Hesap bazlı görünüm: {hesap: {'groups': [...], 'disabled': bool}} — iç içe üyelik 'via Grup' ile belirtilir."""
    acc = OrderedDict()
    for g, members in domain:
        via = None
        for m in members:
            if m.endswith(' [Grup]'):
                via = m[:-len(' [Grup]')]
                continue
            if not _is_account(m):
                continue
            nested = m.startswith('>')
            if not nested:
                via = None
            e = acc.setdefault(_account_name(m), {'groups': [], 'disabled': False})
            e['disabled'] = e['disabled'] or m.endswith(' (Disabled)')
            label = f'{g} (via {via})' if nested and via else g
            if label not in e['groups']:
                e['groups'].append(label)
    return acc


def _accounts_table(acc):
    head = ''.join(_i18n(h, 'th') for h in ('Account', 'Groups', 'Status'))
    rows = []
    for name, e in sorted(acc.items(), key=lambda kv: (-len(kv[1]['groups']), kv[0].lower())):
        pills = ''.join(_pill(g, 'grp', True) if False else
                        f"<span class='pill grp first'>{html.escape(g)}</span>" for g in e['groups'])
        status = _pill('Disabled', '', True) if e['disabled'] else _i18n('Active')
        rows.append(f"<tr><td class='vm'>{html.escape(name)}</td><td>{pills}</td><td>{status}</td></tr>")
    return f"<div class='tw'><table><thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"


def _findings(win_rows, lin_rows, domain):
    """(başlık, adet, etkilenen sunucular) — yalnızca adet > 0 olanlar."""
    def hit(rows, pred):
        return sorted({r['VM Adı'] for r in rows if not r.get('_error') and pred(r)})
    win_lines = lambda r: str(r.get('Administrators', '')).split('\n') + str(r.get('Remote Desktop Users', '')).split('\n')
    out = [
        ('Orphan SIDs in admin groups', hit(win_rows, lambda r: any(x.endswith('(Silinmis hesap)') for x in win_lines(r)))),
        ('Disabled accounts in admin groups', hit(win_rows, lambda r: any(
            x.endswith('(Disabled)') for x in str(r.get('Administrators', '')).split('\n')))),
        ('Non-root UID 0 accounts', hit(lin_rows, lambda r: any(
            x.strip() and x.strip() != 'root' for x in str(r.get('Root Yetkili (UID 0)', '')).split('\n')))),
        ('Unrestricted sudo (NOPASSWD: ALL)', hit(lin_rows, lambda r: bool(re.search(
            r'NOPASSWD:\s*ALL\b', str(r.get('Sudoers Kuralları', ''))) ))),
        ('sudo asks for target user password (targetpw)', hit(lin_rows, lambda r: 'Defaults targetpw' in str(
            r.get('Sudoers Kuralları', '')))),
    ]
    res = [(t, len(s), s) for t, s in out if s]
    if isinstance(domain, list):
        for g, ms in domain:
            if g == 'Domain Admins':
                n = len({_account_name(m) for m in ms if _is_account(m)})
                res.append(('Domain Admins members', n, []))
    return res


def write_html(path, win_rows, lin_rows, domain):
    """domain: None (istenmedi) | str (hata) | [(grup, [üyeler])]."""
    ok_win = [r for r in win_rows if not r.get('_error')]
    ok_lin = [r for r in lin_rows if not r.get('_error')]
    fails = [(r, 'Windows', WIN_COLS[2]) for r in win_rows if r.get('_error')] + \
            [(r, 'Linux', LIN_COLS[2]) for r in lin_rows if r.get('_error')]
    total = len(win_rows) + len(lin_rows)

    # Domain
    dom_accounts, dom_html = 0, ''
    if isinstance(domain, list):
        seen = set()
        cards = []
        for g, ms in domain:
            names = {_account_name(m) for m in ms if _is_account(m)}
            seen |= names
            body = ''.join(_line(m) for m in ms) or f"<span class='none'>{_i18n('No members')}</span>"
            cards.append(f"<div class='gcard'><div class='gh'><span>{html.escape(g)}</span>"
                         f"<span class='cnt{'' if names else ' zero'}'>{len(names)}</span></div>"
                         f"<div class='gb'>{body}</div></div>")
        dom_accounts = len(seen)
        dom_html = f"<div class='groups'>{''.join(cards)}</div>"
    elif isinstance(domain, str):
        dom_html = (f"<div class='groups'><div class='gcard err'><div class='gb'>"
                    f"{html.escape(domain)}</div></div></div>")

    # Yetkili lokal hesap sayısı (sunucu, hesap çiftleri)
    pairs = sum(1 for r in ok_win for x in str(r.get('Administrators', '')).split('\n') if _is_account(x)
                and not x.startswith('>')) + \
        sum(1 for r in ok_lin for c in ('Root Yetkili (UID 0)',) for x in str(r.get(c, '')).split('\n') if x.strip())

    # Özet
    finds = _findings(win_rows, lin_rows, domain)
    frows = ''.join(
        f"<tr><td>{_i18n(t)}</td><td><b>{n}</b></td><td>{html.escape(', '.join(s[:8]) + (' …' if len(s) > 8 else ''))}</td></tr>"
        for t, n, s in finds)
    ftable = (f"<div class='tw'><table><thead><tr>{_i18n('Finding', 'th')}{_i18n('Count', 'th')}"
              f"{_i18n('Servers', 'th')}</tr></thead><tbody>{frows}</tbody></table></div>") if finds \
        else f"<div class='tw'><div class='gb'><span class='none'>{_i18n('No findings')}</span></div></div>"
    kpis = (
        f"<div class='kpis'>"
        f"<div class='kpi'><small>{_i18n('Servers scanned')}</small><b>{total}</b>"
        f"<span>{len(ok_win) + len(ok_lin)} {_i18n('reachable')}</span></div>"
        + (f"<div class='kpi'><small>{_i18n('Domain privileged accounts')}</small><b>{dom_accounts}</b>"
           f"<span>{len(domain)} {_i18n('groups')}</span></div>" if isinstance(domain, list) else '')
        + f"<div class='kpi'><small>{_i18n('Privileged local accounts')}</small><b>{pairs}</b>"
          f"<span>{len(ok_win) + len(ok_lin)} {_i18n('Servers')}</span></div>"
        + (f"<div class='kpi bad'><small>{_i18n('Unreachable')}</small><b>{len(fails)}</b><span>&nbsp;</span></div>"
           if fails else '') + "</div>")
    overview = f"{kpis}<h2>{_i18n('Findings')}</h2>{ftable}"

    sections = [('sec-over', 'Overview', '-', overview)]
    if dom_html:
        sections.append(('sec-domain', 'Domain Privileges', dom_accounts if isinstance(domain, list) else '-', dom_html))
    if isinstance(domain, list) and dom_accounts:
        sections.append(('sec-acct', 'Privileged Accounts', dom_accounts, _accounts_table(_domain_accounts(domain))))
    sections.append(('sec-win', 'Windows Servers', len(win_rows), _table(WIN_COLS, win_rows)))
    sections.append(('sec-lin', 'Linux Servers', len(lin_rows), _table(LIN_COLS, lin_rows)))
    if fails:
        rows = ''.join(
            f"<tr class='err'><td class='vm'>{html.escape(r['VM Adı'])}</td><td class='mono'>{html.escape(r['IP'])}</td>"
            f"<td>{p}</td><td>{html.escape(str(r.get(c, '')))}</td></tr>" for r, p, c in fails)
        sections.append(('sec-fail', 'Unreachable', len(fails),
                         f"<div class='tw'><table><thead><tr>{_i18n('VM Name', 'th')}<th>IP</th>{_i18n('Platform', 'th')}"
                         f"{_i18n('Reason', 'th')}</tr></thead><tbody>{rows}</tbody></table></div>"))

    nav = ''.join(
        f"<button class='main-btn' data-target='{sid}' data-label=\"{html.escape(label)}\">{_i18n(label)}"
        + (f"<span class='main-btn-badge{' main-btn-badge-critical' if sid == 'sec-fail' else ''}'>{n}</span>"
           if n != '-' else '') + "</button>"
        for sid, label, n, _ in sections)
    content = ''.join(f"<div class='container' id='{sid}'>{body}</div>" for sid, _, _, body in sections)
    tr_json = json.dumps(TR, ensure_ascii=False).replace('</', '<\\/')
    page = (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{APP_TITLE}</title>"
        "<link rel='preconnect' href='https://fonts.googleapis.com'>"
        "<link href='https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&"
        "family=IBM+Plex+Mono:wght@400;500&display=swap' rel='stylesheet'>"
        f"<style>{_CSS}</style></head><body><div class='layout'>"
        f"<nav class='side'><div class='brand'><b>PrivScope</b>{_i18n('Privileged account inventory')}</div>"
        f"{nav}<button class='lang' id='langBtn'>TR</button></nav>"
        f"<main class='main'><div class='top'><div><h1 id='pageTitle' data-i18n='Overview'>Overview</h1>"
        f"<div class='meta'>{_i18n('Generated')} {datetime.now():%Y-%m-%d %H:%M} · {total} "
        f"{_i18n('servers scanned')}</div></div><input id='q' type='search' aria-label='Search'></div>"
        f"{content}</main></div><script>"
        + _JS.replace('%%TR%%', tr_json) + "</script></body></html>")
    with open(path, 'w', encoding='utf-8') as f:
        f.write(page)


# --------------------------------------------------------------------------
# Tarama orkestrasyonu (worker thread)
# --------------------------------------------------------------------------
def file_targets(path, rep):
    """Dosyadaki hedefler; appliance/altyapı VM'leri elenir. kind None ise tarama sırasında belirlenir."""
    out = []
    for t in load_targets(path):
        if EXCLUDE_RE.search(t['name']):
            rep.log(f'Kapsam dışı (appliance/altyapı): {t["name"]}')
        else:
            out.append(t)
    return out


def vcenter_targets(host, user, pw, rep):
    """vCenter'daki açık VM'lerden hedef listesi."""
    rep.log('vCenter\'a bağlanılıyor...')
    out = []
    for v in fetch_vms(host, user, pw, rep):
        name = str(v.get('name', ''))
        if EXCLUDE_RE.search(name):
            rep.log(f'Kapsam dışı (appliance/altyapı): {name}')
            continue
        kind = classify_os(v)
        if not kind:
            rep.log(f'Atlandı (OS tanınmadı): {name}')
            continue
        out.append({'name': name, 'kind': kind, 'ip': first_ipv4(v), 'user': '', 'pw': '',
                    'dns': str(v.get('guest.hostName') or '')})
    return out


def run_scan(cfg, rep, out_path):
    try:
        if cfg.get('targets') is not None:  # uygulamada düzenlenmiş liste
            targets = [dict(t) for t in cfg['targets']]
        elif cfg['source'] == 'file':
            targets = file_targets(cfg['ip_file'], rep)
        else:
            targets = vcenter_targets(cfg['vc_host'], cfg['vc_user'], cfg['vc_pw'], rep)
        rep.add_secrets(t.get('pw') for t in targets)
        own = sum(bool(t.get('user') and t.get('pw')) for t in targets)
        rep.log(f'{len(targets)} hedef ({own} tanesinde kendi cred\'i var).')

        rep.log(f'Taranacak: {len(targets)} sunucu.')
        total = len(targets)
        rep.progress(0, total)

        creds = {'windows': (cfg['win_user'], cfg['win_pw']),
                 'linux': (cfg['lin_user'], cfg['lin_pw'])}

        def work(t):
            row = {'VM Adı': t['name'], 'IP': t['ip']}

            def fail(msg):
                cols = WIN_COLS if t['kind'] == 'windows' else LIN_COLS
                row[cols[2]] = msg
                row['_error'] = True
                return t, row

            if rep.cancel.is_set():
                return fail('Erişim sağlanamadı: iptal edildi')
            if not t['ip']:
                return fail('Erişim sağlanamadı: IP alınamadı (VMware Tools?)')
            if not t['kind']:
                t['kind'] = detect_kind(t['ip'])
                if not t['kind']:  # OS belirlenemedi -> Linux sekmesinde raporlanır
                    t['kind'] = 'linux'
                    return fail('OS belirlenemedi: dosyada OS bilgisi yok, '
                                '5985/5986/22 portları da kapalı')
            # Excel'deki satır cred'i (lokal Windows / Linux) GUI cred'inden önceliklidir
            acct = t.get('acct') if t['kind'] == 'windows' else None
            if acct == 'domain':  # GUI'deki domain admin
                user, pw = creds['windows']
            elif acct == 'local':  # yalnızca satırdaki lokal hesap
                user, pw = t.get('user') or '', t.get('pw') or ''
                if not (user and pw):
                    return fail('Erişim: lokal hesap için kullanıcı/şifre girilmemiş')
            elif t.get('user') and t.get('pw'):
                user, pw = t['user'], t['pw']
            else:
                user, pw = creds[t['kind']]
            if not (user and pw):
                return fail('Erişim: cred yok')
            try:
                scan = scan_windows if t['kind'] == 'windows' else scan_linux
                row.update(scan(t, user, pw, rep))
            except Exception as ex:
                return fail(f'Erişim sağlanamadı: {rep.reason(ex)}')
            return t, row

        win_rows, lin_rows, done = [], [], 0
        with ThreadPoolExecutor(max_workers=cfg['workers']) as pool:
            futures = [pool.submit(work, t) for t in targets]
            for fut in as_completed(futures):
                t, row = fut.result()
                (win_rows if t['kind'] == 'windows' else lin_rows).append(row)
                done += 1
                state = row[(WIN_COLS if t['kind'] == 'windows' else LIN_COLS)[2]] \
                    if row.get('_error') else 'OK'
                rep.log(f'[{done}/{total}] {t["name"]} ({t["ip"] or "-"}): '
                        f'{state if state == "OK" else state}')
                rep.progress(done, total)
                rep.q.put(('stats', len(win_rows), len(lin_rows),
                           sum(bool(r.get('_error')) for r in win_rows + lin_rows)))

        if rep.cancel.is_set():
            rep.log('İptal edildi; rapor yazılmadı.')
            rep.q.put(('done', None, 'İptal edildi.'))
            return

        domain = None  # None = istenmedi
        if cfg['domain']:
            rep.log(f'Domain yetkili grupları okunuyor (DC: {cfg["dc_host"]})...')
            try:
                domain = scan_domain(cfg['dc_host'], cfg['win_user'], cfg['win_pw'],
                                     cfg.get('extra_list', ()), cfg.get('auto_groups', False))
            except Exception as ex:
                domain = f'Erişim sağlanamadı: {rep.reason(ex)}'
                rep.log(domain)

        if cfg['domain'] and win_rows:  # yerel Administrators içindeki domain gruplarının üyelerini aç
            names = nested_group_names(win_rows)
            if names:
                try:
                    exp = expand_domain_groups(cfg['dc_host'], cfg['win_user'], cfg['win_pw'], names)
                    for r in win_rows:
                        for col in ('Administrators', 'Remote Desktop Users'):
                            if r.get(col):
                                r[col] = inject_nested(r[col], exp)
                except Exception as ex:
                    rep.log(f'Domain gruplarının üyeleri açılamadı: {rep.reason(ex)}')

        write_html(out_path, win_rows, lin_rows, domain)
        rep.log(f'Rapor kaydedildi: {out_path}')
        rep.q.put(('done', out_path, None))
    except ImportError as ex:
        rep.q.put(('done', None, f'Eksik modül: {ex.name}\n'
                                 'pip install pyvmomi paramiko openpyxl pypsrp'))
    except PermissionError:
        rep.q.put(('done', None, 'Rapor dosyası yazılamadı (başka bir programda açık olabilir).'))
    except Exception as ex:
        rep.q.put(('done', None, rep.reason(ex)))


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
BG, CARD, EDGE = '#f4f2ee', '#ffffff', '#e0dcd3'
TEAL, TEAL_DK, INK, MUTED, DARK = '#0f766e', '#0b5d57', '#1c1b19', '#6b665c', '#12332f'
FONT = 'Segoe UI'


SELECT_CHOICES = ('Tümü', 'Linux', 'Windows', 'Windows · Domain', 'Windows · Lokal',
                  "Cred'siz olanlar", 'Seçimi kaldır')
ACCESS_CHOICES = ('otomatik', 'Windows · otomatik', 'Windows · Domain', 'Windows · Lokal', 'Linux')
_ACCESS = {'Linux': ('linux', None), 'Windows · otomatik': ('windows', None),
           'Windows · Domain': ('windows', 'domain'), 'Windows · Lokal': ('windows', 'local')}


def access_label(kind, acct):
    """(OS, hesap türü) -> tek satırlık erişim etiketi."""
    if kind == 'linux':
        return 'Linux'
    if kind == 'windows':
        return {'domain': 'Windows · Domain', 'local': 'Windows · Lokal'}.get(acct, 'Windows · otomatik')
    return 'otomatik'


def access_value(label):
    """Erişim etiketi -> (OS, hesap türü); 'otomatik' -> OS port yoklamasıyla belirlenir."""
    return _ACCESS.get(label, (None, None))


class RowDialog(tk.Toplevel):
    """Tek bir sunucu satırını düzenler: ad, IP, OS, hesap türü, kullanıcı, şifre."""

    def __init__(self, parent, row, on_save):
        super().__init__(parent)
        self.title(row.get('name') or row.get('ip') or 'Sunucu')
        self.configure(bg=BG)
        self.transient(parent)
        self.row, self.on_save = row, on_save
        self.name = tk.StringVar(value=row['name'])
        self.ip = tk.StringVar(value=row['ip'])
        self.user = tk.StringVar(value=row['user'])
        self.pw = tk.StringVar(value=row['pw'])
        self.access = tk.StringVar(value=access_label(row['kind'], row.get('acct')))
        self.show_pw = tk.BooleanVar(value=False)

        frm = tk.Frame(self, bg=BG)
        frm.pack(padx=20, pady=(16, 6))

        def field(r, label, widget):
            tk.Label(frm, text=label, bg=BG, fg='#57534b', font=(FONT, 9), anchor='w', width=14).grid(
                row=r, column=0, sticky='w', pady=4)
            widget.grid(row=r, column=1, sticky='w', padx=(8, 0), pady=4)

        field(0, 'VM adı', ttk.Entry(frm, textvariable=self.name, width=34))
        field(1, 'IP', ttk.Entry(frm, textvariable=self.ip, width=34))
        if row.get('dns'):
            field(2, 'DNS adı', tk.Label(frm, text=row['dns'], bg=BG, fg=MUTED, font=(FONT, 9)))
        field(3, 'Erişim', ttk.Combobox(frm, textvariable=self.access, state='readonly', width=31,
                                        values=ACCESS_CHOICES))
        user_entry = ttk.Entry(frm, textvariable=self.user, width=34)
        field(5, 'Kullanıcı', user_entry)
        self.pw_entry = ttk.Entry(frm, textvariable=self.pw, width=34, show='*')
        field(6, 'Şifre', self.pw_entry)
        ttk.Checkbutton(frm, text='Şifreyi göster', variable=self.show_pw, style='Card.TCheckbutton',
                        command=lambda: self.pw_entry.configure(show='' if self.show_pw.get() else '*')
                        ).grid(row=7, column=1, sticky='w', padx=(8, 0))

        btns = tk.Frame(self, bg=BG)
        btns.pack(fill='x', padx=20, pady=(8, 16))
        tk.Button(btns, text='Kaydet', command=self.save, bg=TEAL, fg='#ffffff', activebackground=TEAL_DK,
                  activeforeground='#ffffff', relief='flat', font=(FONT, 10, 'bold'), padx=22, pady=5,
                  bd=0, cursor='hand2').pack(side='right')
        ttk.Button(btns, text='İptal', command=self.destroy).pack(side='right', padx=8, ipady=3)
        self.bind('<Return>', lambda e: self.save())
        self.bind('<Escape>', lambda e: self.destroy())
        user_entry.focus_set()
        self.grab_set()

    def save(self):
        r = self.row
        r['ip'] = self.ip.get().strip()
        r['name'] = self.name.get().strip() or r['ip']
        r['kind'], r['acct'] = access_value(self.access.get())
        r['user'] = self.user.get().strip()
        r['pw'] = self.pw.get()
        self.on_save()
        self.destroy()


class ListEditor(tk.Toplevel):
    """Sunucu listesini uygulama içinde düzenler (Excel açmadan)."""
    COLS = (('name', 'VM adı', 150), ('ip', 'IP', 100), ('access', 'Erişim', 140),
            ('dns', 'DNS adı (VMware Tools)', 160), ('user', 'Kullanıcı', 110), ('pw', 'Şifre', 70))

    def __init__(self, app, targets):
        super().__init__(app)
        self.app = app
        self.title('Sunucu listesi')
        self.configure(bg=BG)
        self.transient(app)
        self.rows = [dict({'user': '', 'pw': '', 'kind': None, 'acct': None, 'dns': ''}, **t) for t in targets]
        self.sort_col, self.sort_rev = None, False

        top = tk.Frame(self, bg=BG)
        top.pack(fill='x', padx=14, pady=(12, 4))
        for text, cmd in (('Satır ekle', self.add_row), ('Seçilileri sil', self.del_rows),
                          ('Yalnızca seçilileri tut', self.keep_rows),
                          ("DNS'ten hesap türü öner", self.suggest_acct)):
            ttk.Button(top, text=text, style='Ghost.TButton', command=cmd).pack(side='left', padx=(0, 8))
        self.count = tk.Label(top, bg=BG, fg=MUTED, font=(FONT, 9))
        self.count.pack(side='right')

        bulk = tk.Frame(self, bg=BG)
        bulk.pack(fill='x', padx=14, pady=(0, 6))
        tk.Label(bulk, text='Seç:', bg=BG, fg='#57534b', font=(FONT, 9)).pack(side='left')
        self.pick = ttk.Combobox(bulk, state='readonly', width=18, values=SELECT_CHOICES)
        self.pick.set('(seç…)')
        self.pick.pack(side='left', padx=(6, 12))
        self.pick.bind('<<ComboboxSelected>>', lambda e: self.select_by(self.pick.get()))
        tk.Button(bulk, text='Seçililere aynı cred / erişim türü ata…', command=self.bulk_cred, bg=TEAL,
                  fg='#ffffff', activebackground=TEAL_DK, activeforeground='#ffffff', relief='flat',
                  font=(FONT, 9, 'bold'), padx=14, pady=4, bd=0, cursor='hand2').pack(side='left')
        tk.Label(bulk, text='Ctrl/Shift ile çoklu seçim · sağ tık menüsü', bg=BG, fg=MUTED,
                 font=(FONT, 9)).pack(side='left', padx=12)

        wrap = tk.Frame(self, bg=BG)
        wrap.pack(fill='both', expand=True, padx=14)
        self.tree = ttk.Treeview(wrap, columns=[c[0] for c in self.COLS], show='headings',
                                 selectmode='extended', height=18)
        for key, title, width in self.COLS:
            self.tree.heading(key, text=title, command=lambda k=key: self.sort_by(k))
            self.tree.column(key, width=width, anchor='w')
        sb = ttk.Scrollbar(wrap, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side='left', fill='both', expand=True)
        sb.pack(side='left', fill='y')
        self.menu = tk.Menu(self, tearoff=0)
        for label, cmd in (('Satırı düzenle…', self.edit_row),
                           ('Seçililere aynı cred / erişim türü ata…', self.bulk_cred),
                           ('Yalnızca seçilileri tut', self.keep_rows), ('Seçilileri sil', self.del_rows)):
            self.menu.add_command(label=label, command=cmd)
        self.tree.bind('<Button-3>', self._context)
        self.tree.bind('<Double-1>', self.edit_row)
        self.tree.bind('<Return>', lambda e: self.edit_row())
        self.tree.bind('<Control-a>', lambda e: self.tree.selection_set(self.tree.get_children()))

        tk.Label(self, bg=BG, fg=MUTED, font=(FONT, 9), anchor='w',
                 text='Satıra çift tıkla: kullanıcı, şifre ve hesap türünü gir. OS "otomatik" ise port yoklamasıyla belirlenir. '
                      'Şifreler yalnızca bellekte tutulur, dosyaya yazılmaz.').pack(
            fill='x', padx=14, pady=(6, 0))
        bottom = tk.Frame(self, bg=BG)
        bottom.pack(fill='x', padx=14, pady=12)
        tk.Button(bottom, text='Tamam', command=self.ok, bg=TEAL, fg='#ffffff', activebackground=TEAL_DK,
                  activeforeground='#ffffff', relief='flat', font=(FONT, 10, 'bold'), padx=22, pady=5,
                  bd=0, cursor='hand2').pack(side='right')
        ttk.Button(bottom, text='İptal', command=self.destroy).pack(side='right', padx=8, ipady=3)
        self.refresh()
        self.grab_set()

    @staticmethod
    def _vals(r):
        return (r['name'], r['ip'], access_label(r['kind'], r.get('acct')), r.get('dns') or '', r['user'],
                '•' * 8 if r['pw'] else '')

    def _sort_key(self, key, r):
        if key == 'ip':
            try:
                return [int(x) for x in r['ip'].strip().split('.')]
            except ValueError:
                return [999, 999, 999, 999]
        if key == 'pw':
            return [0 if r['pw'] else 1]
        idx = [c[0] for c in self.COLS].index(key)
        text = str(self._vals(r)[idx]).lower()  # sayıları doğal sırala: srv2 < srv10
        return [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', text)]

    def sort_by(self, key):
        """Başlığa tıkla: A-Z; tekrar tıkla: Z-A. Seçim korunur."""
        self.sort_rev = (not self.sort_rev) if self.sort_col == key else False
        self.sort_col = key
        selected = {id(self.rows[int(i)]) for i in self.tree.selection()}
        self.rows.sort(key=lambda r: self._sort_key(key, r), reverse=self.sort_rev)
        self.refresh()
        self.tree.selection_set([str(i) for i, r in enumerate(self.rows) if id(r) in selected])
        for k, title, _ in self.COLS:
            mark = (' \u25BC' if self.sort_rev else ' \u25B2') if k == key else ''
            self.tree.heading(k, text=title + mark)

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        for i, r in enumerate(self.rows):
            self.tree.insert('', 'end', iid=str(i), values=self._vals(r))
        self.count.configure(text=f'{len(self.rows)} sunucu')

    def edit_row(self, event=None):
        """Satıra çift tık (ya da Enter): kullanıcı/şifre/hesap türü penceresi."""
        if event is not None:
            if self.tree.identify('region', event.x, event.y) not in ('cell', 'tree'):
                return
            iid = self.tree.identify_row(event.y)
        else:
            sel = self.tree.selection()
            iid = sel[0] if sel else ''
        if iid:
            RowDialog(self, self.rows[int(iid)],
                      lambda: (self.refresh(), self.tree.selection_set(iid)))

    def add_row(self):
        self.rows.append({'name': '', 'ip': '', 'kind': None, 'acct': None, 'dns': '', 'user': '', 'pw': ''})
        self.refresh()
        last = str(len(self.rows) - 1)
        self.tree.selection_set(last)
        self.tree.see(last)

    def del_rows(self):
        sel = {int(i) for i in self.tree.selection()}
        self.rows = [r for i, r in enumerate(self.rows) if i not in sel]
        self.refresh()

    def keep_rows(self):
        sel = {int(i) for i in self.tree.selection()}
        if not sel:
            messagebox.showinfo(APP_TITLE, 'Önce tutmak istediğin satırları seç (Ctrl+tık ile çoklu).',
                                parent=self)
            return
        self.rows = [r for i, r in enumerate(self.rows) if i in sel]
        self.refresh()

    def _context(self, event):
        iid = self.tree.identify_row(event.y)
        if iid and iid not in self.tree.selection():
            self.tree.selection_set(iid)
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def select_by(self, choice):
        """Listeden türe göre toplu seçim (Linux, Windows, cred'siz…)."""
        preds = {
            'Tümü': lambda r: True,
            'Linux': lambda r: r['kind'] == 'linux',
            'Windows': lambda r: r['kind'] == 'windows',
            'Windows · Domain': lambda r: r['kind'] == 'windows' and r.get('acct') == 'domain',
            'Windows · Lokal': lambda r: r['kind'] == 'windows' and r.get('acct') == 'local',
            "Cred'siz olanlar": lambda r: not (r['user'] and r['pw']),
            'Seçimi kaldır': lambda r: False,
        }
        pred = preds.get(choice)
        if pred:
            self.tree.selection_set([str(i) for i, r in enumerate(self.rows) if pred(r)])
        self.pick.set('(seç…)')


    def suggest_acct(self):
        """Windows + hesap türü 'otomatik' satırlar: FQDN (noktalı) -> Domain, kısa ad -> Lokal."""
        dom = loc = none = 0
        for r in self.rows:
            if r['kind'] != 'windows' or r.get('acct'):
                continue
            dns = (r.get('dns') or '').strip()
            if '.' in dns:
                r['acct'], dom = 'domain', dom + 1
            elif dns:
                r['acct'], loc = 'local', loc + 1
            else:
                none += 1
        self.refresh()
        messagebox.showinfo(APP_TITLE, f'{dom} satır Domain, {loc} satır Lokal önerildi; '
                                       f'{none} satırda DNS adı yok (otomatik kaldı).\n\n'
                                       'Bu bir tahmin: lokal olanlara kullanıcı/şifre girmeyi unutma. '
                                       'Taramadan sonra "Domain / Workgroup" kolonu gerçeği gösterir.',
                            parent=self)

    def bulk_cred(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo(APP_TITLE, 'Önce listeden satır seç (Ctrl+A: tümü, Shift/Ctrl: çoklu).',
                                parent=self)
            return
        dlg = tk.Toplevel(self)
        dlg.title('Seçililere cred / hesap türü ata')
        dlg.configure(bg=BG)
        dlg.transient(self)
        tk.Label(dlg, text=f'{len(sel)} satıra uygulanacak', bg=BG, fg=INK,
                 font=(FONT, 10, 'bold')).pack(anchor='w', padx=16, pady=(14, 6))
        user, pw, acct = tk.StringVar(), tk.StringVar(), tk.StringVar(value='(değiştirme)')
        tk.Label(dlg, text='Erişim türü', bg=BG, fg='#57534b', font=(FONT, 9)).pack(
            anchor='w', padx=16)
        ttk.Combobox(dlg, textvariable=acct, state='readonly', width=31,
                     values=('(değiştirme)',) + ACCESS_CHOICES).pack(padx=16, pady=(2, 8))
        for label, var, secret in (('Kullanıcı (boşsa dokunma)', user, False),
                                   ('Şifre (boşsa dokunma)', pw, True)):
            tk.Label(dlg, text=label, bg=BG, fg='#57534b', font=(FONT, 9)).pack(anchor='w', padx=16)
            ttk.Entry(dlg, textvariable=var, width=34, show='*' if secret else '').pack(
                padx=16, pady=(2, 8))

        def apply():
            for i in sel:
                row = self.rows[int(i)]
                if user.get().strip() or pw.get():
                    row['user'], row['pw'] = user.get().strip(), pw.get()
                if acct.get() != '(değiştirme)':
                    row['kind'], row['acct'] = access_value(acct.get())
            self.refresh()
            self.tree.selection_set(sel)
            dlg.destroy()
        ttk.Button(dlg, text='Uygula', command=apply).pack(pady=(0, 14))
        dlg.grab_set()
        dlg.wait_window()

    def ok(self):
        valid = [r for r in self.rows if _IPV4.fullmatch((r['ip'] or '').strip())]
        for r in valid:
            r['ip'] = r['ip'].strip()
            r['name'] = (r['name'] or '').strip() or r['ip']
        dropped = len(self.rows) - len(valid)
        if dropped:
            messagebox.showwarning(APP_TITLE, f'{dropped} satırda geçerli IP yok, listeden çıkarıldı.',
                                   parent=self)
        self.app.set_targets(valid)
        self.destroy()


HELP_TEXT = (
    ('h1', 'PrivScope nasıl kullanılır?'),
    ('p', 'Sunucularda kimin yönetici (yetkili) olduğunu tarar ve tek dosyalık HTML rapor üretir. '
          'Yalnızca okuma yapar; şifreler yalnızca bellekte tutulur.'),
    ('h2', 'Hızlı başlangıç'),
    ('li', '1. Sunucu listesi: "vCenter\'dan canlı çek" ya da "Hazır dosyadan oku" (RVTools/Excel/CSV/txt) seç, '
           '"Listeyi getir"e bas.'),
    ('li', '2. Açılan tabloda satıra çift tıkla: kullanıcı, şifre ve Erişim türünü gir. Ortak şifreli '
           'sunucuları seçip "Seçililere aynı cred / erişim türü ata…" ile toplu gir.'),
    ('li', '3. Windows / Linux varsayılan giriş bilgilerini gir (tümünde aynıysa).'),
    ('li', '4. İstersen "Domain yetkileri"ni Etkin yap, DC adresini yaz. Özel yetkili grupların (örn. VIP0) '
           'adını "Ek gruplar"a yaz ya da otomatik buldur.'),
    ('li', '5. "Taramayı başlat", kayıt yerini seç, bitince HTML raporu tarayıcıda aç.'),
    ('h2', 'Erişim türleri'),
    ('li', 'Windows · Domain: domain\'e bağlı Windows. Üstteki domain admin bilgisi kullanılır.'),
    ('li', 'Windows · Lokal: domain dışı Windows. Satırdaki yerel yönetici (örn. Administrator) kullanılır.'),
    ('li', 'Linux: SSH ile bağlanır. Satırdaki kullanıcı/şifre (root ya da sudo yetkili).'),
    ('li', 'otomatik: işletim sistemi bilinmiyorsa portlara bakılarak belirlenir.'),
    ('h2', 'Hedeflerde gerekenler'),
    ('li', 'Windows: yerel Administrators üyesi hesap. WinRM açık olmalı (5985/5986): yönetici PowerShell\'de '
           '"Enable-PSRemoting -Force". WinRM yoksa otomatik WMI/DCOM (135 + dinamik RPC), o da yoksa ADSI (445) denenir.'),
    ('li', 'Lokal Windows\'ta yerleşik olmayan yönetici hesabı için: HKLM\\SOFTWARE\\Microsoft\\Windows\\'
           'CurrentVersion\\Policies\\System\\LocalAccountTokenFilterPolicy = 1 (DWORD).'),
    ('li', 'Domain grupları: DC\'de WinRM ve ActiveDirectory modülü.'),
    ('li', 'Linux: SSH (22), parola girişi açık, root ya da sudo yetkili hesap (sudo için aynı şifre kullanılır).'),
    ('li', 'vCenter: 443, en az salt okunur (Read-only) rol.'),
    ('h2', 'Sık karşılaşılan mesajlar'),
    ('li', 'kimlik doğrulama reddedildi: kullanıcı/şifre o makinede geçersiz. Lokal makinede "Windows · Lokal" seç.'),
    ('li', 'zaman aşımı: makine kapalı ya da güvenlik duvarı engelliyor (5985/135/445).'),
    ('li', 'bağlantı reddedildi: port dinlenmiyor (WinRM kapalı).'),
    ('li', 'Erişim: cred yok: satırda ve varsayılanda kullanıcı/şifre girilmemiş.'),
    ('li', 'InvalidLogin (vCenter): vCenter kullanıcı adı ya da şifresi yanlış.'),
    ('h2', 'Güvenlik'),
    ('li', 'Hiçbir hesabı, grubu ya da ayarı değiştirmez. Şifreler diske yazılmaz, günlükte gizlenir.'),
    ('li', 'Yanlış şifreyle çok sayıda makineye denemek hesabı kilitleyebilir; ilk taramada küçük bir liste dene.'),
    ('li', 'Rapor gerçek hesap bilgisi içerir; ekip dışına çıkarmadan önce kontrol et.'),
)


class HelpWindow(tk.Toplevel):
    """Uygulama içi kısa kullanım kılavuzu (F1 / Yardım düğmesi)."""

    def __init__(self, parent):
        super().__init__(parent)
        self.title('PrivScope Yardım')
        self.configure(bg=BG)
        self.geometry('720x640')
        self.minsize(520, 360)
        wrap = tk.Frame(self, bg=BG)
        wrap.pack(fill='both', expand=True, padx=14, pady=14)
        box = ScrolledText(wrap, wrap='word', relief='flat', bd=0, bg=CARD, fg=INK, padx=18, pady=14,
                           font=(FONT, 10), highlightthickness=1, highlightbackground=EDGE)
        box.pack(fill='both', expand=True)
        box.tag_configure('h1', font=(FONT, 15, 'bold'), spacing1=2, spacing3=6)
        box.tag_configure('h2', font=(FONT, 11, 'bold'), foreground=TEAL, spacing1=14, spacing3=4)
        box.tag_configure('p', spacing3=4)
        box.tag_configure('li', lmargin1=14, lmargin2=30, spacing3=3)
        for style, text in HELP_TEXT:
            box.insert('end', text + '\n', style)
        box.configure(state='disabled')
        self.bind('<Escape>', lambda e: self.destroy())


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.configure(bg=BG)
        self.resizable(False, False)
        self.q = queue.Queue()
        self.rep = None
        self.vars = {}
        self.entries = {}
        self.source = tk.StringVar(value='vcenter')
        self.domain = tk.BooleanVar(value=False)
        self.auto_groups = tk.BooleanVar(value=False)
        self.workers = tk.IntVar(value=8)
        self.prog_txt = tk.StringVar(value='0 / 0')
        self.targets = None  # düzenlenebilir sunucu listesi (None = tarama sırasında otomatik)
        self.list_info = tk.StringVar()
        self._styles()
        self._header()
        body = tk.Frame(self, bg=BG)
        body.pack(fill='both', expand=True, padx=24, pady=16)
        left = tk.Frame(body, bg=BG)
        left.pack(side='left', fill='y')
        right = tk.Frame(body, bg=BG)
        right.pack(side='left', fill='both', expand=True, padx=(16, 0))
        self._source_card(left)
        self._cred_card(left)
        self._run_card(right)
        self._domain_card(right)
        self._log_card(right)
        self.sync_source()
        self.sync_domain()
        self.bind('<F1>', self.show_help)
        self.after(100, self.poll)

    # -- kurulum ----------------------------------------------------------
    def _styles(self):
        st = ttk.Style(self)
        st.theme_use('clam')
        st.configure('TEntry', fieldbackground='#ffffff', bordercolor='#cfcabe',
                     lightcolor='#cfcabe', darkcolor='#cfcabe', padding=5)
        st.configure('Seg.Toolbutton', background='#ffffff', foreground='#57534b',
                     bordercolor='#cfcabe', padding=(10, 6), font=(FONT, 9), anchor='center')
        st.map('Seg.Toolbutton', background=[('selected', TEAL), ('active', '#e6f4f1')],
               foreground=[('selected', '#ffffff')])
        st.configure('Teal.Horizontal.TProgressbar', troughcolor='#e8e5de', background=TEAL,
                     bordercolor='#e8e5de', lightcolor=TEAL, darkcolor=TEAL, thickness=8)
        st.configure('Ghost.TButton', background='#ffffff', foreground=TEAL, bordercolor=TEAL,
                     padding=(12, 5), font=(FONT, 9, 'bold'))
        st.map('Ghost.TButton', background=[('active', '#e6f4f1')])
        st.configure('Treeview', rowheight=24, font=(FONT, 9))
        st.configure('Treeview.Heading', font=(FONT, 9, 'bold'))
        st.configure('Card.TCheckbutton', background=CARD, foreground=INK, font=(FONT, 9))
        st.map('Card.TCheckbutton', background=[('active', CARD)])

    def show_help(self, event=None):
        if getattr(self, '_help', None) is not None and self._help.winfo_exists():
            self._help.lift()
            self._help.focus_set()
        else:
            self._help = HelpWindow(self)

    def _header(self):
        h = tk.Frame(self, bg=DARK)
        h.pack(fill='x')
        box = tk.Frame(h, bg=DARK)
        box.pack(side='left', padx=24, pady=14)
        tk.Label(box, text='PrivScope', bg=DARK, fg='#f4f2ee', font=(FONT, 15, 'bold')).pack(anchor='w')
        tk.Label(box, text='Yetkili hesap envanteri: Domain, Windows, Linux', bg=DARK,
                 fg='#a7c4bf', font=(FONT, 9)).pack(anchor='w')
        tk.Label(h, text='Yalnızca okuma yapar. Şifreler diske yazılmaz.', bg=DARK, fg='#a7c4bf',
                 font=(FONT, 9)).pack(side='right', padx=(8, 24))
        tk.Button(h, text='Yardım (F1)', command=self.show_help, bg='#1c4b45', fg='#f4f2ee',
                  activebackground='#2a5c55', activeforeground='#ffffff', relief='flat', bd=0,
                  font=(FONT, 9, 'bold'), padx=12, pady=5, cursor='hand2').pack(side='right')

    def _card(self, parent, step, title):
        outer = tk.Frame(parent, bg=CARD, highlightbackground=EDGE, highlightthickness=1)
        outer.pack(fill='x', pady=(0, 12))
        head = tk.Frame(outer, bg=CARD)
        head.pack(fill='x', padx=14, pady=(12, 6))
        c = tk.Canvas(head, width=22, height=22, bg=CARD, highlightthickness=0)
        c.create_oval(1, 1, 21, 21, fill=TEAL if step != '3' else '#cfcabe', outline='')
        c.create_text(11, 11, text=step, fill='white', font=(FONT, 9, 'bold'))
        c.pack(side='left')
        tk.Label(head, text=title, bg=CARD, fg=INK, font=(FONT, 11, 'bold')).pack(side='left', padx=8)
        inner = tk.Frame(outer, bg=CARD)
        inner.pack(fill='x', padx=14, pady=(0, 12))
        return head, inner

    def _entry(self, parent, key, secret=False, width=24):
        self.vars.setdefault(key, tk.StringVar())
        e = ttk.Entry(parent, textvariable=self.vars[key], width=width, show='*' if secret else '')
        self.entries[key] = e
        return e

    def _field(self, parent, label, key, secret=False, width=24):
        tk.Label(parent, text=label, bg=CARD, fg='#57534b', font=(FONT, 9)).pack(anchor='w', pady=(4, 2))
        self._entry(parent, key, secret, width).pack(fill='x')

    def _note(self, parent, text, bg=CARD, fg=MUTED):
        tk.Label(parent, text=text, bg=bg, fg=fg, font=(FONT, 9), wraplength=400,
                 justify='left').pack(anchor='w', pady=(8, 0), fill='x')

    # -- kartlar ----------------------------------------------------------
    def _source_card(self, parent):
        _, inner = self._card(parent, '1', 'Sunucu listesi')
        seg = tk.Frame(inner, bg=CARD)
        seg.pack(fill='x')
        for text, val in (('vCenter\'dan canlı çek', 'vcenter'), ('Hazır dosyadan oku', 'file')):
            ttk.Radiobutton(seg, text=text, variable=self.source, value=val, style='Seg.Toolbutton',
                            command=self.sync_source).pack(side='left', fill='x', expand=True)
        self.switch = tk.Frame(inner, bg=CARD)
        self.switch.pack(fill='x', pady=(10, 0))

        self.frame_file = tk.Frame(self.switch, bg=CARD)
        row = tk.Frame(self.frame_file, bg=CARD)
        row.pack(fill='x')
        self._entry(row, 'ip_file', width=40).pack(side='left', fill='x', expand=True)
        ttk.Button(row, text='Gözat', style='Ghost.TButton', command=self.browse).pack(
            side='left', padx=(8, 0))
        self._note(self.frame_file, 'RVTools, Excel, CSV veya txt. IP yeterli; işletim sistemi '
                                    'dosyadaki OS kolonundan alınır.')

        self.frame_vc = tk.Frame(self.switch, bg=CARD)
        self._field(self.frame_vc, 'vCenter (host veya IP)', 'vc_host', width=40)
        self._field(self.frame_vc, 'Kullanıcı', 'vc_user', width=40)
        self._field(self.frame_vc, 'Şifre', 'vc_pw', True, width=40)

        bar = tk.Frame(inner, bg=CARD)
        bar.pack(fill='x', pady=(10, 0))
        ttk.Button(bar, text='Listeyi getir', style='Ghost.TButton', command=self.load_list).pack(side='left')
        self.btn_edit = ttk.Button(bar, text='Düzenle', style='Ghost.TButton', command=self.edit_list)
        self.btn_edit.pack(side='left', padx=8)
        tk.Label(bar, textvariable=self.list_info, bg=CARD, fg=MUTED, font=(FONT, 9)).pack(side='left')

    def _cred_card(self, parent):
        _, inner = self._card(parent, '2', 'Varsayılan giriş bilgileri')
        cols = tk.Frame(inner, bg=CARD)
        cols.pack(fill='x')
        for i, (title, ukey, pkey, ulabel) in enumerate((
                ('WINDOWS · domain admin', 'win_user', 'win_pw', 'Kullanıcı (DOMAIN\\user)'),
                ('LINUX · tümünde aynıysa', 'lin_user', 'lin_pw', 'Kullanıcı'))):
            col = tk.Frame(cols, bg=CARD)
            col.pack(side='left', fill='x', expand=True, padx=(0, 10) if i == 0 else 0)
            tk.Label(col, text=title, bg=CARD, fg=TEAL, font=(FONT, 8, 'bold')).pack(anchor='w')
            self._field(col, ulabel, ukey, width=18)
            self._field(col, 'Şifre', pkey, True, width=18)
        tip = tk.Frame(inner, bg='#e6f4f1')
        tip.pack(fill='x', pady=(10, 0))
        tk.Label(tip, bg='#e6f4f1', fg='#14524c', font=(FONT, 9), wraplength=400, justify='left',
                 text='Şifreleri farklı sunucular (lokal Windows, farklı Linux hesapları) için '
                      '"Listeyi getir" sonrası "Düzenle" tablosundaki Kullanıcı ve Şifre '
                      'sütunlarını doldur (dosya kaynağında dosyadaki Username/Password '
                      'kolonları da okunur). O satırda tablodaki bilgi kullanılır.').pack(
            anchor='w', padx=10, pady=8)

    def _domain_card(self, parent):
        head, inner = self._card(parent, '3', 'Domain yetkileri (isteğe bağlı)')
        ttk.Checkbutton(head, text='Etkin', variable=self.domain, style='Card.TCheckbutton',
                        command=self.sync_domain).pack(side='right')
        row = tk.Frame(inner, bg=CARD)
        row.pack(fill='x')
        tk.Label(row, text='DC adresi', bg=CARD, fg='#57534b', font=(FONT, 9), width=10,
                 anchor='w').pack(side='left')
        self._entry(row, 'dc_host', width=30).pack(side='left', fill='x', expand=True)
        row2 = tk.Frame(inner, bg=CARD)
        row2.pack(fill='x', pady=(6, 0))
        tk.Label(row2, text='Ek gruplar', bg=CARD, fg='#57534b', font=(FONT, 9), width=10,
                 anchor='w').pack(side='left')
        self._entry(row2, 'extra_groups', width=30).pack(side='left', fill='x', expand=True)
        self.chk_auto = ttk.Checkbutton(inner, text='Yetkili özel grupları otomatik bul (adminCount=1)',
                                        variable=self.auto_groups, style='Card.TCheckbutton')
        self.chk_auto.pack(anchor='w', pady=(6, 0))
        self._note(inner, 'DC\'de Windows bilgileriyle Domain/Enterprise Admins gibi gruplar okunur. '
                          'Özel yetkili grupların (örn. VIP0) adını virgülle yaz ya da otomatik buldur.')

    def _run_card(self, parent):
        outer = tk.Frame(parent, bg=CARD, highlightbackground=EDGE, highlightthickness=1)
        outer.pack(fill='x', pady=(0, 12))
        inner = tk.Frame(outer, bg=CARD)
        inner.pack(fill='x', padx=14, pady=14)
        btns = tk.Frame(inner, bg=CARD)
        btns.pack(fill='x')
        self.btn_start = tk.Button(btns, text='Taramayı başlat', command=self.start, bg=TEAL, fg='#ffffff',
                                   activebackground=TEAL_DK, activeforeground='#ffffff', relief='flat',
                                   font=(FONT, 11, 'bold'), cursor='hand2', pady=8, bd=0)
        self.btn_start.pack(side='left', fill='x', expand=True)
        self.btn_cancel = ttk.Button(btns, text='İptal', command=self.cancel, state='disabled')
        self.btn_cancel.pack(side='left', padx=(8, 0), ipady=4)

        top = tk.Frame(inner, bg=CARD)
        top.pack(fill='x', pady=(14, 4))
        tk.Label(top, text='İlerleme', bg=CARD, fg='#57534b', font=(FONT, 9)).pack(side='left')
        tk.Label(top, textvariable=self.prog_txt, bg=CARD, fg='#57534b', font=(FONT, 9)).pack(side='right')
        self.bar = ttk.Progressbar(inner, mode='determinate', style='Teal.Horizontal.TProgressbar')
        self.bar.pack(fill='x')

        tiles = tk.Frame(inner, bg=CARD)
        tiles.pack(fill='x', pady=(12, 0))
        self.stat = {}
        for key, label, bg, fg in (('win', 'Windows', BG, INK), ('lin', 'Linux', BG, INK),
                                   ('fail', 'Erişilemeyen', '#fbeae8', '#a1291f')):
            t = tk.Frame(tiles, bg=bg)
            t.pack(side='left', fill='x', expand=True, padx=(0, 8) if key != 'fail' else 0)
            self.stat[key] = tk.Label(t, text='0', bg=bg, fg=fg, font=(FONT, 16, 'bold'))
            self.stat[key].pack(anchor='w', padx=10, pady=(6, 0))
            tk.Label(t, text=label, bg=bg, fg=fg if key == 'fail' else '#57534b',
                     font=(FONT, 9)).pack(anchor='w', padx=10, pady=(0, 6))

        opt = tk.Frame(inner, bg=CARD)
        opt.pack(fill='x', pady=(10, 0))
        tk.Label(opt, text='Paralel tarama', bg=CARD, fg='#57534b', font=(FONT, 9)).pack(side='left')
        ttk.Spinbox(opt, from_=1, to=32, width=4, textvariable=self.workers).pack(side='left', padx=8)

    def _log_card(self, parent):
        box = tk.Frame(parent, bg='#16201f')
        box.pack(fill='both', expand=True)
        tk.Label(box, text='GÜNLÜK', bg='#16201f', fg='#7fa39d', font=(FONT, 8, 'bold')).pack(
            anchor='w', padx=14, pady=(12, 4))
        self.log_box = ScrolledText(box, width=48, height=8, state='disabled', bg='#16201f',
                                    fg='#cfe3df', insertbackground='#cfe3df', relief='flat',
                                    font=('Consolas', 9), wrap='word', padx=8, pady=4, bd=0)
        self.log_box.pack(fill='both', expand=True, padx=6, pady=(0, 8))
        self.log_box.tag_configure('ts', foreground='#7fa39d')
        self.log_box.tag_configure('ok', foreground='#5eead4')
        self.log_box.tag_configure('bad', foreground='#fca5a5')

    # -- davranış ---------------------------------------------------------
    def set_targets(self, targets):
        self.targets = targets
        if targets is None:
            self.list_info.set('Liste yok: tarama sırasında otomatik alınır')
        else:
            own = sum(bool(t.get('user') and t.get('pw')) for t in targets)
            self.list_info.set(f'{len(targets)} sunucu ({own} tanesinde kendi cred\'i var)')
        self.btn_edit.configure(state='disabled' if targets is None else 'normal')

    def load_list(self):
        cfg = {k: v.get().strip() for k, v in self.vars.items()}
        if self.source.get() == 'file':
            if not cfg['ip_file']:
                messagebox.showwarning(APP_TITLE, 'Önce bir dosya seç.')
                return
            try:
                self.set_targets(file_targets(cfg['ip_file'], Reporter(self.q, [])))
            except Exception as ex:
                messagebox.showerror(APP_TITLE, f'Dosya okunamadı: {ex}')
                return
            self.edit_list()
        else:
            if not (cfg['vc_host'] and cfg['vc_user'] and cfg['vc_pw']):
                messagebox.showwarning(APP_TITLE, 'vCenter bilgileri eksik.')
                return
            self.list_info.set('vCenter\'dan çekiliyor...')
            threading.Thread(target=self._fetch_list, args=(cfg,), daemon=True).start()

    def _fetch_list(self, cfg):
        rep = Reporter(self.q, [cfg['vc_pw']])
        try:
            self.q.put(('targets', vcenter_targets(cfg['vc_host'], cfg['vc_user'], cfg['vc_pw'], rep)))
        except Exception as ex:
            self.q.put(('listerr', rep.reason(ex)))

    def edit_list(self):
        if self.targets is not None:
            ListEditor(self, self.targets)

    def sync_source(self):
        self.set_targets(None)
        use_file = self.source.get() == 'file'
        (self.frame_vc if use_file else self.frame_file).pack_forget()
        (self.frame_file if use_file else self.frame_vc).pack(fill='x')

    def sync_domain(self):
        state = 'normal' if self.domain.get() else 'disabled'
        for k in ('dc_host', 'extra_groups'):
            self.entries[k].configure(state=state)
        self.chk_auto.configure(state=state)

    def browse(self):
        path = filedialog.askopenfilename(filetypes=[
            ('IP listesi', '*.xlsx *.xlsm *.csv *.txt'), ('Tümü', '*.*')])
        if path:
            self.vars['ip_file'].set(path)
            self.set_targets(None)

    def cancel(self):
        if self.rep:
            self.rep.cancel.set()
            self.log('İptal isteniyor (süren taramalar bitince duracak)...')

    def log(self, msg):
        box = self.log_box
        box.configure(state='normal')
        box.insert('end', f'{datetime.now():%H:%M:%S}  ', 'ts')
        bad = re.search(r'Erişim|HATA|belirlenemedi|İptal', msg)
        tags = ('bad',) if bad else (('ok',) if msg.rstrip().endswith('OK') else ())
        box.insert('end', msg + '\n', tags)
        box.see('end')
        box.configure(state='disabled')

    def set_running(self, running):
        self.btn_start.configure(state='disabled' if running else 'normal',
                                 bg='#8fb9b4' if running else TEAL)
        self.btn_cancel.configure(state='normal' if running else 'disabled')

    def start(self):
        cfg = {k: v.get().strip() for k, v in self.vars.items()}
        cfg['workers'] = max(1, min(32, self.workers.get()))
        cfg['source'] = self.source.get()
        cfg['domain'] = self.domain.get()
        cfg['auto_groups'] = self.auto_groups.get()
        cfg['extra_list'] = [g.strip() for g in cfg.get('extra_groups', '').split(',') if g.strip()]
        if cfg['domain'] and not (cfg['dc_host'] and cfg['win_user'] and cfg['win_pw']):
            messagebox.showwarning(APP_TITLE, 'Domain için DC adresi ve Windows cred gerekli.')
            return
        cfg['targets'] = self.targets
        if self.targets is not None:
            pass  # düzenlenmiş liste hazır; kaynak bilgisi gerekmez
        elif cfg['source'] == 'file':
            if not cfg['ip_file']:
                messagebox.showwarning(APP_TITLE, 'IP listesi dosyası seçilmedi.')
                return
        elif not (cfg['vc_host'] and cfg['vc_user'] and cfg['vc_pw']):
            messagebox.showwarning(APP_TITLE, 'vCenter bilgileri eksik.')
            return
        path = filedialog.asksaveasfilename(
            defaultextension='.html', filetypes=[('HTML', '*.html')],
            initialfile=f'PrivScope_{datetime.now():%Y%m%d}.html')
        if not path:
            return
        self.rep = Reporter(self.q, [cfg['vc_pw'], cfg['win_pw'], cfg['lin_pw']])
        self.set_running(True)
        self.bar.configure(value=0)
        self.prog_txt.set('0 / 0')
        for k in self.stat:
            self.stat[k].configure(text='0')
        threading.Thread(target=run_scan, args=(cfg, self.rep, path), daemon=True).start()

    def poll(self):
        """Worker thread'den gelen mesajları ana thread'de işler."""
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] == 'log':
                    self.log(msg[1])
                elif msg[0] == 'progress':
                    self.bar.configure(maximum=max(msg[2], 1), value=msg[1])
                    self.prog_txt.set(f'{msg[1]} / {msg[2]}')
                elif msg[0] == 'targets':
                    self.set_targets(msg[1])
                    self.edit_list()
                elif msg[0] == 'listerr':
                    self.set_targets(None)
                    messagebox.showerror(APP_TITLE, f'Liste alınamadı: {msg[1]}')
                elif msg[0] == 'stats':
                    for k, n in zip(('win', 'lin', 'fail'), msg[1:]):
                        self.stat[k].configure(text=str(n))
                elif msg[0] == 'done':
                    self.set_running(False)
                    if msg[2]:
                        self.log(f'HATA: {msg[2]}')
                        messagebox.showerror(APP_TITLE, msg[2])
                    else:
                        messagebox.showinfo(APP_TITLE, f'Rapor kaydedildi:\n{msg[1]}')
        except queue.Empty:
            pass
        self.after(100, self.poll)


if __name__ == '__main__':
    try:  # yüksek DPI ekranlarda bulanık/taşan pencereyi önler
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    App().mainloop()
