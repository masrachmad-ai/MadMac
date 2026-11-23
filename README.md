# MadMac — Tim Bot Trading BTC Futures (Binance)

Bot scalping **BTC/USDT Futures** yang dijalankan oleh 5 "agent", masing-masing dengan satu tugas.
**Saat ini semuanya mode PAPER (testnet).** Uang asli baru dipakai setelah Anda mengetik `go live`.

> ⚠️ Tidak ada bot yang menjamin profit. Scalping futures dengan leverage adalah gaya yang paling
> sering menghabiskan akun ritel. Bot ini dibangun untuk **melindungi modal dulu**, mencari cuan kedua.

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
| 🔭 **Scout** | Pantau chart BTC 5 menit 24/7, laporkan setup sesuai aturan | Tidak |
| 🧪 **Lab** | Uji setiap aturan di data asli Binance + fee + slippage | Tidak |
| 🛡️ **Shield** | Hitung ukuran posisi, cek semua batas, **rem darurat** | Tidak (hanya YA/TIDAK) |
| ✈️ **Pilot** | Kirim order **hanya** jika Shield YA **dan** Anda setuju | Ya, dengan 2 kunci |
| 📒 **Journal** | Catat setiap sinyal, keputusan, trade, alasannya | Tidak |
| 🧭 **Coordinator** | Satu-satunya yang bicara dengan Anda (Telegram) | Tidak |

Detail tiap agent + prompt-nya: [docs/AGENTS.md](docs/AGENTS.md)

## Alur satu trade

```
Candle 5m tutup → Scout: "ada setup" → Shield: cek & hitung ukuran
  → Coordinator kirim ke Telegram [✅ Setujui] [❌ Tolak]   (90 detik)
  → Anda tekan ✅ → Shield cek ULANG dengan harga terbaru
  → Pilot: order market + stop-loss & target DI SERVER BINANCE
  → Pilot verifikasi stop terpasang (kalau gagal → tutup posisi + rem)
  → Journal mencatat semuanya
```

## Aturan Anda

Semua ada di **[config.yaml](config.yaml)**, dalam bahasa Indonesia biasa.

## Dokumen

- [docs/DEPLOY.md](docs/DEPLOY.md): menyalakan bot 24/7 tanpa coding
- [docs/AGENTS.md](docs/AGENTS.md): tabel pilihan otomasi + kartu tiap agent
- [docs/STRESS_TEST.md](docs/STRESS_TEST.md): 7 kegagalan umum, apa yang terjadi, perbaikannya
- [docs/FINAL_CHECK.md](docs/FINAL_CHECK.md): daftar periksa sebelum uang asli + **WHY THIS COULD FAIL**

## Catatan teknis untuk pengembang

Python 3.12, dependensi: `requests`, `PyYAML`. Tes: `python -m pytest -q` (31 tes).
Perintah: `python -m madmac run | lab | selfcheck`.
