# PrivScope

**Sunucularınızda kimin yönetici (yetkili) olduğunu tek bakışta gösteren envanter aracı.**
Domain, Windows ve Linux sunucuları tarar; kimin domain admin, yerel Administrators, root ya da sudo yetkisi olduğunu
tek dosyalık, aranabilir bir HTML rapora döker. Yalnızca **okuma** yapar, hiçbir hesabı değiştirmez.
Şifreler yalnızca bellekte tutulur, diske yazılmaz ve günlüğe basılmaz.

![PrivScope ana pencere](docs/img/app-main.png)

## Neler yapar?

- **Domain yetkileri:** Domain Admins, Enterprise Admins, Schema Admins, Administrators, Operator grupları ve Group Policy Creator Owners. Kendi **özel yetkili gruplarını** da (örn. `VIP0`) ekleyebilir ya da otomatik buldurabilirsin.
- **İç içe gruplar açılır:** Bir grubun üyesi başka bir grupsa altındaki kullanıcılar da listelenir.
- **Windows sunucular:** Yerel Administrators, Remote Desktop Users, lokal kullanıcılar (devre dışı olanlar işaretli), silinmiş hesaplar (orphan SID), domain mi workgroup mu.
- **Linux sunucular:** UID 0 hesapları, `sudo`/`wheel`/`admin` grup üyeleri, sudoers kuralları, login shell'i olan kullanıcılar (kilitli olanlar işaretli).
- **Bulgular:** Riskli durumlar Özet sayfasında otomatik listelenir (silinmiş hesap, root dışı UID 0, `NOPASSWD: ALL` vb.).
- **Farklı erişim yolları:** Windows'ta WinRM, yoksa WMI/DCOM, o da yoksa ADSI; Linux'ta SSH. Domain'deki makine de, domain dışındaki lokal makine de taranır.

## EXE ile hızlı başlangıç

1. `PrivScope.exe` dosyasını çalıştır (kurulum gerekmez). Exe yoksa aşağıdaki [EXE üretme](#exe-üretme) bölümüne bak.
2. **Sunucu listesi** kartında kaynağı seç ve listeyi getir.
3. Windows / Linux giriş bilgilerini gir (farklı şifreliler için listede satır satır).
4. **Taramayı başlat**, kayıt yerini seç.
5. Bitince açılan HTML raporu tarayıcıda aç.

## Adım adım kullanım

### 1) Sunucu listesi

İki kaynaktan biri:

| Kaynak | Ne zaman |
|---|---|
| **vCenter'dan canlı çek** | vCenter erişimin varsa. Açık VM'ler ve işletim sistemi otomatik gelir. Appliance/altyapı VM'leri (`vCLS`, `vcsa`, `photon` vb.) elenir. |
| **Hazır dosyadan oku** | RVTools `.xlsx`, Excel, CSV ya da düz txt (satır başına bir IP). RVTools'un `vInfo` sayfasındaki OS ve IP kolonlarından işletim sistemi otomatik alınır. |

**Listeyi getir**'e basınca liste uygulamaya yüklenir ve düzenleme tablosu açılır. Bu tabloyu daha sonra **Düzenle** ile tekrar açabilirsin.

### 2) Listeyi düzenle (Excel açmadan)

![Sunucu listesi düzenleme](docs/img/app-list.png)

- **Satıra çift tıkla** (ya da Enter): VM adı, IP, **Erişim**, kullanıcı ve şifreyi gir.
- **Erişim** seçenekleri:

| Erişim | Anlamı |
|---|---|
| `Windows · Domain` | Domain'e bağlı Windows. Üst ekrandaki domain admin bilgisi kullanılır. |
| `Windows · Lokal` | Domain dışı Windows. Satırdaki lokal hesap (örn. `Administrator`) kullanılır. |
| `Windows · otomatik` | Satırda cred varsa o, yoksa domain admin. |
| `Linux` | SSH ile bağlanır. Satırdaki kullanıcı/şifre kullanılır. |
| `otomatik` | İşletim sistemi bilinmiyorsa portlara bakılarak belirlenir. |

- **Toplu cred (30 Linux'un 15'i aynı şifreliyse):** satırları Ctrl/Shift ile seç (ya da **Seç:** listesinden *Linux*, *Windows*, *Cred'siz olanlar*…), **Seçililere aynı cred / erişim türü ata…** de. Sağ tık menüsünde de var. Kalanlara satıra çift tıklayıp tek tek gir.
- **DNS'ten hesap türü öner:** Windows satırlarında noktalı DNS adı (`sunucu.firma.local`) varsa Domain, kısa adsa Lokal önerir. Bu bir tahmindir; rapordaki **Domain / Workgroup** kolonu gerçeği gösterir.
- Başlığa tıklayınca sıralanır (A-Z, tekrar tıkla Z-A). **Yalnızca seçilileri tut** yetkili olduğun makineleri ayıklamaya yarar.
- Düzenlenen liste taramada doğrudan kullanılır. Kaynağı ya da dosyayı değiştirirsen liste sıfırlanır.

### 3) Varsayılan giriş bilgileri

- **Windows:** domain admin (`ALAN\kullanici`). `Windows · Domain` satırlarında kullanılır.
- **Linux:** tüm sunucularda aynıysa buraya. Farklıysa boş bırakıp tabloya gir.

### 4) Domain yetkileri (isteğe bağlı)

**Etkin** kutusunu işaretle, DC adresini yaz. Windows domain admin bilgisiyle DC'ye bağlanılır (DC'de ActiveDirectory modülü olmalı).

- **Ek gruplar:** Standart olmayan ama yüksek yetkili grupların adını virgülle yaz: `VIP0, Server Admins`.
- **Yetkili özel grupları otomatik bul:** Active Directory'nin "yetkili" işaretini (`adminCount=1`) taşıyan özel grupları kendisi bulur. Yetkisi grup üyeliğinden değil delegasyondan geliyorsa bu işaret olmaz, o zaman adını *Ek gruplar*a yaz.

Domain seçeneği açıkken sunucuların yerel yönetici grubunda geçen domain grupları da DC'den çözülüp altlarına eklenir.

### 5) Tara ve raporu aç

**Taramayı başlat**'a bas. İlerleme çubuğu, Windows/Linux/Erişilemeyen sayaçları ve günlük canlı akar. `OK` yeşil, hatalar kırmızı görünür.

## Raporu okuma

Tek dosyalık HTML: sunucuya gerek yok, tarayıcıda açılır. Solda sayfalar (sayı rozetli), üstte arama kutusu, altta **TR/EN** düğmesi.

| Sayfa | İçerik |
|---|---|
| **Overview** | Taranan sunucu, yetkili hesap sayıları ve **Bulgular** tablosu |
| **Domain Privileges** | Her yetkili grup bir kart; iç içe gruplar `└` ile açılır |
| **Privileged Accounts** | Hesap bazlı görünüm: bir hesap hangi yetkili gruplarda (`via Grup` = iç içe üyelik) |
| **Windows Servers** | Sunucu başına yerel yöneticiler, RDP, lokal kullanıcılar, Domain/Workgroup |
| **Linux Servers** | UID 0, sudo grubu, sudoers kuralları, kullanıcılar |
| **Unreachable** | Bağlanılamayan sunucular ve sebepleri |

![Özet](docs/img/report-overview.png)

![Domain yetkileri](docs/img/report-domain.png)

![Hesap bazlı görünüm](docs/img/report-accounts.png)

![Windows sunucular](docs/img/report-windows.png)

**Rozetler:** `Group` (grup), `Disabled` (devre dışı), `Locked` (kilitli), `Orphan SID` (silinmiş hesap), `Unreachable` (erişilemedi).

## Windows'a nasıl bağlanır?

Sırayla denenir; ilki başarılı olunca durur:

1. **WinRM** (5985/5986): en hızlı ve eksiksiz yol.
2. **WMI/DCOM** (135 + dinamik RPC): WinRM kapalıysa. Grup üyeleri ve Domain/Workgroup bilgisini eksiksiz verir.
3. **ADSI/445**: yalnızca 445 açıksa. Bazı makineler bu yolla grup üyesi vermez; o zaman Administrators için `okunamadi` yazılır.

Kimlik bilgisi **reddedilirse** diğer yöntemler denenmez: aynı yanlış şifreyle ikinci deneme hesabı kilitleyebilir.

Linux'ta SSH (22) kullanılır. Root olmayan kullanıcıda komutlar `sudo -S` ile çalışır (aynı şifre sudo için de kullanılır).

## Gereksinimler

| Ne | Ayrıntı |
|---|---|
| İşletim sistemi | Windows (uygulama Windows'ta çalışır) |
| Python (kaynaktan çalıştırırsan) | 3.11+ |
| Ağ portları | vCenter 443 · Windows 5985/5986 (veya 135+445) · Linux 22 · DC 5985 |
| Yetki | Hedef makinelerde yönetici (Windows) ya da root/sudo (Linux) |

Gerekli paketler: `pip install pyvmomi paramiko openpyxl pypsrp` (`openpyxl` yalnızca Excel/RVTools listesini okumak için).

## Sık karşılaşılan sorunlar

| Mesaj | Anlamı / çözüm |
|---|---|
| `kimlik doğrulama reddedildi` | Kullanıcı/şifre o makinede geçersiz. Lokal makinede *Windows · Lokal* seçip makinenin kendi yöneticisini gir. Domain hesabının o makineye güveni bozuk da olabilir. |
| `zaman aşımı (güvenlik duvarı ya da makine yanıt vermiyor)` | Makine kapalı ya da WinRM/güvenlik duvarı engelliyor. Hedefte `Enable-PSRemoting -Force` ya da 5985 izni gerekir; yoksa WMI/ADSI devreye girer. |
| `bağlantı reddedildi (port kapalı…)` | Port dinlenmiyor (WinRM kapalı). |
| `erişim reddedildi` (ADSI/WMI) | Hesap o makinede yönetici değil ya da şifre geçersiz. |
| `Erişim: cred yok` | O satırda ve varsayılanda kullanıcı/şifre girilmemiş. |
| vCenter `InvalidLogin` | Kullanıcı adı ya da şifre yanlış (ör. `administrator@vsphere.local`). Yazımı kontrol et. |

## Güvenlik

- Şifreler yalnızca bellekte tutulur; günlükte `***` ile maskelenir. Kapatınca silinir.
- Araç yalnızca okuma yapar.
- vCenter/SSH sertifika ve host key doğrulaması kapalıdır (iç ağ varsayımı).
- **Rapor gerçek hesap bilgisi içerir.** Müşteri ya da ekip dışına çıkarmadan önce kontrol et.
- Excel/RVTools listesine yazdığın şifreler düz metindir; tarama sonrası ilgili kolonları sil.

## EXE üretme

```
pip install pyinstaller
pyinstaller --onefile --windowed --name PrivScope pam_audit.py
```

Çıktı: `dist\PrivScope.exe`. Kaynaktan çalıştırmak için: `python pam_audit.py`.

## Ekran görüntüleri hakkında

Bu README'deki görüntüler örnek verilerle (`corp.local`, `srv-*`) üretilmiştir; gerçek bir ortamı göstermez.
