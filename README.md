# MadMac — Tim Bot Trading Futures Multi-Koin (Binance)

Bot algo derivatif untuk **10 koin Binance Futures** (chart 5 menit, strategi *smart money* + data derivatif),
dijalankan oleh beberapa "agent" yang masing-masing punya satu tugas.
**Saat ini semuanya mode PAPER (testnet).** Uang asli baru dipakai setelah Anda mengetik `go live`.

> ⚠️ Tidak ada bot yang menjamin profit atau bisa meramal harga. Bot ini **mengukur** apakah sebuah
> strategi punya keunggulan nyata (setelah fee, di data yang belum pernah dilihat, dibanding entry acak)
> dan **hanya** memakai yang lolos. Modal dilindungi dulu, mencari cuan kedua.

---

## 🟢 START HERE (10 menit)

**Satu hal: buat API key Binance Futures TESTNET dan simpan di tempat aman.**

1. Buka **https://testnet.binancefuture.com** → login / daftar.
2. Di bagian bawah halaman, tab **API Key** → salin **API Key** dan **Secret Key**.
3. Simpan di password manager. **Jangan kirim ke chat siapa pun** (termasuk ke saya).

Setelah itu, ikuti [docs/DEPLOY.md](docs/DEPLOY.md) langkah demi langkah.

---

## Tim

| Agent | Satu tugasnya | Boleh menyentuh uang? |
|---|---|---|
| 🔭 **Scout** | Pantau 10 koin di chart 5 menit 24/7 dengan data pasar asli, laporkan setup | Tidak |
| 🧪 **Lab** | Uji setiap strategi × koin di 6 bulan data asli: fee, walk-forward, vs entry acak, Monte Carlo | Tidak |
| 🛡️ **Shield** | Ukuran posisi, maks 3 posisi, batas risiko total & korelasi, **rem darurat** | Tidak (hanya YA/TIDAK) |
| ✈️ **Pilot** | Limit order (maker) + stop-loss di server Binance, **hanya** jika Shield YA | Ya (izin auto dari Anda) |
| 📒 **Journal** | Catat setiap sinyal, keputusan, trade, alasannya | Tidak |
| 🧭 **Coordinator** | Satu-satunya yang bicara dengan Anda (Telegram) | Tidak |

Detail tiap agent + prompt-nya: [docs/AGENTS.md](docs/AGENTS.md)

## Alur satu trade

```
Candle 5m tutup di salah satu koin → Scout: "ada setup" (hanya pasangan yang lulus Lab)
  → Shield: cek rem, korelasi, risiko total, fee, hitung ukuran
  → (approval: auto — izin tetap dari Anda; atau tombol ✅ jika per_trade)
  → Shield cek ULANG dengan harga terbaru
  → Pilot: limit order post-only (maker). Tidak terisi dalam 1 candle → dibatalkan
  → Terisi (sebagian pun) → stop-loss DI SERVER BINANCE, diverifikasi
  → Take-profit (limit) · batas waktu 3 jam · Journal mencatat semuanya
```

## Aturan Anda

Semua ada di **[config.yaml](config.yaml)**, dalam bahasa Indonesia biasa.

## Dokumen

- [docs/DEPLOY.md](docs/DEPLOY.md): menyalakan bot 24/7 tanpa coding
- [docs/AGENTS.md](docs/AGENTS.md): tabel pilihan otomasi + kartu tiap agent
- [docs/STRATEGI.md](docs/STRATEGI.md): penjelasan setiap strategi (smart money, derivatif, klasik)
- [docs/STRESS_TEST.md](docs/STRESS_TEST.md): 7 kegagalan umum, apa yang terjadi, perbaikannya
- [docs/FINAL_CHECK.md](docs/FINAL_CHECK.md): daftar periksa sebelum uang asli + **WHY THIS COULD FAIL**

## Catatan teknis untuk pengembang

Python 3.12, dependensi: `requests`, `PyYAML`. Tes: `python -m pytest -q` (54 tes).
Perintah: `python -m madmac run | lab | selfcheck`.
