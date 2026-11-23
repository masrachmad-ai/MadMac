# Stress Test: kegagalan umum + risiko multi-koin

Semua tes ada di `tests/` dan dijalankan dengan Binance PALSU yang sengaja dibuat rusak.
Hasil terakhir saat build: **54 passed** (`python -m pytest -q`). Anda bisa menjalankan ulang di server (DEPLOY langkah 7).

## 7 kegagalan dari brief Anda

| # | Kegagalan | Yang disimulasikan | Yang terjadi | Perbaikan yang dibangun |
|---|---|---|---|---|
| 1 | **Order dobel** | Sinyal yang sama dieksekusi dua kali / bot restart | Hanya **1** order | ID order dibuat dari sinyal; Journal menandai sinyal yang sudah dieksekusi; Shield menolak posisi kedua di koin yang sama |
| 2 | **Stop gagal / hilang** | Binance menolak stop; stop dihapus manual | Posisi **ditutup** + rem (atau hanya alarm jika `close_if_stop_fails: false`); stop yang hilang **dipasang ulang** ≤5 detik | Stop selalu dibaca ulang dari Binance |
| 3 | **Ukuran posisi salah** | Leverage terlalu kecil, stop 0,1% (gaya 1 menit), modal $50 | Risiko tidak pernah > 0,5%; lot dibulatkan ke bawah; **stop 0,1% DITOLAK** (fee > 25% risiko); modal kecil ditolak | Penjaga fee; batas leverage memperkecil posisi |
| 4 | **Data harga basi** | Harga 2 menit lalu; candle 1 jam lalu; candle yang belum tutup | Entry **diblokir** per koin; candle yang belum tutup dibuang | Jam server Binance; cek umur data |
| 5 | **API timeout** | Order timeout tapi sampai; timeout dan tidak jelas; angka float "0.30000000000000004" | Tidak dikirim ulang, dicari dengan ID → tercatat benar. Tidak jelas → **rem + alarm**. Angka selalu dikirim bersih. | Order tidak pernah di-retry buta |
| 6 | **Zona waktu** | Server diset ke WIB | Hari trading selalu 00:00 UTC = 07:00 WIB | Semua waktu internal UTC, `TZ=UTC` |
| 7 | **Partial fill** | Limit terisi 50% | Stop dipasang **segera** saat ada yang terisi; sisa order dibatalkan saat timeout; take-profit untuk jumlah yang benar-benar terisi | Stop `closePosition` mengikuti posisi nyata |

## Risiko baru: banyak koin & limit order

| Tes | Hasil |
|---|---|
| Posisi ke-4 saat sudah ada 3 | Ditolak |
| LONG ke-3 saat sudah ada 2 LONG (korelasi ke BTC) | Ditolak |
| Total risiko terbuka hampir penuh | Posisi baru **dikecilkan**; jika ruang < 50% → ditolak |
| Limit order yang belum terisi | Tetap dihitung sebagai eksposur |
| Limit tidak terisi dalam 1 candle | Dibatalkan, tidak ada posisi |
| Post-only ditolak (harga sudah lewat) | Tidak ada trade, tidak dikejar dengan market order |
| Trade melewati batas waktu strategi (3 jam) | Ditutup (bagian dari rencana trade) |
| Posisi di koin yang dipantau tapi bukan dari bot | Rem + alarm, posisi **tidak disentuh** |
| /flat | Semua posisi ditutup, limit dibatalkan |
| Testnet: harga beda dari pasar asli | Entry/stop/target digeser bersama, jarak & risiko tetap |

## Izin & rem

| Tes | Hasil |
|---|---|
| Pilot tanpa persetujuan Shield atau tanpa izin | Error keras, 0 order |
| Mode auto + rem aktif | 0 order |
| Mode per_trade | Tidak ada order sebelum ✅ |
| Pasangan strategi×koin belum lulus Lab / hasil Lab > 30 hari | Diblokir |
| Mode live tanpa "go live" | Diblokir |
| 3 rugi beruntun / rugi −4% hari ini | Rem aktif |
| Orang lain mengirim perintah ke bot Anda | Diabaikan dan dicatat |
| /stop | Rem aktif < 1 detik |

## Kejujuran Lab (tests/test_lab.py)

| Tes | Hasil |
|---|---|
| **Tidak melihat masa depan:** sinyal di candle mana pun tidak berubah ketika data masa depan ditambahkan (6 strategi × 5 titik potong) | Lolos |
| **Data acak tanpa pola** (random walk) | **Semua 6 strategi DITOLAK** |
| **Data dengan momentum nyata** | Strategi tren **DILOLOSKAN**, jadi Lab bisa bilang "ya" kalau buktinya ada |
| Limit hanya "menyentuh" harga | Dianggap **tidak** terisi |
| Stop & target tersentuh di candle yang sama | Dihitung **rugi** |
| Data open interest tidak ada | Dilaporkan "tidak tersedia", tidak dikarang |
| Koreksi banyak uji (Benjamini-Hochberg) & Monte Carlo | Lolos |

## Yang BELUM teruji (jujur)

- **Belum pernah tersambung ke Binance asli.** Lingkungan build memblokir Binance. `selfcheck` di server
  Anda adalah tes pertama: data pasar, open interest, funding, stop-loss uji, dan limit post-only uji.
- **Format arsip `metrics` dan `fundingRate`** belum diverifikasi. Kalau berbeda, Lab melaporkannya.
- Telegram asli belum diuji (`selfcheck` mengirim pesan uji).
