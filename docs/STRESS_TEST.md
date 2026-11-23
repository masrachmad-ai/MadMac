# Stress Test: 7 Kegagalan Umum

Semua tes ada di `tests/test_stress.py` dan dijalankan dengan Binance PALSU yang sengaja dibuat rusak.
Hasil terakhir saat build: **31 passed** (`python -m pytest -q`). Anda bisa menjalankan ulang di server (DEPLOY langkah 7).

| # | Kegagalan | Yang disimulasikan | Yang terjadi | Perbaikan yang dibangun |
|---|---|---|---|---|
| 1 | **Order dobel** | Anda menekan ✅ dua kali / bot restart dan melihat sinyal yang sama | Hanya **1** order terkirim | ID order dibuat dari sinyal (sinyal sama = ID sama); Journal menandai sinyal yang sudah dieksekusi; Shield menolak posisi kedua |
| 2 | **Stop gagal / hilang** | Binance menolak stop-loss; stop dihapus manual di app | Posisi **ditutup** + rem aktif (atau hanya alarm jika `close_if_stop_fails: false`); stop yang hilang **dipasang ulang** dalam ≤5 detik | Stop selalu dibaca ulang dari Binance setelah dipasang; cek ulang setiap 5 detik |
| 3 | **Ukuran posisi salah** | Risiko 2%, leverage 20x, stop sangat dekat (0,1%), akun kecil ($20) | Ukuran tidak pernah melebihi 2%; dibulatkan ke bawah sesuai lot Binance; leverage tidak pernah > batas; **stop 0,1% DITOLAK** (fee = ~40% risiko); akun $20 ditolak (di bawah minimum Binance) | Penjaga fee; batas leverage memperkecil posisi, tidak pernah menaikkan batas |
| 4 | **Data harga basi** | Harga 2 menit lalu; candle 1 jam lalu; candle yang belum tutup | Entry **diblokir**; Scout diam + alarm sekali | Pakai jam server Binance; candle yang belum tutup dibuang |
| 5 | **API timeout** | Order timeout tapi sebenarnya sampai; order timeout dan tidak jelas | Order **tidak dikirim ulang**: dicari dengan ID-nya → posisi tercatat benar. Jika tetap tidak jelas → **rem + alarm** | Order tidak pernah di-retry buta |
| 6 | **Kesalahan zona waktu** | Server diset ke WIB, jam 06:59 WIB vs 07:00 WIB | Hari trading selalu mulai **00:00 UTC = 07:00 WIB**; rem harian lepas tepat di batas itu | Semua waktu internal UTC; Docker diset `TZ=UTC` |
| 7 | **Partial fill** | Hanya 50% order terisi | Journal mencatat jumlah **yang benar-benar terisi**; stop melindungi seluruh posisi aktual | Stop memakai `closePosition`, jadi mengikuti posisi nyata, bukan rencana |

## Tes tambahan (izin & rem)

| Tes | Hasil |
|---|---|
| Pilot dipanggil tanpa persetujuan Shield atau tanpa izin Anda | **Ditolak keras** (error), 0 order |
| Strategi belum lulus Lab | Diblokir |
| Mode live tanpa mengetik "go live" | Diblokir |
| 3 rugi beruntun | Rem aktif sampai Anda ketik /resume |
| Rugi −4% hari ini | Rem aktif, lepas otomatis 07:00 WIB besok |
| Orang lain mengirim ✅ / "/resume" / "go live" ke bot Anda | Diabaikan dan dicatat |
| /stop | Rem aktif < 1 detik, sinyal yang menunggu dibatalkan |
| Sinyal kedaluwarsa lalu ditekan ✅ | Tidak ada order |
| Alur penuh: sinyal → tidak ada order → Anda tekan ✅ → order + stop | Lolos |

## Yang BELUM teruji (jujur)

- **Panggilan ke Binance asli belum pernah dijalankan.** Lingkungan build saya tidak bisa
  menjangkau Binance (jaringan diblokir). Nama endpoint ditulis dari dokumentasi Binance yang saya
  ketahui, tapi **belum diverifikasi ulang**, terutama endpoint stop-loss "Algo Order" yang
  baru. `selfcheck` di server Anda adalah tes pertama terhadap Binance sungguhan:
  ia memasang stop uji, membacanya kembali, lalu menghapusnya.
- Telegram asli belum diuji. `selfcheck` mengirim pesan uji.
